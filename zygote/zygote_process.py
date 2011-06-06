import logging
import os
import select
import signal
import sys

import tornado.ioloop
import tornado.httpserver

from .util import is_eintr, setproctitle
from .message import Message

class Zygote(object):
    """A Zygote is a process that manages children worker processes.
    
    When the zygote process is instantiated it does a few things:
     * chdirs to the absolute position pointed by a basepath symlink
     * munges sys.path to point to the new version of the code
     * imports the target module, to pre-fork load resources
     * creates read and write pipes to the parent process
    
    After this point, the zygote sits in a read/write loop select(2), waiting
    for commands sent by the parent process over the pipe that was
    established. The protocol is very simple, all commands sent by the parent
    process are exactly one character:
     'c' --> create new child
     'k' --> kill a child
     'r' --> send child report over the write pipe
     'x' --> kill all children and exit
    
    Likewise, the zygote process has a simple protocol it uses to send reporting
    information to the parent process. The format of all of these commands are a
    control character, data, and then a ! to indicate that the message is
    done. The types of data that can be sent back:

     'c' numeric_pid '!' --> a new child with the given pid was created
     'k' numeric_pid '!' --> a child with the given pid was killed
     'r' numeric_pids '!' --> the pids listed are currently children, separated by commas
    
    So an example message 'c1005!' means that a worker was spawned, and its pid
    was 1005. The example message 'r1008!' means that there is exactly one
    worker and its pid is 1008, and the example message 'r1008,1009!' means that
    there are exactly two workers and their pids are 1008 and 1009.

    Additionally, when the zygote exits it will implicitly close its write pipe,
    signaling to the parent that it is dead.
    """

    log = logging.getLogger('zygote.process')

    def __init__(self, sock, basepath, module, read_fd, write_fd):
        self.version = basepath.split('/')[-1]
        setproctitle('[zygote version=%s]' % (self.version,))

        os.chdir(basepath)
        sys.path.insert(0, basepath)
        t = __import__(module, [], [], ['get_application'], 0)

        self.sock = sock
        self.write_buffer = ''

        # the list of children pids actively running
        self.children = []

        # the list of pids for children that are expected to exit
        self.terminating = []

        self.get_application = t.get_application
        self.read_fd = read_fd
        self.write_fd = write_fd
        self.keep_running = True

        signal.signal(signal.SIGCHLD, self.reap_child)

        def zygote_exit(signum, frame):
            sys.exit(0)
        signal.signal(signal.SIGINT, zygote_exit)
        signal.signal(signal.SIGTERM, zygote_exit)

        self.log.debug('new zygote started')

    def write(self, control_character, msg):
        self.write_buffer += '%s%s!' % (control_character, msg)

    def reap_child(self, signum, frame):
        assert signum == signal.SIGCHLD
        while True:
            pid, status = os.waitpid(0, os.WNOHANG)
            if pid == 0:
                break

            self.log.info('reaped child %d' % (pid,))
            self.write(Message.CHILD_DIED, str(pid))

            assert pid in (self.children + self.terminating)

            if pid in self.children:
                self.children.remove(pid)
                if self.keep_running:
                    # the child accidentally died, respawn it
                    self.spawn_child()
            elif pid in self.terminating:
                self.terminating.remove(pid)
            else:
                assert False # not reached

        if not self.keep_running and not (self.children or self.terminating):
            # keep_running is False, and all children have been reaped, so exit
            sys.exit(0)

    def loop(self):
        while True:
            read_fds = [self.read_fd]
            write_fds = [self.write_fd] if self.write_buffer else []
            try:
                reads, writes, _ = select.select(read_fds, write_fds, [])
            except select.error, e:
                if is_eintr(e):
                    continue
                else:
                    raise
            if reads:
                read_data = os.read(self.read_fd, 512)
                for c in read_data:
                    if c == Message.SPAWN_CHILD:
                        self.spawn_child()
                    elif c == Message.KILL_CHILD:
                        self.kill_child()
                    elif c == Message.REPORT:
                        self.report()
                    elif c == Message.EXIT:
                        self.exit()
            if writes:
                bytes = os.write(self.write_fd, self.write_buffer)
                self.write_buffer = self.write_buffer[bytes:]

    def spawn_child(self):
        assert self.keep_running
        pid = os.fork()
        if pid:
            self.log.info('spawned child %d' % (pid,))
            self.children.append(pid)
            self.write(Message.CHILD_CREATED, str(pid))
        else:
            setproctitle('zygote-worker version=%s' % self.version)
            io_loop = tornado.ioloop.IOLoop()
            app = self.get_application()
            http_server = tornado.httpserver.HTTPServer(app, io_loop=io_loop)
            io_loop.add_handler(self.sock, http_server._handle_events, io_loop.READ)
            io_loop.start()

    def kill_child(self):
        assert self.keep_running
        if not self.children:
            return
        c = self.children.pop(0)
        os.kill(c, signal.SIGTERM)

    def report(self):
        self.write(Message.REPORT, ','.join(str(pid) for pid in self.children))

    def exit(self):
        self.keep_running = False
        for c in self.children:
            os.kill(c, signal.SIGTERM)
