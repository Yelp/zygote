import datetime
import fcntl
import logging
import os
import socket
import sys

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

    def __init__(self, sock, basepath, module, num_workers, control_port):
        if self.__class__.instantiated:
            self.log.error('cannot instantiate zygote master more than once')
            sys.exit(1)
        self.__class__.instantiated = True

        self.io_loop = tornado.ioloop.IOLoop()
        self.sock = sock
        self.basepath = basepath
        self.module = module
        self.num_workers = num_workers
        self.time_created = datetime.datetime.now()

        self.zygote_collection = accounting.ZygoteCollection()

        # create an abstract unix domain socket. this socket will be used to
        # receive messages from zygotes and their children
        self.domain_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM, 0)
        self.domain_socket.bind('\0zygote_%d' % os.getpid())
        self.io_loop.add_handler(self.domain_socket.fileno(), self.recv_protol_msg, self.io_loop.READ)

        zygote.handlers.get_httpserver(self.io_loop, control_port, self)

    def recv_protol_msg(self, fd, events):
        assert fd == self.domain_socket.fileno()
        data = self.domain_socket.recv(self.RECV_SIZE)
        msg = message.Message.parse(data)
        msg_type = type(msg)
        if msg_type is message.MessageWorkerStart:
            self.zygote_collection[msg.worker_ppid].add_worker(msg.pid)
        elif msg_type is message.MessageWorkerExit:
            zygote = self.zygote_collection[msg.pid]
            zygote.remove_worker(msg.child_pid)
            zygote.request_spawn() # XXX: unconditionally request a respawn
        elif msg_type is message.MessageHTTPBegin:
            self.register

    def create_zygote(self):
        """"Create a new zygote"""
        # read the basepath symlink
        realbase = os.path.realpath(self.basepath)
        z = self.zygote_collection.basepath_to_zygote(realbase)
        if z is not None:
            # a zygote for this basepath already exists, reuse that
            self.log.info('zygote for base %r already exists, reusing %d' % (realbase, z.pid))
            return z
        else:
            # the basepath has changed, create a new zygote
            pid = os.fork()
            if pid:
                self.log.info('started zygote %d pointed at base %r' % (pid, realbase))
                return self.zygote_collection.add_zygote(pid, realbase)
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

    def remove_zygote(self, read_fd, write_fd):
        for z in self.zygotes:
            _, pid, r, w = z
            if read_fd == r or write_fd == w:
                os.close(r)
                os.close(w)
                del self.write_buffers[w]
                del self.write_buffers[pid]
                break
        else:
            assert False, 'could not find that zygote'

        self.zygotes.remove(z)

    def write(self, fd, msg):
        self.write_buffers[fd] = self.write_buffers.get(fd, '') + msg
        self.io_loop.add_handler(fd, self.handle_write, self.io_loop.WRITE)

    def read_fd_to_zygote(self, r):
        for base, pid, read, write in self.zygotes:
            if read == r:
                return base, pid, write
        assert False

    def base_to_write_fd(self, base):
        for b, pid, _, write_fd in self.zygotes:
            if b == base:
                return pid, write_fd
        return None, None

    def get_read_messages(self, pid, msg):
        """Add in the currently read buffer from a pid, and yield out complete
        messages like ('c', '1984'). This takes care of all read buffering.
        """
        current_msg = self.read_buffers.get(pid, '') + msg
        while current_msg:
            pos = current_msg.find('!')
            if pos < 0:
                break

            msg = current_msg[:pos]
            yield msg[0], msg[1:]
            current_msg = current_msg[pos + 1:]

        if current_msg:
            self.read_buffers[pid] = current_msg
        elif pid in self.read_buffers:
            del self.read_buffers[pid]

    def start(self):
        z = self.create_zygote()
        for x in xrange(self.num_workers):
            z.request_spawn()
        self.io_loop.start()

    def handle_read(self, fd, events):
        msg = os.read(fd, 512)
        base, pid, write = self.read_fd_to_zygote(fd)
        if len(msg) == 0:
            self.remove_zygote(fd, write)
            os.close(fd)
            self.io_loop.remove_handler(fd)
            return

        for msg_control, msg_body in self.get_read_messages(pid, msg):
            if msg_control == Message.CHILD_CREATED:
                child_pid = int(msg_body)
                self.zygote_statistics.add_child(pid, child_pid)
            elif msg_control == Message.CHILD_DIED:
                child_pid = int(msg_body)
                self.zygote_statistics.remove_child(pid, child_pid)
            self.log.debug('got message %r from pid %d' % (msg_control + msg_body, pid))

    def handle_write(self, fd, events):
        if fd not in self.write_buffers:
            # maybe the write_buffer was removed by remove_zygote in the
            # read loop above
            return
        bytes = os.write(fd, self.write_buffers[fd])
        if bytes == len(self.write_buffers[fd]):
            del self.write_buffers[fd]
            self.io_loop.remove_handler(fd)
        else:
            self.write_buffers[fd] = self.write_buffers[fd][bytes:]

def main(opts, module):
    zygote.util.setproctitle('[zygote master %s]' % (module,))

    # Initialize the logging module
    log = logging.getLogger('zygote')
    formatter = logging.Formatter('[%(process)d] %(asctime)s :: %(levelname)-7s :: %(name)s - %(message)s')

    if os.isatty(sys.stderr.fileno()):
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG if opts.debug else logging.INFO)
        console_handler.setFormatter(formatter)
        logging.root.addHandler(console_handler)
        #log.addHandler(console_handler)

    if opts.debug:
        logging.root.setLevel(logging.DEBUG)
    else:
        logging.root.setLevel(logging.INFO)
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

    master = ZygoteMaster(sock, opts.basepath, module, opts.num_workers, opts.control_port)
    master.start()
