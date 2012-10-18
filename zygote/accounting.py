import datetime
import os
import signal
import time

import zygote.util
from zygote import message
from zygote.util import meminfo_fmt

log = zygote.util.get_logger('zygote.accounting')

def format_millis(v):
    if v is not None:
        return '%1.1f' % (v * 1000.0)

class Worker(object):

    def __init__(self, pid, time_created=None):
        if time_created:
            self.time_created = datetime.datetime.fromtimestamp(time_created / 1e6)
        else:
            self.time_created = datetime.datetime.now()
        self.pid = pid
        self.vsz = ''
        self.rss = ''
        self.shr = ''
        self.remote_ip = None
        self.request_count = 0
        self.request_started = None
        self.http = None

    def update_meminfo(self):
        for k, v in meminfo_fmt(self.pid).iteritems():
            setattr(self, k, v)

    def __eq__(self, other_pid):
        return self.pid == other_pid

    def start_request(self, remote_ip, http):
        self.remote_ip = remote_ip
        self.request_started = time.time()
        self.request_count += 1
        self.http = http

    def end_request(self):
        self.remote_ip = None
        self.http = None
        self.request_started = None

    def to_dict(self):
        d = {'pid': self.pid,
             'vsz': self.vsz,
             'rss': self.rss,
             'shr': self.shr,
             'time_created': self.time_created,
             'remote_ip': self.remote_ip,
             'request_count': self.request_count,
             'http': self.http}
        if self.request_started is None:
            d['elapsed'] = None
            d['elapsed_formatted'] = None
        else:
            now = time.time()
            d['elapsed'] = now - self.request_started
            d['elapsed_formatted'] = format_millis(d['elapsed'])
        return d

    def request_exit(self):
        """Instruct this worker to exit"""
        os.kill(self.pid, signal.SIGTERM)

class Zygote(object):
    """Stub representing the zygote from the master side of the fork. Is not
    actually the zygote, but sends some commands over the unix domain socket
    to the zygote.

    TODO: Move parsing of messages *from* the unix domain socket into this object,
    and use a regular callback system
    """
    __generation = 0

    def __init__(self, pid, basepath, io_loop, canary=False):
        """Initialize using real Zygote's pid, basepath and master's
        io_loop.

        Master also marks the zygote as 'canary' at initialization if
        it's an update to a newer revision of the source. Only if the
        canary is live, master continues on transition workers.
        """
        self.basepath = basepath
        self.pid = pid
        self.worker_map = {}
        self.time_created = datetime.datetime.now()
        self.vsz = ''
        self.rss = ''
        self.shr = ''
        self.connected = False
        self.send_queue = []
        self.write_queue_active = False
        self.canary = canary

        # wait until the control_socket can be connected, since it might take a
        # moment before the forked child creates their socket. a better way to
        # do this would be to have the parent create the control_socket and then
        # the child inherits it through forking
        self.control_socket = zygote.util.AFUnixSender(io_loop)
        self.control_socket.connect('\0zygote_%d' % self.pid)

        self.shutting_down = False

        self.generation = self.__class__.__generation
        self.__class__.__generation += 1

    def update_meminfo(self):
        for k, v in meminfo_fmt(self.pid).iteritems():
            setattr(self, k, v)
        for worker in self.worker_map.itervalues():
            worker.update_meminfo()

    def workers(self):
        return self.worker_map.values()

    def add_worker(self, pid, time_created=None):
        worker = Worker(pid, time_created)
        self.worker_map[pid] = worker

    def remove_worker(self, pid):
        try:
            del self.worker_map[pid]
        except KeyError:
            log.warning("Tried to delete unknown worker %d (did worker initialization fail?)", pid)

    def begin_http(self, pid, http):
        self.worker_map[pid].http = http

    def end_http(self, pid, http):
        self.worker_map[pid].http = None

    def idle_workers(self):
        return [w for w in self.worker_map.itervalues() if w.http is None]

    def get_worker(self, pid):
        return self.worker_map.get(pid)

    def request_spawn(self):
        """Instruct this zygote to spawn a new worker"""
        log.debug('requesting spawn on Zygote %d', self.generation)
        self.control_socket.send(message.MessageCreateWorker.emit(''))

    def request_kill_workers(self, num_workers_to_kill):
        """Instruct this zygote to kill an idle worker"""
        self.control_socket.send(message.MessageKillWorkers.emit('%d' % num_workers_to_kill))

    def request_shut_down(self):
        """Instruct this zygote to shut down all workers"""
        self.control_socket.send(message.MessageShutDown.emit(""))
        self.shutting_down = True

    @property
    def worker_count(self):
        return len(self.worker_map)

    def to_dict(self):
        return {
            'basepath': self.basepath,
            'pid': self.pid,
            'workers': sorted(self.worker_map.values(), key=lambda x: x.time_created),
            'vsz': self.vsz,
            'rss': self.rss,
            'shr': self.shr,
            'time_created': self.time_created,
            'generation': self.generation,
            }

class ZygoteCollection(object):

    def __init__(self):
        self.zygote_map = {}

    def add_zygote(self, pid, basepath, io_loop, canary=False):
        z = Zygote(pid, basepath, io_loop, canary=canary)
        self.zygote_map[pid] = z
        return z

    def update_meminfo(self):
        for z in self.zygote_map.values():
            z.update_meminfo()

    def remove_zygote(self, pid):
        del self.zygote_map[pid]

    def get_worker(self, pid):
        for zygote in self.zygote_map.itervalues():
            w = zygote.get_worker(pid)
            if w:
                return w
        return None

    def basepath_to_zygote(self, basepath):
        for zygote in self.zygote_map.itervalues():
            if zygote.basepath == basepath:
                return zygote
        return None

    def __getitem__(self, pid):
        # ZygoteMaster requests a zygote using it's pid when it
        # recieves a message from the zygote. In certain cases,
        # ZygoteMaster can request a zygote that is already
        # removed. This can happen if zygote dies and master handles
        # the signal accordingly before reading the message from
        # socket.
        return self.zygote_map.get(pid, None)

    def __iter__(self):
        return self.zygote_map.itervalues()

    def other_zygotes(self, target):
        return [z for z in self.zygote_map.itervalues() if z != target]

    def to_dict(self):
        return {'zygotes': sorted(self.zygote_map.values(), key=lambda x: x.time_created)}

    def pids(self):
        return self.zygote_map.keys()

    def worker_count(self):
        """Return the total number of workers"""
        return sum(len(z.workers()) for z in self.zygote_map.itervalues())
