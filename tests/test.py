import errno
import random
import re
import signal
import socket
import subprocess
import sys
import time
import os

import tornado.simple_httpclient
from tornado.httpclient import HTTPRequest, HTTPClient

from testify import *

num_re = re.compile(r'\d+$')
stat_re = re.compile(r'^(?P<pid>\d+) \((?P<exename>[^)]+)\) (?P<state>[A-Z]) (?P<ppid>\d+) (?P<pgid>\d+) (?P<sid>\d+) (?P<tty_nr>\d+) (?P<tpgid>-?\d+) (?P<flags>\d+) \d+ (\d+) \d+ \d+.*')

class ZygoteTest(TestCase):

    __test__ = False

    USE_DEVNULL = True

    basedir = './example'
    control_port = None
    port = None
    num_workers = 4

    def get_url(self, path):
        req = HTTPRequest('http://localhost:%d%s' % (self.port, path))
        try:
            response = self.http_client.fetch(req)
        except socket.error, e:
            if e.errno == errno.ECONNREFUSED:
                assert False, 'socket was not connected'
            raise
        #if not self.http_client._io_loop._stopped:
        #    self.http_client._io_loop.stop()
        return response

    def check_response(self, resp, code=200):
        assert_equals(resp.code, code)

    @setup
    def create_http_client(self):
        self.http_client = HTTPClient()
        if not isinstance(self.http_client._async_client, tornado.simple_httpclient.SimpleAsyncHTTPClient):
            self.http_client._async_client = tornado.simple_httpclient.SimpleAsyncHTTPClient(client._io_loop)

    @class_setup
    def choose_ports(self):
        if self.port is None:
            self.port = random.randrange(29000, 30000)
        if self.control_port is None:
            self.control_port = random.randrange(5000, 6000)

    @setup
    def create_process(self):
        env = os.environ.copy()
        #zygote_path = os.path.join(os.getcwd(), 'zygote')
        zygote_path = os.getcwd()
        if not env.get('PYTHONPATH'):
            env['PYTHONPATH'] = zygote_path
        else:
            parts = env['PYTHONPATH'].split(':')
            if parts[0] != zygote_path:
                env['PYTHONPATH'] = zygote_path + ':' + env['PYTHONPATH']

        kw = {'env': env}
        if self.USE_DEVNULL:
            devnull = open(os.devnull, 'w')
            kw['stdout'] = kw['stderr'] = devnull
        else:
            kw['stdout'] = sys.stdout
            kw['stderr'] = sys.stderr

        self.proc = subprocess.Popen(['python', 'zygote/main.py',
                                          '-d',
                                          '-b', self.basedir,
                                          '-p', str(self.port),
                                          '--control-port', str(self.control_port),
                                          '--num-workers', str(self.num_workers),
                                          '-m', 'example'], **kw)

    @setup
    def sanity_check_process(self):
        """Ensure the process didn't crash immediately"""
        assert_equals(self.proc.returncode, None)
        time.sleep(1)

    def get_process_tree(self):
        pid_map = {}
        for potential_pid in os.listdir('/proc'):
            if not num_re.match(potential_pid):
                continue
            pid = int(potential_pid)
            try:
                with open('/proc/%d/stat' % pid) as stat_file:
                    data = stat_file.read().strip()
            except IOError:
                continue
            try:
                m = stat_re.match(data)
                ppid = int(m.group('ppid'))
            except AttributeError:
                print >>sys.stderr, "Error reading /proc/%d/stat: %s" % (pid, data)
                raise
            pid_map.setdefault(pid, [])
            pid_map.setdefault(ppid, []).append(pid)
        return pid_map

    @setup
    def check_process_tree(self):
        pid_map = self.get_process_tree()
        self.processes = set([self.proc.pid])
        for zygote_pid in pid_map[self.proc.pid]:
            self.processes.add(zygote_pid)
            for child in pid_map.get(zygote_pid, []):
                self.processes.add(child)

        # there should be one master process, one worker process, and num_workers workers
        assert_equal(len(self.processes), self.num_workers + 2)

    @teardown
    def remove_process(self):
        self.proc.send_signal(signal.SIGTERM)
        assert_equals(self.proc.wait(), 0)

        # make sure all of the processes in the process tree terminated
        for pid in self.processes:
            try:
                os.kill(pid, 0)
            except OSError, e:
                if e.errno == errno.ESRCH:
                    continue

            assert False, 'pid %d still alive' % (pid,)

        self.assert_(not self.is_port_connected(self.port))
        self.assert_(not self.is_port_connected(self.control_port))

        self.removed = True

    def is_port_connected(self, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(('127.0.0.1', port))
        except socket.error, e:
            if e.errno == errno.ECONNREFUSED:
                return False
            raise
        s.close()
        return True

    def get_zygote(self, process_tree, num_expected=1):
        assert_equal(len(process_tree[self.proc.pid]), num_expected)
        return process_tree[self.proc.pid][0]

class ZygoteTests(ZygoteTest):

    def test_http_get(self):
        for x in xrange(self.num_workers + 1):
            resp = self.get_url('/')
            self.check_response(resp)
            assert resp.body.startswith('uptime: ')

    def test_kill_intermediate_zygote(self):
        pid_map = self.get_process_tree()
        zygote = self.get_zygote(pid_map)
        workers = pid_map[zygote]
        assert_equal(len(workers), self.num_workers)

        os.kill(zygote, signal.SIGKILL)
        time.sleep(1)

        new_pid_map = self.get_process_tree()
        for w in workers:
            try:
                os.kill(w, 0)
            except OSError, e:
                if e.errno == errno.ESRCH:
                    continue
                else:
                    raise
            assert False, 'worker pid %d was still alive' % (w,)
        assert_equal(len(new_pid_map[self.proc.pid]), 1)
        new_zygote = new_pid_map[self.proc.pid][0]
        assert_equal(len(new_pid_map[new_zygote]), self.num_workers)

    def test_hup(self):
        """Test sending SIGHUP to the master"""
        process_tree = self.get_process_tree()
        initial_zygote = self.get_zygote(process_tree)
        os.kill(self.proc.pid, signal.SIGHUP)
        time.sleep(1)

        process_tree = self.get_process_tree()
        final_zygote = self.get_zygote(process_tree)
        assert_not_equal(initial_zygote, final_zygote)

    def test_hup_intermediate(self):
        """Test sending SIGHUP to the zygote (this is an abnormal case!)"""
        process_tree = self.get_process_tree()
        initial_zygote = self.get_zygote(process_tree)

        # this should cause the intermediate to die, since it should not have a
        # SIGHUP handler
        os.kill(initial_zygote, signal.SIGHUP)
        time.sleep(1)

        process_tree = self.get_process_tree()
        final_zygote = self.get_zygote(process_tree)
        assert_not_equal(initial_zygote, final_zygote)

if __name__ == '__main__':
    main()
