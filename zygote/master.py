import fcntl
import logging
import os
import select
import socket
import sys

import tornado.ioloop
import tornado.httpserver

import zygote.util
from zygote.message import Message
from zygote.zygote_process import Zygote

class ZygoteMaster(object):

    log = logging.getLogger('zygote.master')

    def __init__(self, sock, basepath, module, num_workers):
        self.io_loop = tornado.ioloop.IOLoop()
        self.sock = sock
        self.basepath = basepath
        self.module = module
        self.num_workers = num_workers
        self.zygotes = []
        self.write_buffers = {}
        self.read_buffers = {}

    def create_zygote(self):
        # read the basepath symlink
        realbase = os.path.realpath(self.basepath)
        pid, write_fd = self.base_to_write_fd(realbase)
        if pid is not None:
            # a zygote for this basepath already exists, reuse that
            self.log.info('zygote for base %r already exists, reusing %d' % (realbase, pid))
            return write_fd
        else:
            # the basepath has changed, create a new zygote
            # TODO: it would actually be better to fork here (in the ZygoteMaster) so we could 

            read1, write1 = os.pipe()
            read2, write2 = os.pipe()

            pid = os.fork()
            if pid:
                self.log.info('started zygote %d pointed at base %r' % (pid, realbase))
                self.zygotes.append((realbase, pid, read2, write1))
                return write1
            else:
                # Try to clean up some of the file descriptors and whatnot that
                # exist in the parent before continuing. Strictly speaking, this
                # isn't necessary, but it seems good to remove these resources
                # if they're not needed in the child.
                del self.io_loop
                for _, _, r, w in self.zygotes:
                    os.close(r)
                    os.close(w)

                # create the zygote
                z = Zygote(self.sock, realbase, self.module, read1, write2)
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

    def read_fds(self):
        return [r for _, _, r, _ in self.zygotes]

    def write(self, fd, msg):
        self.write_buffers[fd] = self.write_buffers.get(fd, '') + msg

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
        w = self.create_zygote()
        self.write(w, Message.SPAWN_CHILD * self.num_workers)
        self.loop()

    def loop(self):
        while True:
            try:
                reads, writes, _ = select.select(self.read_fds(), self.write_buffers.keys(), [])
            except select.error, e:
                if zygote.util.is_eintr(e):
                    self.log.debug('ignoring EINTR')
                    continue
                else:
                    raise
            except KeyboardInterrupt:
                self.log.info('caught KeyboardInterrupt, exiting')
                break

            for read_fd in reads:
                msg = os.read(read_fd, 512)
                base, pid, write = self.read_fd_to_zygote(read_fd)
                if len(msg) == 0:
                    self.remove_zygote(read_fd, write)
                    continue

                for msg_control, msg_body in self.get_read_messages(pid, msg):
                    self.log.debug('got message %r from pid %d' % (msg_control + msg_body, pid))

            for write_fd in writes:
                if write_fd not in self.write_buffers:
                    # maybe the write_buffer was removed by remove_zygote in the
                    # read loop above
                    continue
                bytes = os.write(write_fd, self.write_buffers[write_fd])
                if bytes == len(self.write_buffers[write_fd]):
                    del self.write_buffers[write_fd]
                else:
                    self.write_buffs[write_fd] = self.write_buffers[write_fd][bytes:]


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

    master = ZygoteMaster(sock, opts.basepath, module, opts.num_workers)
    master.start()
