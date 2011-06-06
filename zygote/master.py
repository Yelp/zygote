import datetime
import fcntl
import logging
import os
import select
import socket
import sys

import tornado.ioloop
import tornado.httpserver
import tornado.web

import zygote.util
from zygote.message import Message
from zygote.zygote_process import Zygote

def meminfo(pid=None):
    d = zygote.util.get_meminfo(pid)
    return {
        'rss': '%1.2f' % (d['res'] / 1024.0),
        'vsz': '%1.2f' % (d['virt'] / 1024.0),
        'shr': '%1.2f' % (d['shr'] / 1024.0)
        }

class HTMLHandler(tornado.web.RequestHandler):

    def get(self):
        env = {'title': 'zygote'}
        env['basepath'] = self.zygote_master.basepath
        env['zygotes'] = self.zygote_master.zygote_statistics
        env['start_time'] = self.zygote_master.time_created.strftime('%Y-%m-%d %H:%M:%S')
        env['time_now'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        env.update(meminfo())

        # update the meminfo data
        for pid, d in env['zygotes'].iteritems():
            d.update(meminfo(pid))
            for c in d.get('children', []):
                m = meminfo(c.pid)
                for k, v in m.items():
                    setattr(c, k, v)

        self.render('control.html', **env)

class Child(object):

    __slots__ = ['created', 'pid', 'vsz', 'rss', 'shr']

    def __init__(self, pid):
        self.pid = pid
        self.created = datetime.datetime.now()
        self.vsz = ''
        self.rss = ''
        self.shr = ''

    def __eq__(self, other_pid):
        return self.pid == other_pid

class ChildList(object):

    def __init__(self):
        self.pids = []

    def clear(self):
        del self.pids[:]

    def add_pid(self, pid):
        self.pids.append(Child(pid))

    def remove_pid(self, pid):
        self.pids.remove(pid)

    def __iter__(self):
        return iter(self.pids)

class ZygoteMaster(object):

    log = logging.getLogger('zygote.master')
    instantiated = False

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
        self.zygotes = []
        self.zygote_statistics = {} # map of pid to various stats
        self.write_buffers = {}
        self.read_buffers = {}
        self.time_created = datetime.datetime.now()

        HTMLHandler.zygote_master = self
        print os.path.realpath('static')
        app = tornado.web.Application([('/', HTMLHandler)],
                                      debug=False,
                                      static_path=os.path.realpath('static'),
                                      template_path=os.path.realpath('templates'))
        http_server = tornado.httpserver.HTTPServer(app, io_loop=self.io_loop)
        http_server.listen(control_port)

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
                self.zygote_statistics[pid] = {'children': ChildList(), 'time_created': datetime.datetime.now(), 'basepath': realbase}
                self.io_loop.add_handler(read2, self.handle_read, self.io_loop.READ)
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
        w = self.create_zygote()
        self.write(w, Message.SPAWN_CHILD * self.num_workers)
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
                self.zygote_statistics[pid]['children'].add_pid(child_pid)
            elif msg_control == Message.CHILD_DIED:
                child_pid = int(msg_body)
                self.zygote_statistics[pid]['children'].remove_pid(child_pid)
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
