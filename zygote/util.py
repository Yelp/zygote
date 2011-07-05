from __future__ import with_statement

import errno
import gc
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
    if not os.path.exists('/proc/self/fd'):
        log.warn('no /proc fd information running, not closing fds')
        return
    gc.collect()
    return
    excl = list(exclude) + [sys.stdout.fileno(), sys.stderr.fileno()]
    for fd_name in os.listdir('/proc/self/fd'):
        fd = int(fd_name)
        if fd not in excl:
            try:
                retry_eintr(lambda: os.close(fd))
            except OSError, e:
                if e.errno == errno.EBADF:
                    # for some reason the fd was bad. nothing we can do about
                    # that
                    log.debug('file descriptor %d was bad, ignoring', fd)
                    pass
                else:
                    raise

def list_open_fds(exclude=[]):
    """This method exists to help debug file descriptor leaks; it shouldn't
    normally be called while running.
    """
    if not os.path.exists('/proc/self/fd'):
        log.warn('no /proc fd information running, not closing fds')
        return

    # ensure that any file descriptors closed by destructors have actually been
    # closed
    gc.collect()

    excl = list(exclude) + [sys.stdout.fileno(), sys.stderr.fileno()]
    for fd_name in os.listdir('/proc/self/fd'):
        fd = int(fd_name)
        if fd not in excl:
            log.info('fd %d open', fd)

def safe_kill(pid):
    try:
        log.debug('killing %d', pid)
        os.kill(pid, signal.SIGTERM)
    except OSError, e:
        log.debug('failed to safe_kill pid %d because of %r' % (pid, e))
        return False
    return True

class AFUnixSender(object):
    """Sender abstraction for an AF_UNIX socket (using the SOCK_DGRAM
    protocol). This handles connecting in a non-blocking fashion, and sending
    messages asynchronously. Messages that are scheduled to be sent before the
    socket is connected will be sent once the socket finishes connecting.
    """

    CONNECT_FREQUENCY = 0.1

    def __init__(self, io_loop, sock=None, logger=None):
        self.io_loop = io_loop
        if sock is None:
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM, 0)
        else:
            self.socket = sock
        self.io_loop._set_nonblocking(self.socket)
        self.log = logger or log

        self.connected = False # is the socket connected>
        self.send_queue = []   # queue of messages to send
        self.sending = False   # are there queued messages?

    def connect(self, target):
        try:
            self.socket.connect(target)
        except socket.error, e:
            if e.errno == errno.EINPROGRESS:
                # usual case -- the nonblocking connect causes EINPROGRESS. When
                # the socket is writeable, then the connect has finished, and we
                # call _finish_connecting
                self.io_loop.add_handler(self.socket.fileno(), self._finish_connecting, self.io_loop.WRITE)
            elif e.errno == errno.ECONNREFUSED:
                # the connection was refused. Retry the connection in
                # CONNECT_FREQUENCY seconds
                self.io_loop.add_timeout(time.time() + self.CONNECT_FREQUENCY, lambda: self.connect(target))
            else:
                raise
        else:
            # we were able to connect immediately
            self._finish_connecting()

    def _finish_connecting(self, fd=None, events=None):
        if fd is not None:
            assert fd == self.socket.fileno()
            self.io_loop.remove_handler(fd)
            error = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if error != 0:
                self.log.error('got socket connect error %r' % (error,))
                raise IOError('errno %d' % (error,))

        self.connected = True
        if self.send_queue:
            self._sendall()

    def _sendall(self):
        if not self.connected:
            return
        if not self.send_queue:
            self.log.error('got _sendall with no send_queue')
            return
        if self.sending:
            # could happen if we schedule multiple messages to be sent in a row
            self.log.debug('already in send loop, be patient')
            return

        def sender(fd, events):
            assert fd == self.socket.fileno()
            self.socket.send(self.send_queue.pop(0))
            if not self.send_queue:
                self.sending = False
                self.io_loop.remove_handler(fd)

        self.io_loop.add_handler(self.socket.fileno(), sender, self.io_loop.WRITE)
        self.sending = True

    def send(self, msg):
        self.send_queue.append(msg)
        self._sendall()
