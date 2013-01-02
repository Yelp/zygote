import atexit
import datetime
import errno
import fcntl
import logging
import os
import signal
import socket
import struct
import sys
import time

import tornado.ioloop

from zygote import accounting
from zygote import handlers
from zygote import message
from zygote.util import close_fds
from zygote.util import safe_kill
from zygote.util import setproctitle
from zygote.util import wait_for_pids
from zygote.util import ZygoteIOLoop
from zygote.util import get_logger
from zygote.util import NullHandler
from zygote.util import LocklessHandler
from zygote.worker import INIT_FAILURE_EXIT_CODE
from zygote.worker import ZygoteWorker


try:
    import ssl # Python 2.6+
except ImportError:
    ssl = None


class ZygoteMaster(object):

    instantiated = False

    RECV_SIZE = 8192

    # number of seconds to wait between polls
    POLL_INTERVAL = 1.0

    # how many seconds to wait before sending SIGKILL to children
    WAIT_FOR_KILL_TIME = 10.0

    def __init__(
        self,
        sock,
        basepath,
        module,
        name,
        version,
        num_workers,
        control_port,
        control_socket_path,
        application_args=None,
        max_requests=None,
        zygote_base=None,
        ssl_options=None,
        debug=False
    ):
        self.logger = get_logger('zygote.master', debug)
        if self.__class__.instantiated:
            self.logger.error('cannot instantiate zygote master more than once')
            sys.exit(1)
        self.__class__.instantiated = True

        self.sock = sock
        self.basepath = basepath
        self.module = module
        self.name = name
        self.version = version
        self.num_workers = num_workers
        self.control_port = control_port
        self.control_socket_path = control_socket_path
        self.application_args = application_args or []
        self.max_requests = max_requests
        self.zygote_base = zygote_base
        self.ssl_options = ssl_options
        self.debug = debug

        self.stopped = False
        self.started_transition = None
        self.prev_zygote = None
        self.current_zygote = None
        self.time_created = datetime.datetime.now()
        self.io_loop = ZygoteIOLoop(log_name='zygote.master.ioloop')
        self.zygote_collection = accounting.ZygoteCollection()

        self.setup_master_socket()
        self.setup_control_socket()

        signal.signal(signal.SIGCHLD, self.reap_child)
        signal.signal(signal.SIGHUP, self.update_revision)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGQUIT):
            signal.signal(sig, self.stop)

        self.open_fds, self.status_http_server = handlers.get_httpserver(
                self.io_loop,
                self.control_port,
                self,
                zygote_base=self.zygote_base,
                ssl_options=self.ssl_options,
        )

    def setup_master_socket(self):
        """Create an abstract unix domain socket for master. This
        socket will be used to receive messages from zygotes and their
        children.
        """
        self.logger.debug("Binding to master domain socket")
        self.master_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM, 0)
        self.master_socket.bind('\0zygote_%d' % os.getpid())
        self.io_loop.add_handler(self.master_socket.fileno(), self.handle_protocol_msg, self.io_loop.READ)

    def setup_control_socket(self):
        try:
            socket_path = self.control_socket_path
            if os.path.exists(socket_path):
                # NOTE: Starting the same application twice we won't get
                # here since main() won't be able to bind. We can add a
                # (ex|nb) file lock if needed.
                self.logger.error("Control socket exitsts %s. Probably from a previous run. Removing...", socket_path)
                self.cleanup_control_socket()
            self.logger.debug("Binding to control socket %s", socket_path)
            self.control_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM, 0)
            self.control_socket.bind(socket_path)
            self.io_loop.add_handler(self.control_socket.fileno(), self.handle_control_msg, self.io_loop.READ)
        except Exception, e:
            # This is treated as a fatal error as only way to recover
            # from this is to restart zygote master and it's not what
            # we want.
            self.logger.error("Can not bind to control socket: %s", e)
            self.logger.error("Control socket is needed to make configuration changes on the running zygote master.")
            sys.exit(1)

    def cleanup_control_socket(self):
        if os.path.exists(self.control_socket_path):
            self.logger.debug("Removing control socket at %s" % self.control_socket_path)
            os.unlink(self.control_socket_path)

    def handle_control_msg(self, fd, events):
        assert fd == self.control_socket.fileno()
        data = self.control_socket.recv(self.RECV_SIZE)
        msg = message.ControlMessage.parse(data)
        msg_type = type(msg)

        # NOTE: We can possibly use SO_PEERCRED on control socket to
        # get more information about the client.
        self.logger.info('received message of type %s', msg_type.__name__,)

        if msg_type is message.ControlMessageScaleWorkers:
            self.scale_workers(msg.num_workers)

    def handle_protocol_msg(self, fd, events):
        """Callback for messages received on the master_socket"""
        assert fd == self.master_socket.fileno()
        data = self.master_socket.recv(self.RECV_SIZE)
        msg = message.Message.parse(data)
        msg_type = type(msg)
        self.logger.debug('received message of type %s from pid %d', msg_type.__name__, msg.pid)

        if msg_type is message.MessageCanaryInit:
            self.logger.info("Canary zygote initialized. Transitioning idle workers.")
            # This is not the canary zygote anymore
            self.current_zygote.canary = False
            # We can also release the handle on the previous
            # zygote. It is already in the zygote_collection for
            # accounting purposses, but we won't need to keep track of
            # it anymore.
            self.prev_zygote = None
            # Canary initialization was successful, we can now transition workers
            self.io_loop.add_callback(self.transition_idle_workers)
        elif msg_type is message.MessageWorkerStart:
            # a new worker was spawned by one of our zygotes; add it to
            # zygote_collection, and note the time created and the zygote parent
            zygote = self.zygote_collection[msg.worker_ppid]
            if zygote:
                zygote.add_worker(msg.pid, msg.time_created)
        elif msg_type is message.MessageWorkerExitInitFail:
            if not self.current_zygote.canary:
                self.logger.error("A worker initialization failed, giving up")
                self.stop()
                return
        elif msg_type is message.MessageWorkerExit:
            # a worker exited. tell the current/active zygote to spawn a new
            # child. if this was the last child of a different (non-current)
            # zygote, kill that zygote
            zygote = self.zygote_collection[msg.pid]
            if not zygote:
                return

            zygote.remove_worker(msg.child_pid)
            if zygote.shutting_down:
                self.logger.debug('Removed a worker from shutting down zygote %d, %d left', msg.pid, len(zygote.workers()))
                return
            else:
                self.logger.debug('Removed a worker from zygote %d, %d left', msg.pid, len(zygote.workers()))

            if not self.stopped:
                if zygote in (self.current_zygote, self.prev_zygote):
                    if self.num_workers > zygote.worker_count:
                        # Only start a new if we're below quota. This
                        # is how we scale down the number of workers.
                        zygote.request_spawn()
                else:
                    # Not a zygote that we care about. Request shutdown.
                    zygote.request_shut_down()
        elif msg_type is message.MessageHTTPBegin:
            # a worker started servicing an HTTP request
            worker = self.zygote_collection.get_worker(msg.pid)
            if worker:
                worker.start_request(msg.remote_ip, msg.http_line)
        elif msg_type is message.MessageHTTPEnd:
            # a worker finished servicing an HTTP request
            worker = self.zygote_collection.get_worker(msg.pid)
            if worker:
                worker.end_request()
                if self.max_requests is not None and worker.request_count >= self.max_requests:
                    self.logger.info('Worker %d reached max_requests %d, killing it', worker.pid, self.max_requests)
                    safe_kill(worker.pid, signal.SIGQUIT)
        else:
            self.logger.warning('master got unexpected message of type %s', msg_type)


    def scale_workers(self, num_workers):
        prev_num_workers = self.num_workers
        diff_num_workers = num_workers - self.num_workers
        self.num_workers = num_workers
        if not diff_num_workers:
            return
        elif diff_num_workers > 0:
            self.logger.info('Increasing number of workers from %d to %d.', prev_num_workers, num_workers)
            for _ in range(diff_num_workers):
                self.current_zygote.request_spawn()
        else:
            self.logger.info('Reducing number of workers from %d to %d.', prev_num_workers, num_workers)
            self.current_zygote.request_kill_workers(-diff_num_workers)

    def reap_child(self, signum, frame):
        """Signal handler for SIGCHLD. Reaps children and updates
        self.zygote_collection.
        """
        assert signum == signal.SIGCHLD
        while True:
            try:
                # The Zygotes are in their own process group, so need to
                # call waitpid() with -1 instead of 0. See waitpid(2).
                pid, status = os.waitpid(-1, os.WNOHANG)
            except OSError, e:
                if e.errno == errno.ECHILD:
                    break
                elif e.errno == errno.EINTR:
                    continue
                raise
            if pid == 0:
                break

            status_code = os.WEXITSTATUS(status)
            self.logger.info('zygote %d exited with status %d', pid, status_code)

            # the zygote died. if the zygote was not the current zygote it's OK;
            # otherwise, we need to start a new one
            try:
                self.zygote_collection.remove_zygote(pid)
            except KeyError:
                pass

            if status_code == INIT_FAILURE_EXIT_CODE:
                if pid == self.current_zygote.pid and self.current_zygote.canary:
                    if self.prev_zygote:
                        self.curent_zygote = self.prev_zygote
                    self.logger.error("Could not initialize canary worker. Giving up trying to respawn")
                else:
                    self.logger.error("Could not initialize zygote worker, giving up")
                    self.really_stop()
                return

            if not self.stopped:
                active_zygote = self.current_zygote

                if pid == self.current_zygote.pid:
                    self.current_zygote = self.create_zygote()
                    active_zygote = self.current_zygote
                elif self.prev_zygote and pid == self.prev_zygote.pid:
                    self.prev_zygote = self.create_zygote()
                    active_zygote = self.prev_zygote

                # we may need to create new workers for the active zygote... this
                # is a bit racy, although that seems to be pretty unlikely in
                # practice
                workers_needed = self.num_workers - self.zygote_collection.worker_count()
                for x in xrange(workers_needed):
                    active_zygote.request_spawn()

            elif len(self.zygote_collection.zygote_map.values()) == 0:
                self.really_stop()

    def stop(self, signum=None, frame=None):
        """
        Stop the zygote master. Steps:
          * Ask all zygotes to kill and wait on their children
          * Wait for zygotes to exit
          * Kill anything left over if necessary
        """
        if self.stopped:
            return
        # kill all of the workers
        self.logger.info('stopping all zygotes and workers')
        pids = set()
        for zygote in self.zygote_collection:
            pids.add(zygote.pid)
            self.logger.debug('requesting shutdown on %d', zygote.pid)
            zygote.request_shut_down()

        self.logger.debug('setting self.stopped')
        self.stopped = True

        self.logger.debug('master is stopping. will not try to update anymore.')
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

        self.logger.debug('stopping io_loop.')
        if getattr(self, 'io_loop', None) is not None:
            self.io_loop.stop()

        self.logger.info('waiting for workers to exit before stoping master.')
        wait_for_pids(pids, self.WAIT_FOR_KILL_TIME, self.logger, kill_pgroup=True)
        self.logger.info('all zygotes exited; good night')

        self.really_stop(0)

    def really_stop(self, status=0):
        self.cleanup_control_socket()
        sys.exit(status)

    def transition_idle_workers(self):
        """Transition idle HTTP workers from old zygotes to the current
        zygote.
        """
        if not self.started_transition:
            self.started_transition = time.time()
        if (time.time() - self.started_transition) > self.WAIT_FOR_KILL_TIME:
            self.logger.debug("sending SIGKILL for transition because it was Too Damn Slow")
            sig = signal.SIGKILL
        else:
            sig = signal.SIGQUIT

        other_zygotes = self.zygote_collection.other_zygotes(self.current_zygote)
        if self.current_zygote.canary and self.prev_zygote:
            if self.prev_zygote in other_zygotes:
                other_zygotes.remove(self.prev_zygote)

        kill_count = 0
        other_zygote_count = len(other_zygotes)
        for zygote in other_zygotes:
            for worker in zygote.idle_workers():
                self.logger.debug("killing worker %d with signal %d", worker.pid, sig)
                if safe_kill(worker.pid, sig):
                    kill_count += 1
        self.logger.info('Attempted to transition %d workers from %d zygotes', kill_count, other_zygote_count)

        if other_zygote_count:
            # The list of other zygotes was at least one, so we should
            # reschedule another call to transition_idle_workers. When a zygote
            # runs out of worker children, the handle_protocol_msg function will
            # notice this fact when it receives the final MessageWorkerExit, and
            # at that time it will kill the worker, which is how this timeout
            # loop gets ended.
            self.io_loop.add_timeout(time.time() + self.POLL_INTERVAL, self.transition_idle_workers)
        else:
            self.started_transition = None

        # Cleanup empty zygotes for the next iteration of the transition.
        for zygote in other_zygotes:
            if zygote.worker_count == 0:
                self.kill_empty_zygote(zygote, sig)

    def kill_empty_zygote(self, zygote, sig=signal.SIGQUIT):
        """Send zygote SIGQUIT if it has zero workers. """
        # The only valid time to kill a zygote is if it doesn't have
        # any workers left.
        if zygote.worker_count == 0:
            self.logger.info("killing zygote with pid %d" % zygote.pid)
            safe_kill(zygote.pid, sig)

    def update_revision(self, signum=None, frame=None):
        """The SIGHUP handler, calls create_zygote and possibly initiates the
        transition of idle workers.

        This preserves the current zygote and initializes a "canary"
        zygote as the current one.
        """
        self.prev_zygote = self.current_zygote
        self.current_zygote = self.create_zygote(canary=True)

    def create_zygote(self, canary=False):
        """"Create a new zygote"""
        # read the basepath symlink
        realbase = os.path.realpath(self.basepath)

        pid = os.fork()
        if pid:
            self.logger.info('started zygote %d pointed at base %r', pid, realbase)
            z = self.zygote_collection.add_zygote(pid, realbase, self.io_loop, canary=canary)
            if not canary:
                self.io_loop.add_callback(self.transition_idle_workers)
            return z
        else:
            # Try to clean up some of the file descriptors and whatnot that
            # exist in the parent before continuing. Strictly speaking, this
            # isn't necessary, but it seems good to remove these resources
            # if they're not needed in the child.
            del self.io_loop
            close_fds(self.sock.fileno())
            signal.signal(signal.SIGHUP, signal.SIG_DFL)

            # Make the zygote a process group leader
            os.setpgid(os.getpid(), os.getpid())
            # create the zygote
            z = ZygoteWorker(
                    sock=self.sock,
                    basepath=realbase,
                    module=self.module,
                    name=self.name,
                    version=self.version,
                    args=self.application_args,
                    ssl_options=self.ssl_options,
                    canary=canary,
                    debug=self.debug
            )
            z.loop()

    def start(self):
        self.current_zygote = self.create_zygote()
        for x in xrange(self.num_workers):
            self.current_zygote.request_spawn()
        self.io_loop.start()

def main(opts, extra_args):
    setproctitle('zygote master %s' % (opts.name or opts.module,))
    zygote_logger = get_logger('zygote', opts.debug)

    if not logging.root.handlers:
        # XXX: WARNING
        #
        # We're disabling the root logger. Tornado's RequestHandler ONLY
        # supports logging uncaught errors to the root logger. This will end
        # poorly for you!
        #
        # We should probably provide a RequestHandler subclass that has
        # _handle_request_exception overridden to do something useful.
        # That might be hard to do without adding a tight version dependency
        # on tornado.
        logging.root.addHandler(NullHandler())

    if opts.debug:
        logging.root.setLevel(logging.DEBUG)
    else:
        logging.root.setLevel(logging.INFO)

    zygote_logger.info('main started')

    # Create the TCP listen socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    flags = fcntl.fcntl(sock.fileno(), fcntl.F_GETFD)
    flags |= fcntl.FD_CLOEXEC
    fcntl.fcntl(sock.fileno(), fcntl.F_SETFD, flags)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setblocking(0)
    sock.bind((opts.interface, opts.port))
    sock.listen(128)

    ssl_options=None
    if opts.cert:
        ssl_options = dict(
                certfile=opts.cert,
                keyfile=opts.key,
                ca_certs=opts.cacerts,
                cert_reqs=ssl.CERT_OPTIONAL if opts.cacerts else ssl.CERT_NONE,
        )
        zygote_logger.info('using SSL with %s', ssl_options)

        sock = ssl.wrap_socket(sock,
                server_side=True,
                do_handshake_on_connect=False,
                **ssl_options
        )

    master = ZygoteMaster(
        sock,
        basepath=opts.basepath,
        module=opts.module,
        name=opts.name or opts.module,
        version=opts.version,
        num_workers=opts.num_workers,
        control_port=opts.control_port,
        control_socket_path=opts.control_socket_path,
        application_args=extra_args,
        max_requests=opts.max_requests,
        zygote_base=opts.zygote_base,
        ssl_options=ssl_options,
        debug=opts.debug
    )
    atexit.register(master.stop)
    master.start()
