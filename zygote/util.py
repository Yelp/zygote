# -*- coding: utf-8 -*-
from __future__ import with_statement

import errno
import fcntl
import functools
import logging
import os
import resource
import signal
import socket
import sys
import time

import tornado.ioloop

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

def set_nonblocking(fd):
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

def get_rundir():
    return os.getenv('RUNDIR') if os.getenv('RUNDIR') else os.getcwd()

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
            except OSError, e:
                if e.errno == errno.EBADF:
                    # for some reason the fd was bad. nothing we can do about
                    # that
                    pass
                else:
                    raise

def is_pid_alive(pid):
    """Sends null signal to a process to check if it's alive"""
    try:
        os.kill(pid, 0)
        return True
    except OSError, e:
        # Access denied, but process is alive
        return e.errno == errno.EPERM
    except:
        return False

def safe_kill(pid, sig=signal.SIGUSR1, process_group=False):
    try:
        log.debug('killing %d', pid)
        if process_group:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except OSError, e:
        # Process may have died before we send the signal
        if not is_pid_alive(pid):
            return True
        log.warning('failed to safe_kill pid %d because of %r' % (pid, e))
        return False
    return True

def wait_for_pids(pids, timeout, log, kill_pgroup=False):
    """Wait for the given Set of pids to die. If they
    haven't died after timeout seconds, send them all SIGKILL.
    
    If kill_pgroup is true, the kill will be sent to
    the process group instead of to the process itself.
    """
    start = time.time()
    elapsed = 0
    while elapsed < timeout:
        if not pids:
            break
        pid, _, _ = os.wait3(os.WNOHANG)
        if pid == 0:
            time.sleep(0.5)
        else:
            pids.remove(pid)
        elapsed = time.time() - start
    else:
        # will only get here if we ran out of time
        log.warning("PIDs [%s] didn't quit after %f seconds, sending SIGKILL", ",".join(str(p) for p in pids), timeout)
        for pid in pids:
            safe_kill(pid, signal.SIGKILL, kill_pgroup)

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
        set_nonblocking(self.socket)
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

        def maybe_send_queue(fd, _):
            """Try to send the message queue. Returns True if the entire
            queue was sent"""
            assert fd == self.socket.fileno()
            while self.send_queue:
                self.sending = True
                try:
                    self.socket.send(self.send_queue.pop(0))
                except IOError, e:
                    if e.errno == errno.EWOULDBLOCK:
                        self.log.debug("got EWOULDBLOCK")
                        return False
                    elif e.errno in (errno.ECONNREFUSED, errno.ENOTCONN):
                        # If the worker has already shut down, and needs to be reaped,
                        # then we'll get back an ENOTCONN or an ECONNREFUSED from this
                        # send call. We should be reaping this process shortly hereafter
                        # anyway, so just ignore it
                        return False
                    else:
                        self.sending = False
                        raise
                except IndexError:
                    # We'll get the IndexError when we've sent the entire
                    # send_queue; if this ever gets multithreaded, we might
                    # get an IndexError instead of breaking out of the loop
                    # naturally, so gotta handle that!
                    break
                except Exception, e:
                    # Sometimes we seem to get errors that are not IOErrors?
                    if getattr(e, 'errno', None) not in (errno.ECONNREFUSED, errno.ENOTCONN):
                        raise
                    return False
            self.sending = False
            return True

        def sender(fd, *args, **kwargs):
            success = maybe_send_queue(fd, [])
            if success:
                self.io_loop.remove_handler(fd)

        # Try and send immediately
        success = maybe_send_queue(self.socket.fileno(), [])
        # if that fails, put it in the ioloop to send later
        if not success:
            self.io_loop.add_handler(self.socket.fileno(), sender, self.io_loop.WRITE)

    def send(self, msg):
        self.send_queue.append(msg)
        self._sendall()

class ZygoteIOLoop(tornado.ioloop.IOLoop):
    """Override IOLoop to log to our own logger instead of the root logger"""

    def __init__(self, *args, **kwargs):
        log_name = kwargs.pop('log_name', 'zygote.io_loop')
        self.log = logging.getLogger(log_name)
        super(ZygoteIOLoop, self).__init__(*args, **kwargs)

    def handle_callback_exception(self, callback):
        self.log.exception("Error in callback %s", callback)

    def add_handler(self, fd, handler, events):
        """Add a handler to the IOLoop, with exception handling"""

        # Get the list of available attributes from the handler.
        # A workaround for: http://bugs.python.org/issue3445
        available_attrs = (a for a in functools.WRAPPER_ASSIGNMENTS if hasattr(handler, a))

        @functools.wraps(handler, assigned=available_attrs)
        def wrapped_handler(*args, **kwargs):
            try:
                handler(*args, **kwargs)
            except Exception:
                self.handle_callback_exception(handler)

        return super(ZygoteIOLoop, self).add_handler(fd, wrapped_handler, events)



if hasattr(logging, 'NullHandler'):
    NullHandler = logging.NullHandler
else:
    class NullHandler(logging.Handler):
        def emit(self, record):
            pass

# TODO: support logging to things other than stderr
#
# Why do we need this? Because we can not log in our signal handlers
# (http://docs.python.org/library/logging.html#thread-safety).
#
# This OTOH may cause log lines from master & worker processes merge
# into a single line. Happens very rarely and it's still better than
# master stuck in a deadlock.
class LocklessHandler(logging.StreamHandler):
    def createLock(self):
        self.lock = None

def get_logger(logger_name, debug=False):
    logger = logging.getLogger(logger_name)
    if os.isatty(sys.stderr.fileno()):
        formatter = logging.Formatter('[%(process)d] %(asctime)s :: %(levelname)-7s :: %(name)s - %(message)s')
        handler = LocklessHandler()
        handler.setFormatter(formatter)
        handler.setLevel(logging.DEBUG if debug else logging.INFO)
        logger.handlers = [handler]
        logger.propagate = False
    else:
        logger.handlers = [NullHandler()]
    return logger


def sanitize_headers(headers):
    """Sanitize sensitive request headers for logging"""
    results = dict(headers)
    # Redact instead of remove Authorization header so that those
    # using Basic Auth can debug if needed
    if results.get('Authorization'):
        results['Authorization'] = '***redacted***'
    return results
