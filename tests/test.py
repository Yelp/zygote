from contextlib import contextmanager
import errno
import random
import signal
import socket
import subprocess
import sys
import time
import os

import tornado.simple_httpclient
from tornado.httpclient import HTTPRequest, HTTPClient, AsyncHTTPClient

from testify import *

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

    @class_teardown
    def remove_process(self):
        self.proc.send_signal(signal.SIGTERM)
        assert_equals(self.proc.wait(), 0)

class ZygoteTests(ZygoteTest):

    def test_http_get(self):
        for x in xrange(self.num_workers + 1):
            resp = self.get_url('/')
            self.check_response(resp)
            assert resp.body.startswith('uptime: ')

if __name__ == '__main__':
    main()
