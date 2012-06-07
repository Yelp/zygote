import atexit
import errno
import logging
import os
import signal
import socket
import sys
import time

import tornado
from ._httpserver import HTTPServer
from .tornado_proxy import ProxyWrappedHTTPServer

from .util import setproctitle, AFUnixSender, ZygoteIOLoop, safe_kill, wait_for_pids, set_nonblocking
from .message import Message, MessageCreateWorker, MessageWorkerStart, MessageWorkerExit, MessageWorkerExitInitFail, MessageHTTPEnd, MessageHTTPBegin, MessageShutDown

# Exit with this exit code when there was a failure to init the worker
# (which might be hard to represent otherwise if it, for example, occurs
# while setting up the domain socket)
INIT_FAILURE_EXIT_CODE = 4

WORKER_INIT_FAILURE_EXIT_CODE = 5

def establish_signal_handlers(logger):
    # delete atexit handlers from parent
    del atexit._exithandlers[:]

    def zygote_exit(signum, frame):
        if signum == signal.SIGINT:
            logger.info('received SIGINT, exiting')
        elif signum == signal.SIGTERM:
            logger.info('received SIGTERM, exiting')
        elif signum == signal.SIGQUIT:
            logger.info('recieved SIGQUIT (clean exit), exiting')
        else:
            logger.info('received signal %d, exiting', signum)
        sys.exit(0)
    # we explicitly ignore SIGINT and SIGTERM
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    for sig in (signal.SIGQUIT,):
        signal.signal(sig, zygote_exit)

def notify(sock, msg_cls, body=''):
    """Send a message to the zygote master. Should be using AFUnixSender?"""
    sock.send(msg_cls.emit(str(body)))

class ZygoteWorker(object):
    """A Zygote is a process that manages children worker processes.

    When the zygote process is instantiated it does a few things:
     * chdirs to the absolute position pointed by a basepath symlink
     * munges sys.path to point to the new version of the code
     * imports the target module, to pre-fork load resources
     * creates read and write pipes to the parent process
    """

    log = logging.getLogger('zygote.worker.zygote_process')

    RECV_SIZE = 8192

    # how many seconds to wait before sending SIGKILL to children
    WAIT_FOR_KILL_TIME = 10.0

    def __init__(self, sock, basepath, module, args, ssl_options=None, expect_proxy=False):
        self.args = args
        self.ssl_options = ssl_options
        self.expect_proxy = expect_proxy
        self.ppid = os.getppid()

        # Set up the control socket nice and early
        try:
            self.control_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM, 0)
            self.control_socket.bind('\0zygote_%d' % os.getpid())
        except Exception:
            # If we can't bind to the control socket, just give up
            self.log.error("Could not bind to control socket, aborting early!")
            sys.exit(INIT_FAILURE_EXIT_CODE)

        try:
            self._real_init(sock, basepath, module, args)
        except Exception:
            self.log.exception("Error performing initializtion of %s", self)
            sys.exit(INIT_FAILURE_EXIT_CODE)

    def _real_init(self, sock, basepath, module, args):
        """Actual initialization function. Broken out for error handling"""

        self.version = basepath.split('/')[-1]
        setproctitle('zygote version=%s' % (self.version,))

        # Create a pipe(2) pair. This will be used so workers can detect when
        # the intermediate zygote exits -- when this happens, a read event will
        # happen on the read_pipe file descriptor, and the child can exit. We do
        # this so that if the intermediate zygote exits unexpectedly for some
        # reason, while it still has children workers running (which is an
        # abnormal situation in and of itself), we aren't left with orphaned
        # worker processes. Note that the write_pipe is normally never written
        # on, we're just using this hack to get a read event on the read pipe.
        self.read_pipe, self.write_pipe = os.pipe()

        self.io_loop = ZygoteIOLoop(log_name='zygote.worker.ioloop')

        os.chdir(basepath)
        sys.path.insert(0, basepath)
        t = __import__(module, [], [], ['initialize', 'get_application'], 0)

        self.sock = sock

        self.get_application = t.get_application

        establish_signal_handlers(self.log)

        set_nonblocking(self.control_socket)
        self.io_loop.add_handler(self.control_socket.fileno(), self.handle_control, self.io_loop.READ)

        self.notify_socket = AFUnixSender(self.io_loop)
        self.notify_socket.connect('\0zygote_%d' % self.ppid)

        signal.signal(signal.SIGCHLD, self.reap_child)

        # If there is an initialize function defined then call it.
        if hasattr(t, 'initialize'):
            self.log.info('initializing zygote')
            t.initialize(*self.args)

        self.log.info('new zygote started')

    def handle_control(self, fd, events):
        assert fd == self.control_socket.fileno()
        data = self.control_socket.recv(self.RECV_SIZE)
        msg = Message.parse(data)
        if type(msg) is MessageCreateWorker:
            self.spawn_worker()
        elif type(msg) is MessageShutDown:
            self.kill_workers(msg.pids)
        else:
            assert False

    def kill_workers(self, pids):
        """Kill all workers and wait (synchronously) for them
        to exit"""
        # reset the signal handler so that we don't get interrupted
        # by SIGCHLDs
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        waiting_pids = set()

        self.log.debug('zygote requesting kill on %d pids', len(pids))
        for pid in pids:
            if safe_kill(pid, signal.SIGQUIT):
                waiting_pids.add(pid)
        wait_for_pids(waiting_pids, self.WAIT_FOR_KILL_TIME, self.log)
        self.log.debug('zygote done killing children, terminating')
        sys.exit(0)

    def reap_child(self, signum, frame):
        assert signum == signal.SIGCHLD
        while True:
            try:
                pid, status = os.waitpid(0, os.WNOHANG)
            except OSError, e:
                if e.errno == errno.ECHILD:
                    break
                elif e.errno == errno.EINTR:
                    continue
                raise # should just be EINVAL on Linux

            if pid == 0:
                break

            status_code = os.WEXITSTATUS(status)
            self.log.info('reaped worker %d, status %d', pid, status_code)
            if status_code == WORKER_INIT_FAILURE_EXIT_CODE:
                notify(self.notify_socket, MessageWorkerExitInitFail, '%d %d' % (pid, status_code))
            else:
                notify(self.notify_socket, MessageWorkerExit, '%d %d' % (pid, status_code))

    def loop(self):
        self.io_loop.start()

    def spawn_worker(self):
        time_created = time.time()
        pid = os.fork()
        if not pid:
            try:
                self.log.debug("Calling _initialize_worker")
                self._initialize_worker(time_created)
                self.log.debug("Worker initialized")
            except Exception, e:
                self.log.exception("Error initializing worker process: %s", e)
                sys.exit(WORKER_INIT_FAILURE_EXIT_CODE)
            self.log.debug("Looks okay to me, smooth sailing!")

    def _initialize_worker(self, time_created):
        # We're the child. We need to close the write_pipe in order for the
        # read_pipe to get an event when the parent's write_pipe closes
        # (otherwise the kernel is too smart and thinks that it's waiting
        # for writes from *this* process' write_pipe).
        os.close(self.write_pipe)

        log = logging.getLogger('zygote.worker.worker_process')
        log.debug('new worker started')

        def on_parent_exit(fd, events):
            log.error('detected that intermediate zygote died, exiting')
            sys.exit(0)

        # create a new i/o loop
        del self.io_loop
        io_loop = ZygoteIOLoop(log_name='zygote.worker.worker_process.ioloop')

        # add the read pipe
        io_loop.add_handler(self.read_pipe, on_parent_exit, io_loop.READ)

        sock = AFUnixSender(io_loop, logger=log)
        sock.connect('\0zygote_%d' % self.ppid)

        establish_signal_handlers(log)
        def on_headers(line, headers):
            log.debug('sending MessageHTTPBegin')
            notify(sock, MessageHTTPBegin, line)
        def on_close(disconnected=False):
            log.debug('sending MessageHTTPEnd')
            notify(sock, MessageHTTPEnd)

        notify(sock, MessageWorkerStart, '%d %d' % (int(time_created * 1e6), os.getppid()))
        setproctitle('zygote-worker version=%s' % self.version)
        try:
            # io_loop is passed into get_application for program to add handler
            # or schedule task on the main io_loop.  Program that uses this
            # io_loop instance should NOT use io_loop.start() because start()
            # is invoked by the corresponding zygote worker.
            kwargs = {'io_loop': io_loop}
            log.debug("Invoking get_application")
            app = self.get_application(*self.args, **kwargs)
        except Exception:
            log.error("Unable to get application")
            raise
        # TODO: make keep-alive servers work
        log.debug("Creating HTTPServer")
        if self.expect_proxy:
            server = ProxyWrappedHTTPServer
        else:
            server = HTTPServer
        http_server = server(app,
                io_loop=io_loop,
                no_keep_alive=True,
                close_callback=on_close,
                headers_callback=on_headers,
                ssl_options=self.ssl_options
        )
        if tornado.version_info >= (2,1,0):
            http_server.add_socket(self.sock)
        else:
            http_server._socket = self.sock
            io_loop.add_handler(self.sock.fileno(), http_server._handle_events, io_loop.READ)
        log.debug("Started ioloop...")
        io_loop.start()

