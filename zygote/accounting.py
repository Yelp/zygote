import datetime
import errno
import os
import signal
import socket

from zygote import message
from zygote.util import meminfo_fmt


class Worker(object):

    def __init__(self, pid):
        self.time_created = datetime.datetime.now()
        self.pid = pid
        self.vsz = ''
        self.rss = ''
        self.shr = ''
        self.request_count = 0
        self.http = None

    def update_meminfo(self):
        f = meminfo_fmt(self.pid)
        for k, v in meminfo_fmt(self.pid).iteritems():
            setattr(self, k, v)

    def __eq__(self, other_pid):
        return self.pid == other_pid

    def to_dict(self):
        return {'pid': self.pid, 'vsz': self.vsz, 'rss': self.rss, 'shr': self.shr, 'time_created': self.time_created}

    def request_exit(self):
        """Instruct this worker to exit"""
        os.kill(self.pid, signal.SIGTERM)

class Zygote(object):

    def __init__(self, pid, basepath):
        self.basepath = basepath
        self.pid = pid
        self.worker_map = {}
        self.time_created = datetime.datetime.now()
        self.vsz = ''
        self.rss = ''
        self.shr = ''

        # wait until the control_socket can be connected, since it might take a
        # moment before the forked child creates their socket. a better way to
        # do this would be to have the parent create the control_socket and then
        # the child inherits it through forking
        self.control_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM, 0)
        while True:
            try:
                self.control_socket.connect('\0zygote_%d' % self.pid)
                break
            except socket.error, e:
                if e.errno == errno.ECONNREFUSED:
                    continue
                else:
                    raise

    def update_meminfo(self):
        for k, v in meminfo_fmt(self.pid).iteritems():
            setattr(self, k, v)
        for worker in self.worker_map.itervalues():
            worker.update_meminfo()

    def workers(self):
        return self.worker_map.values()

    def add_worker(self, pid):
        worker = Worker(pid)
        self.worker_map[pid] = worker

    def remove_worker(self, pid):
        del self.worker_map[pid]

    def begin_http(self, pid, http):
        self.worker_map[pid].http = http

    def end_http(self, pid, http):
        self.worker_map[pid].http = None

    def idle_workers(self):
        return [w for w in self.worker_map.itervalues() if v.http is None]

    def request_spawn(self):
        """Instruct this zygote to spawn a new worker"""
        self.control_socket.send(message.MessageCreateWorker.emit(''))

    def to_dict(self):
        return {
            'basepath': self.basepath,
            'pid': self.pid,
            'workers': self.worker_map.values(),
            'vsz': self.vsz,
            'rss': self.rss,
            'shr': self.shr,
            'time_created': self.time_created
            }

class ZygoteCollection(object):

    def __init__(self):
        self.zygote_map = {}

    def add_zygote(self, pid, basepath):
        z = Zygote(pid, basepath)
        self.zygote_map[pid] = z
        return z

    def update_meminfo(self):
        for z in self.zygote_map.values():
            z.update_meminfo()

    def remove_zygote(self, pid):
        del self.zygote_map[pid]

    def basepath_to_zygote(self, basepath):
        for zygote in self.zygote_map.itervalues():
            if zygote.basepath == basepath:
                return zygote
        return None

    def __getitem__(self, pid):
        return self.zygote_map[pid]

    def to_dict(self):
        return {'zygotes': self.zygote_map.values()}
