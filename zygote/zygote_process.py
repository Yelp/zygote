import errno
import logging
import os
import signal
import socket
import sys

import tornado.ioloop
import tornado.httpserver
from ._httpserver import HTTPServer

from .util import setproctitle
from .message import Message, MessageCreateWorker, MessageWorkerStart, MessageWorkerExit, MessageHTTPEnd, MessageHTTPBegin

log = logging.getLogger(__name__)


def establish_signal_handlers():
    def zygote_exit(signum, frame):
        sys.exit(0)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, zygote_exit)

class Zygote(object):
    """A Zygote is a process that manages children worker processes.

    When the zygote process is instantiated it does a few things:
     * chdirs to the absolute position pointed by a basepath symlink
     * munges sys.path to point to the new version of the code
     * imports the target module, to pre-fork load resources
     * creates read and write pipes to the parent process
    """

    RECV_SIZE = 8096

    def __init__(self, sock, basepath, module):
        self.version = basepath.split('/')[-1]
        setproctitle('[zygote version=%s]' % (self.version,))

        self.io_loop = tornado.ioloop.IOLoop()

        os.chdir(basepath)
        sys.path.insert(0, basepath)
        t = __import__(module, [], [], ['get_application'], 0)

        self.sock = sock

        self.get_application = t.get_application

        establish_signal_handlers()

        self.control_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM, 0)
        self.control_socket.bind('\0zygote_%d' % os.getpid())
        self.io_loop.add_handler(self.control_socket.fileno(), self.handle_control, self.io_loop.READ)

        self.notify_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM, 0)
        self.notify_socket.connect('\0zygote_%d' % os.getppid())

        signal.signal(signal.SIGCHLD, self.reap_child)

        log.debug('new zygote started')

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
            pid, status = os.waitpid(0, os.WNOHANG)
            if pid == 0:
                break

            status_code = os.WEXITSTATUS(status)
            log.info('reaped child %d, status %d' % (pid, status_code))
            self.notify(MessageWorkerExit.emit('%d %d' % (pid, status_code)))

    def notify(self, msg):
        self.notify_socket.send(msg)

    def loop(self):
        self.io_loop.start()

    def notify(self, msg_cls, body=''):
        try:
            self.notify_socket.send(msg_cls.emit(str(body)))
        except socket.error, e:
            if e.errno == errno.ENOTCONN:
                sys.exit(0)
            else:
                raise

    def spawn_worker(self):
        pid = os.fork()
        if not pid:
            establish_signal_handlers()
            def on_line(line):
                self.notify(MessageHTTPBegin, line)
            def on_close():
                self.notify(MessageHTTPEnd)

            self.notify(MessageWorkerStart, os.getppid())
            setproctitle('zygote-worker version=%s' % self.version)
            io_loop = tornado.ioloop.IOLoop()
            app = self.get_application()
            #http_server = tornado.httpserver.HTTPServer(app, io_loop=io_loop, no_keep_alive=True)
            http_server = HTTPServer(app, io_loop=io_loop, no_keep_alive=True, close_callback=on_close, headers_callback=on_line)
            http_server._socket = self.sock
            io_loop.add_handler(self.sock.fileno(), http_server._handle_events, io_loop.READ)
            io_loop.start()
