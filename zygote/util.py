from __future__ import with_statement

import errno
import logging
import os
import resource
import signal
import socket
import sys
import time

try:
    import setproctitle as _setproctitle
    has_proc_title = True
except ImportError:
    has_proc_title = False

log = logging.getLogger('zygote.util')

def setproctitle(name):
    if has_proc_title:
        _setproctitle.setproctitle(name)

def is_eintr(exc):
    """Returns True if an exception is an EINTR, False otherwise."""
    if hasattr(exc, 'errno'):
        return exc.errno == errno.EINTR
    elif getattr(exc, 'args', None) and hasattr(exc, 'message'):
        return exc.args[0] == errno.EINTR
    return False


def get_meminfo(pid=None):
    """Get the memory statistics for the current process. Values are returned
    as kilobytes. The meanings of the fields are:
      virt -- virtual size
      res -- RSS size
      shr -- shared memory
      trs -- kilobytes from 'code' pages
      drs -- kilobytes from data/stack pages
      lrs -- kilobytes from library pages
      dt -- kilobytes from diry pages
    """
    try:
        with open('/proc/%d/statm' % (pid or os.getpid())) as raw_file:
            data = raw_file.read().rstrip('\n')
    except IOError:
        return dict()

    fields = ['virt', 'res', 'shr', 'trs', 'drs', 'lrs', 'dt']
    pagesize = resource.getpagesize()
    return dict((k, int(v) * pagesize >> 10) for k, v in zip(fields, data.split()))

def meminfo_fmt(pid=None):
    d = get_meminfo(pid)
    return {
        'rss': '%1.2f' % (d['res'] / 1024.0),
        'vsz': '%1.2f' % (d['virt'] / 1024.0),
        'shr': '%1.2f' % (d['shr'] / 1024.0)
        }

def retry_eintr(func, max_retries=5):
    """Retry a function on EINTR"""
    for x in xrange(max_retries):
        try:
            return func()
        except Exception, e:
            if not is_eintr(e) or x == max_retries - 1:
                raise

def close_fds(*exclude):
    """Try to close open file descriptors. This will probably only work on
    Linux, since it uses /proc/PID/fd to get information on what file
    descriptors to close.

    An alternative for non-Linux systems would be to just try to close random
    file descriptors (say, the first 16k), but it doesn't seem like it's really
    worth the trouble (and doing this is potentially slow).
    """
    return # XXX: fixme
    if not os.path.exists('/proc/self/fd'):
        log.warn('no /proc fd information running, not closing fds')
        return
    excl = list(exclude) + [sys.stdin.fileno(), sys.stdout.fileno(), sys.stderr.fileno()]
    for fd_name in os.listdir('/proc/self/fd'):
        fd = int(fd_name)
        if fd not in excl:
            try:
                retry_eintr(lambda: os.close(fd))
                print 'closed %d' % (fd,)
            except OSError, e:
                if e.errno == errno.EBADF:
                    # for some reason the fd was bad. nothing we can do about
                    # that
                    pass
                else:
                    raise

def safe_kill(pid):
    try:
        log.debug('killing %d', pid)
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True

class AFUnixSender(object):

    CONNECT_FREQUENCY = 0.1

    def __init__(self, io_loop, sock=None, logger=None):
        self.io_loop = io_loop
        if sock is None:
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM, 0)
        else:
            self.socket = sock
        self.io_loop._set_nonblocking(self.socket)
        self.log = logger or log

        self.connected = False
        self.send_queue = []
        self.sending = False

    def connect(self, target):
        try:
            self.socket.connect(target)
        except socket.error, e:
            if e.errno == errno.EINPROGRESS:
                self.io_loop.add_handler(self.socket.fileno(), self._finish_connecting, self.io_loop.WRITE)
            elif e.errno == errno.ECONNREFUSED:
                self.io_loop.add_timeout(time.time() + self.CONNECT_FREQUENCY, lambda: self.connect(target))
            else:
                raise
        else:
            self.connected = True
            self._sendall()

    def _finish_connecting(self):
        error = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if error == 0:
            self.connected = True
            if self.send_queue:
                self._sendall()
        else:
            self.log.error('got socket connect error %r' % (error,))
            sys.exit(1)

    def _sendall(self):
        if not (self.connected and self.send_queue):
            return
        if self.sending:
            return

        def sender(fd, events):
            assert fd == self.socket.fileno()
            self.socket.send(self.send_queue.pop(0))
            if not self.send_queue:
                self.sending = False
                self.io_loop.remove_handler(fd)

        self.io_loop.add_handler(self.socket.fileno(), sender, self.io_loop.WRITE)

    def send(self, msg):
        self.send_queue.append(msg)
        self._sendall()
