import errno
import os
import resource

try:
    import setproctitle as _setproctitle
    has_proc_title = True
except ImportError:
    has_proc_title = False

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
    pid = pid or os.getpid()
    raw_file = None
    try:
        raw_file = open('/proc/%d/statm' % pid)
        data = raw_file.read().rstrip('\n')
        raw_file.close()
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
