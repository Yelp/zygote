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

import zygote.util
import zygote.handlers
from zygote import message
from zygote import accounting
from zygote.zygote_process import Zygote

class ZygoteMaster(object):

    log = logging.getLogger('zygote.master')
    instantiated = False

    RECV_SIZE = 8096

    WORKER_TRANSITION_INTERVAL = 1.0 # number of seconds to poll when
                                     # transitioning workers

    def __init__(self, sock, basepath, module, num_workers, control_port, max_requests=None):
        if self.__class__.instantiated:
            self.log.error('cannot instantiate zygote master more than once')
            sys.exit(1)
        self.__class__.instantiated = True

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

        zygote.handlers.get_httpserver(self.io_loop, control_port, self)

    def reap_child(self, signum, frame):
        assert signum == signal.SIGCHLD
        while True:
            try:
                pid, status = os.waitpid(0, os.WNOHANG)
            except OSError, e:
                if e.errno == errno.ECHILD:
                    break
                raise
            if pid == 0:
                break

            status_code = os.WEXITSTATUS(status)
            self.log.info('zygote %d exited with status %d', pid, status_code)
            self.zygote_collection.remove_zygote(pid)

    def stop(self, signum=None, frame=None):

        def safe_kill(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

        pids = set()
        for zygote in self.zygote_collection:
            for worker in zygote.workers():
                safe_kill(worker.pid)
            pids.add(zygote.pid)
            safe_kill(zygote.pid)

        while pids:
            pid, status = os.wait()
            if pid in pids:
                pids.remove(pid)

        sys.exit(0)

    def recv_protol_msg(self, fd, events):
        """Callback for messages received on the domain_socket"""
        assert fd == self.domain_socket.fileno()
        data = self.domain_socket.recv(self.RECV_SIZE)
        msg = message.Message.parse(data)
        msg_type = type(msg)
        self.log.debug('received message of type %s from pid %d', msg_type.__name__, msg.pid)
        if msg_type is message.MessageWorkerStart:
            self.zygote_collection[msg.worker_ppid].add_worker(msg.pid, msg.time_created)
        elif msg_type is message.MessageWorkerExit:
            zygote = self.zygote_collection[msg.pid]
            zygote.remove_worker(msg.child_pid)

            if zygote == self.current_zygote:
                # request a respawn
                zygote.request_spawn()
            else:
                self.current_zygote.request_spawn()
                if zygote.worker_count == 0:
                    # if this zygote is not the current zygote, and it has no children
                    # left, kill it
                    os.kill(zygote.pid, signal.SIGTERM)

        elif msg_type is message.MessageHTTPBegin:
            worker = self.zygote_collection.get_worker(msg.pid)
            worker.start_request(msg.remote_ip, msg.http_line)
        elif msg_type is message.MessageHTTPEnd:
            worker = self.zygote_collection.get_worker(msg.pid)
            worker.end_request()
            if self.max_requests is not None and worker.request_count >= self.max_requests:
                self.log.info('child %d reached max_requests %d, killing it', worker.pid, self.max_requests)
                os.kill(worker.pid, signal.SIGTERM)

    def transition_idle_workers(self):
        """Transition idle HTTP workers from old zygotes to the current
        zygote.
        """
        zygote_count = 0
        kill_count = 0
        for z in self.zygote_collection.other_zygotes(self.current_zygote):
            zygote_count += 1
            for worker in z.idle_workers():
                os.kill(worker.pid, signal.SIGTERM)
                kill_count += 1
        self.log.info('Attempted to transition %d workers from %d zygotes', kill_count, zygote_count)

        if zygote_count:
            # The list of other_zygotes was at least one, so we should
            # reschedule another call to transition_idle_workers. When a zygote
            # runs out of worker children, the recv_protocol_msg function will
            # notice this fact when it receives the final MessageWorkerExit, and
            # at that time it will kill the worker, which is how this timeout
            # loop gets ended.
            self.io_loop.add_timeout(time.time() + self.WORKER_TRANSITION_INTERVAL, self.transition_idle_workers)

    def update_revision(self, signum=None, frame=None):
        """The SIGHUP handler, calls create zygote and stuff"""
        is_new, z = self.create_zygote(True)
        if not is_new:
            self.log.warning('received SIGHUP for old the current code revision, ignoring')
        else:
            self.transition_idle_workers()

    def create_zygote(self, make_current=True):
        """"Create a new zygote"""
        # read the basepath symlink
        realbase = os.path.realpath(self.basepath)
        z = self.zygote_collection.basepath_to_zygote(realbase)
        if z is not None:
            # a zygote for this basepath already exists, reuse that
            self.log.info('zygote for base %r already exists, reusing %d', realbase, z.pid)
            return False, z
        else:
            # the basepath has changed, create a new zygote
            pid = os.fork()
            if pid:
                self.log.info('started zygote %d pointed at base %r', pid, realbase)
                z = self.zygote_collection.add_zygote(pid, realbase)
                if make_current:
                    self.current_zygote = z
                return True, z
            else:
                # Try to clean up some of the file descriptors and whatnot that
                # exist in the parent before continuing. Strictly speaking, this
                # isn't necessary, but it seems good to remove these resources
                # if they're not needed in the child.
                del self.io_loop
                self.domain_socket.close()

                # create the zygote
                z = Zygote(self.sock, realbase, self.module)
                z.loop()

    def start(self):
        _, z = self.create_zygote()
        for x in xrange(self.num_workers):
            z.request_spawn()
        self.io_loop.start()

def main(opts, module):
    zygote.util.setproctitle('[zygote master %s]' % (module,))

    # Initialize the logging module
    log = logging.getLogger('zygote')
    formatter = logging.Formatter('[%(process)d] %(asctime)s :: %(levelname)-7s :: %(name)s - %(message)s')

    if os.isatty(sys.stderr.fileno()):
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG if opts.debug else logging.INFO)
        console_handler.setFormatter(formatter)
        log.addHandler(console_handler)

    if not logging.root.handlers:
        logging.root.addHandler(logging.NullHandler())

    if opts.debug:
        logging.root.setLevel(logging.DEBUG)
        log.setLevel(logging.DEBUG)
    else:
        logging.root.setLevel(logging.INFO)
        log.setLevel(logging.INFO)
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

    master = ZygoteMaster(sock, opts.basepath, module, opts.num_workers, opts.control_port, opts.max_requests)
    master.start()
