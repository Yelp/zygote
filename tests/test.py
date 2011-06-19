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
stat_re = re.compile(r'^\d+ \(.*\) [A-Z] (\d+) \d+ \d+ \d+ -?\d+ \d+ \d+ (\d+) \d+ \d+.*')

class ZygoteTest(TestCase):

    __test__ = False

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

    @class_setup
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

        with open(os.devnull, 'w') as devnull:
            self.proc = subprocess.Popen(['python', 'zygote/main.py',
                                          '-d',
                                          '-b', self.basedir,
                                          '-p', str(self.port),
                                          '--control-port', str(self.control_port),
                                          '--num-workers', str(self.num_workers),
                                          'example'],
                                         env=env,
                                         stdout=devnull,
                                         stderr=devnull)
                                         #stdout=sys.stdout,
                                         #stderr=sys.stderr)

    @class_setup
    def sanity_check_process(self):
        """Ensure the process didn't crash immediately"""
        assert_equals(self.proc.returncode, None)
        time.sleep(1)

    @class_setup
    def check_process_tree(self):
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
                m = stat_re.match(data).groups()
                ppid = int(m[0])
            except AttributeError:
                print repr(data)
                sys.exit(1)
            pid_map.setdefault(ppid, []).append(pid)

        self.processes = set([self.proc.pid])
        for zygote_pid in pid_map[self.proc.pid]:
            self.processes.add(zygote_pid)
            for child in pid_map.get(zygote_pid, []):
                self.processes.add(child)

        # there should be one master process, one worker process, and num_workers workers
        assert_equal(len(self.processes), self.num_workers + 2)

    @class_teardown
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

class ZygoteTests(ZygoteTest):

    def test_http_get(self):
        for x in xrange(self.num_workers + 1):
            resp = self.get_url('/')
            self.check_response(resp)
            assert resp.body.startswith('uptime: ')

if __name__ == '__main__':
    main()
