import atexit
import errno
import logging
import os
import signal
import socket
import sys
import time

import tornado.ioloop
import tornado.httpserver
from ._httpserver import HTTPServer

from .util import setproctitle, AFUnixSender, close_fds
from .message import Message, MessageCreateWorker, MessageWorkerStart, MessageWorkerExit, MessageHTTPEnd, MessageHTTPBegin


def establish_signal_handlers(logger):
    # delete atexit handlers from parent
    del atexit._exithandlers[:]

    def zygote_exit(signum, frame):
        if signum == signal.SIGINT:
            logger.info('received SIGINT, exiting')
        elif signum == signal.SIGTERM:
            logger.info('received SIGTERM, exiting')
        else:
            logger.info('received signal %d, exiting', signum)
        sys.exit(0)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, zygote_exit)

def notify(sock, msg_cls, body=''):
    """Send a message to the zygote master"""
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

    RECV_SIZE = 8096

    def __init__(self, sock, basepath, module, args):
        self.args = args
        self.version = basepath.split('/')[-1]
        self.ppid = os.getppid()
        setproctitle('[zygote version=%s]' % (self.version,))

        # Create a pipe(2) pair. This will be used so workers can detect when
        # the intermediate zygote exits -- when this happens, a read event will
        # happen on the read_pipe file descriptor, and the child can exit. We do
        # this so that if the intermediate zygote exits unexpectedly for some
        # reason, while it still has children workers running (which is an
        # abnormal situation in and of itself), we aren't left with orphaned
        # worker processes. Note that the write_pipe is normally never written
        # on, we're just using this hack to get a read event on the read pipe.
        self.read_pipe, self.write_pipe = os.pipe()

        self.io_loop = tornado.ioloop.IOLoop()

        os.chdir(basepath)
        sys.path.insert(0, basepath)
        t = __import__(module, [], [], ['get_application'], 0)

        self.sock = sock

        self.get_application = t.get_application

        establish_signal_handlers(self.log)

        self.control_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM, 0)
        self.control_socket.bind('\0zygote_%d' % os.getpid())
        #self.io_loop._set_nonblocking(self.control_socket)
        self.io_loop.add_handler(self.control_socket.fileno(), self.handle_control, self.io_loop.READ)

        self.notify_socket = AFUnixSender(self.io_loop)
        self.notify_socket.connect('\0zygote_%d' % self.ppid)

        signal.signal(signal.SIGCHLD, self.reap_child)

        self.log.info('new zygote started')

    def handle_control(self, fd, events):
        assert fd == self.control_socket.fileno()
        data = self.control_socket.recv(self.RECV_SIZE)
        msg = Message.parse(data)
        if type(msg) is MessageCreateWorker:
            self.spawn_worker()
        else:
            assert False

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
            notify(self.notify_socket, MessageWorkerExit, '%d %d' % (pid, status_code))

    def loop(self):
        self.io_loop.start()

    def spawn_worker(self):
        time_created = time.time()
        pid = os.fork()
        if not pid:
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
            self.io_loop.stop()
            del self.io_loop

            close_fds(self.sock.fileno(), self.read_pipe)
            io_loop = tornado.ioloop.IOLoop()

            # add the read pipe
            io_loop.add_handler(self.read_pipe, on_parent_exit, io_loop.READ)

            sock = AFUnixSender(io_loop)
            sock.connect('\0zygote_%d' % self.ppid)

            establish_signal_handlers(log)
            def on_line(line):
                log.debug('sending MessageHTTPBegin')
                notify(sock, MessageHTTPBegin, line)
            def on_close():
                log.debug('sending MessageHTTPEnd')
                notify(sock, MessageHTTPEnd)

            notify(sock, MessageWorkerStart, '%d %d' % (int(time_created * 1e6), os.getppid()))
            setproctitle('zygote-worker version=%s' % self.version)
            app = self.get_application(*self.args)
            #http_server = tornado.httpserver.HTTPServer(app, io_loop=io_loop, no_keep_alive=True)
            # TODO: make keep-alive servers work
            http_server = HTTPServer(app, io_loop=io_loop, no_keep_alive=True, close_callback=on_close, headers_callback=on_line)
            http_server._socket = self.sock
            io_loop.add_handler(self.sock.fileno(), http_server._handle_events, io_loop.READ)
            io_loop.start()
