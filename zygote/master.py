import atexit
import datetime
import errno
import fcntl
import logging
import os
import signal
import socket
import sys
import time

import tornado.ioloop
import tornado.httpserver
import tornado.web

import zygote.handlers

from .util import safe_kill, close_fds, setproctitle, list_open_fds
from zygote import message
from zygote import accounting
from zygote.worker import ZygoteWorker

if hasattr(logging, 'NullHandler'):
    NullHandler = logging.NullHandler
else:
    class NullHandler(logging.Handler):
        def emit(self, record):
            pass

log = logging.getLogger('zygote.master')

class ZygoteMaster(object):

    instantiated = False

    RECV_SIZE = 8096

    # number of seconds to wait between polls
    POLL_INTERVAL = 1.0

    def __init__(self, sock, basepath, module, num_workers, control_port, application_args=[], max_requests=None, zygote_base=None):
        if self.__class__.instantiated:
            log.error('cannot instantiate zygote master more than once')
            sys.exit(1)
        self.__class__.instantiated = True
        self.stopped = False

        self.application_args = application_args
        self.io_loop = tornado.ioloop.IOLoop()
        self.sock = sock
        self.basepath = basepath
        self.module = module
        self.num_workers = num_workers
        self.max_requests = max_requests
        self.time_created = datetime.datetime.now()

        self.current_zygote = None
        self.zygote_collection = accounting.ZygoteCollection()

        # create an abstract unix domain socket. this socket will be used to
        # receive messages from zygotes and their children
        self.domain_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM, 0)
        self.domain_socket.bind('\0zygote_%d' % os.getpid())
        self.io_loop.add_handler(self.domain_socket.fileno(), self.recv_protol_msg, self.io_loop.READ)

        signal.signal(signal.SIGCHLD, self.reap_child)
        signal.signal(signal.SIGHUP, self.update_revision)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self.stop)

        zygote.handlers.get_httpserver(self.io_loop, control_port, self, zygote_base=zygote_base)

    def reap_child(self, signum, frame):
        """Signal handler for SIGCHLD. Reaps children and updates
        self.zygote_collection.
        """
        assert signum == signal.SIGCHLD
        while True:
            try:
                pid, status = os.waitpid(0, os.WNOHANG)
            except OSError, e:
                if e.errno == errno.ECHILD:
                    break
                elif e.errno == errno.EINTR:
                    continue
                raise
            if pid == 0:
                break

            status_code = os.WEXITSTATUS(status)
            log.info('zygote %d exited with status %d', pid, status_code)

            # the zygote died. if the zygote was not the current zygote it's OK;
            # otherwise, we need to start a new one
            try:
                self.zygote_collection.remove_zygote(pid)
            except KeyError:
                pass

            if not self.stopped:
                if pid == self.current_zygote.pid:
                    self.current_zygote = self.create_zygote()

                # we may need to create new workers for the current zygote... this
                # is a bit racy, although that seems to be pretty unlikely in
                # practice
                workers_needed = self.num_workers - self.zygote_collection.worker_count()
                for x in xrange(workers_needed):
                    self.current_zygote.request_spawn()

            elif len(self.zygote_collection.zygote_map.values()) == 0:
                self.really_stop()

    def stop(self, signum=None, frame=None):
        """Stop the zygote master, by killing all workers and zygote processes,
        and then exiting with status 0 from the master.
        """

        # kill all of the workers
        log.info('stopping all zygotes and workers')
        pids = set()
        for zygote in self.zygote_collection:
            for worker in zygote.workers():
                safe_kill(worker.pid)

        # now we have to wait until all of the workers actually exit... at that
        # point self.really_stop() will be called
        self.stopped = True

    def really_stop(self, status=0):
        sys.exit(status)

    def recv_protol_msg(self, fd, events):
        """Callback for messages received on the domain_socket"""
        assert fd == self.domain_socket.fileno()
        data = self.domain_socket.recv(self.RECV_SIZE)
        msg = message.Message.parse(data)
        msg_type = type(msg)
        log.debug('received message of type %s from pid %d', msg_type.__name__, msg.pid)

        if msg_type is message.MessageWorkerStart:
            # a new worker was spawned by one of our zygotes; add it to
            # zygote_collection, and note the time created and the zygote parent
            self.zygote_collection[msg.worker_ppid].add_worker(msg.pid, msg.time_created)
        elif msg_type is message.MessageWorkerExit:
            # a worker exited. tell the current/active zygote to spawn a new
            # child. if this was the last child of a different (non-current)
            # zygote, kill that zygote
            zygote = self.zygote_collection[msg.pid]
            zygote.remove_worker(msg.child_pid)

            if self.stopped:
                # if we're in stopping mode, don't kill the zygote until all of
                # its children have exited
                if zygote.worker_count == 0:
                    os.kill(zygote.pid, signal.SIGTERM)
            else:
                self.current_zygote.request_spawn()
                if zygote != self.current_zygote and zygote.worker_count == 0:
                    # not the current zygote, and no children left; kill it
                    # left, kill it; shouldn't need to safe_kill here
                    os.kill(zygote.pid, signal.SIGTERM)
        elif msg_type is message.MessageHTTPBegin:
            # a worker started servicing an HTTP request
            worker = self.zygote_collection.get_worker(msg.pid)
            worker.start_request(msg.remote_ip, msg.http_line)
        elif msg_type is message.MessageHTTPEnd:
            # a worker finished servicing an HTTP request
            worker = self.zygote_collection.get_worker(msg.pid)
            worker.end_request()
            if self.max_requests is not None and worker.request_count >= self.max_requests:
                log.info('child %d reached max_requests %d, killing it', worker.pid, self.max_requests)
                os.kill(worker.pid, signal.SIGTERM)

    def transition_idle_workers(self):
        """Transition idle HTTP workers from old zygotes to the current
        zygote.
        """
        other_zygote_count = 0
        kill_count = 0
        for z in self.zygote_collection.other_zygotes(self.current_zygote):
            other_zygote_count += 1
            for worker in z.idle_workers():
                os.kill(worker.pid, signal.SIGTERM)
                kill_count += 1
        log.info('Attempted to transition %d workers from %d zygotes', kill_count, other_zygote_count)

        if other_zygote_count:
            # The list of other zygotes was at least one, so we should
            # reschedule another call to transition_idle_workers. When a zygote
            # runs out of worker children, the recv_protocol_msg function will
            # notice this fact when it receives the final MessageWorkerExit, and
            # at that time it will kill the worker, which is how this timeout
            # loop gets ended.
            self.io_loop.add_timeout(time.time() + self.POLL_INTERVAL, self.transition_idle_workers)

    def update_revision(self, signum=None, frame=None):
        """The SIGHUP handler, calls create_zygote and possibly initiates the
        transition of idle workers.
        """
        self.create_zygote()

    def create_zygote(self):
        """"Create a new zygote"""
        # read the basepath symlink
        realbase = os.path.realpath(self.basepath)

        pid = os.fork()
        if pid:
            log.info('started zygote %d pointed at base %r', pid, realbase)
            z = self.zygote_collection.add_zygote(pid, realbase, self.io_loop)
            self.current_zygote = z
            self.io_loop.add_callback(self.transition_idle_workers)
            return z
        else:
            # Try to clean up some of the file descriptors and whatnot that
            # exist in the parent before continuing. Strictly speaking, this
            # isn't necessary, but it seems good to remove these resources
            # if they're not needed in the child.
            self.io_loop.stop()
            del self.io_loop
            list_open_fds()
            close_fds(self.sock.fileno())
            signal.signal(signal.SIGHUP, signal.SIG_DFL)

            # create the zygote
            z = ZygoteWorker(self.sock, realbase, self.module, self.application_args)
            z.loop()
            sys.exit(1) # not reached

    def start(self):
        z = self.create_zygote()
        for x in xrange(self.num_workers):
            z.request_spawn()
        self.io_loop.start()

def main(opts, extra_args):
    if not sys.stdin.closed:
        # Curiously, this doesn't close(2) the underlying file descriptor
        # (probably to "reserve" fd 0). This still seems like a good thing to
        # do, though, since we're not using stdin
        sys.stdin.close()

    setproctitle('[zygote master %s]' % (opts.module,))

    # Initialize the logging module
    formatter = logging.Formatter('[%(process)d] %(asctime)s :: %(levelname)-7s :: %(name)s - %(message)s')
    zygote_logger = logging.getLogger('zygote')

    if os.isatty(sys.stderr.fileno()):
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG if opts.debug else logging.INFO)
        console_handler.setFormatter(formatter)
        zygote_logger.addHandler(console_handler)

    if not logging.root.handlers:
        logging.root.addHandler(NullHandler())

    if opts.debug:
        logging.root.setLevel(logging.DEBUG)
        zygote_logger.setLevel(logging.DEBUG)
    else:
        logging.root.setLevel(logging.INFO)
        zygote_logger.setLevel(logging.INFO)
    log.info('main started')

    # Create the TCP listen socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    flags = fcntl.fcntl(sock.fileno(), fcntl.F_GETFD)
    flags |= fcntl.FD_CLOEXEC
    fcntl.fcntl(sock.fileno(), fcntl.F_SETFD, flags)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setblocking(0)
    sock.bind((opts.interface, opts.port))
    sock.listen(128)
    master = ZygoteMaster(sock, opts.basepath, opts.module, opts.num_workers, opts.control_port, extra_args, opts.max_requests, opts.zygote_base)
    atexit.register(master.stop)
    master.start()
