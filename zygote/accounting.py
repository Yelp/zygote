import datetime
import os
import signal
import time

import zygote.util
from zygote import message
from zygote.util import meminfo_fmt

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

    def __init__(self, pid, basepath, io_loop):
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

        # wait until the control_socket can be connected, since it might take a
        # moment before the forked child creates their socket. a better way to
        # do this would be to have the parent create the control_socket and then
        # the child inherits it through forking
        self.control_socket = zygote.util.AFUnixSender(io_loop)
        self.control_socket.connect('\0zygote_%d' % self.pid)

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
        del self.worker_map[pid]

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
        self.control_socket.send(message.MessageCreateWorker.emit(''))

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
            'time_created': self.time_created
            }

class ZygoteCollection(object):

    def __init__(self):
        self.zygote_map = {}

    def add_zygote(self, pid, basepath, io_loop):
        z = Zygote(pid, basepath, io_loop)
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
        return self.zygote_map[pid]

    def __iter__(self):
        return self.zygote_map.itervalues()

    def other_zygotes(self, target):
        return [z for z in self.zygote_map.itervalues() if z != target]

    def to_dict(self):
        return {'zygotes': sorted(self.zygote_map.values(), key=lambda x: x.time_created)}

    def pids(self):
        return self.zygote_map.keys()
