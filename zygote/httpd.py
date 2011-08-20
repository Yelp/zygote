import errno
import fcntl
import functools
import logging
import socket
import time

from tornado import stack_context
from tornado import ioloop
from tornado import iostream

import urlparse
try:
    from urlparse import parse_qs
except ImportError:
    from cgi import parse_qs

#from .httpd_util import HTTPBody, HTTPHeaders
from zygote.httpd_util import HTTPBody, HTTPHeaders

def wrap_stack(func, *args):
    if args:
        return stack_context.wrap(functools.partial(func, *args))
    else:
        return stack_context.wrap(func)

class StreamState(object):

    def __init__(self, address, stream):
        self.address = address
        self.stream = stream
        self.request_line = None
        self.request_started = time.time()
        self.response_started = False
        self.response_status = None
        self.response_headers = []
        self.finished_writing = False

    def flush_response(self):
        assert self.response_started == False
        resp = 'HTTP/1.1 ' + self.response_status.strip() + '\r\n'
        resp += '\r\n'.join('%s: %s' % h for h in self.response_headers) + '\r\n\r\n'
        self.stream.write(resp)
        self.response_started = True

    def _maybe_close(self):
        if self.finished_writing:
            self.stream.close()

    def write(self, data):
        if not self.response_started:
            self.flush_response()
        self.stream.write(data, self._maybe_close)

class HTTPServer(object):
    """This is a simple HTTP/WSGI gateway. It's based loosely on the Tornado
    HTTP server. This isn't complete yet; there are a bunch of zygote bits that
    are going to be glued in Real Soon.

    You are allowed to directly call handle_request() if you have a socket to a
    client. That means you can do your port 80 binding elsewhere, if it suits
    you.
    """

    def __init__(self, request_callback, keep_alive=True, xheaders=True, io_loop=None, http_socket=None):
        self.request_callback = request_callback
        self.keep_alive = keep_alive
        self.xheaders = xheaders
        self.io_loop = io_loop or ioloop.IOLoop.instance()
        self._socket = http_socket
        self._started = False
        self._streams = {}

    def bind(self, port, address=''):
        assert not self._started
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        flags = fcntl.fcntl(self._socket.fileno(), fcntl.F_GETFD)
        flags |= fcntl.FD_CLOEXEC
        fcntl.fcntl(self._socket.fileno(), fcntl.F_SETFD, flags)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.setblocking(0)
        self._socket.bind((address, port))
        self._socket.listen(128)

    def start(self):
        assert self._socket
        self._started = True
        self.io_loop.add_handler(self._socket.fileno(), self._handle_events, ioloop.IOLoop.READ)

    def _close_stream(self, address, stream=None):
        if stream is None:
            stream = self._streams[address]
        pass

    def _respond_error(self, stream, code, name, body=None):
        stream.write('HTTP/1.1 %d %s\r\n' % (code, name))
        if body:
            stream.write('\r\n%s\r\n' % (body,))
        stream.close()
        del self._streams[stream]

    def _start_response(self, state, status, headers, exc_info=None):
        state.response_status = status
        state.response_headers = headers
        return functools.partial(self._write_response, state)

    def _flush_response(self, address):
        stream, _, status, headers = self._streams[address]
        resp = 'HTTP/1.1 ' + status.strip() + '\r\n'
        resp += '\r\n'.join('%s: %s' % (k, v) for k, v in headers)
        stream.write(resp + '\r\n')

    def _write_response(self, state, data):
        state.write(data)

    def _on_request_body(self, state, env, data):
        env['wsgi.input'] = HTTPBody(data)
        self._invoke_application(state, env)

    def _on_headers(self, address, stream, data):
        try:
            http_line, headers = data.split('\r\n', 1)
            http_verb, uri, http_version = http_line.split()
            if http_version not in ('HTTP/1.0', 'HTTP/1.1'):
                self.respond_error(address, stream, 400, 'Bad Request')
                return
        except ValueError:
            self.respond_error(address, stream, 400, 'Bad Request')
            return

        state = self._streams[stream]
        state.request_line = http_line
        headers = HTTPHeaders.parse(headers.strip().split('\r\n'))
        content_length = headers.get('Content-Length')
        if content_length:
            content_length = int(content_length)
            if content_length > self.stream.max_buffer_size:
                self._respond_error(address, stream, 413, 'Request Entity Too Large', 'Max Content-Length is %d' % (self.stream.max_buffer_size,))
                return
            if headers.get('Expect') == '100-continue':
                self.stream.write('HTTP/1.1 100 (Continue)\r\n\r\n')

        sock_addr, sock_port = stream.socket.getsockname()
        qs = urlparse.urlparse(uri)
        env = {
            'REQUEST_METHOD': http_verb,
            'SCRIPT_NAME': '',
            'PATH_INFO': qs.path,
            'QUERY_STRING': qs.query,
            'CONTENT_TYPE': headers.get('Content-Type'),
            'CONTENT_LENGTH': content_length,
            'SERVER_NAME': headers.get('Host', sock_addr),
            'SERVER_PORT': sock_port,
            'SERVER_PROTOCOL': http_version,
            'wsgi.version': (1, 0),
            'wsgi.url_scheme': 'http',
            'wsgi.input': None,
            'wsgi.errors': None,
            'wsgi.multithread': False,
            'wsgi.multiprocess': False,
            'wsgi.run_once': False
            }
        for k, v in headers.iteritems():
            env['HTTP_' + k.upper()] = v

        if content_length:
            self.stream.read_bytes(content_length, functools.partial(self._on_request_body, state, env))
            return
        else:
            env['wsgi.input'] = HTTPBody('')
            self._invoke_application(state, env)
            
    def handle_request(self, stream, address):
        self._streams[stream] = StreamState(address, stream)
        stream.read_until('\r\n\r\n', wrap_stack(self._on_headers, address, stream))

    def _invoke_application(self, state, env):
        for data in self.request_callback(env, functools.partial(self._start_response, state)):
            if not data:
                continue
            state.write(data)
        state.finished_writing = True
        state.write('')
        del self._streams[state.stream]

    def _handle_events(self, fd, events):
        while True:
            try:
                connection, address = self._socket.accept()
            except socket.error, e:
                if e.args[0] in (errno.EWOULDBLOCK, errno.EAGAIN):
                    return
                raise
            try:
                stream = iostream.IOStream(connection, io_loop=self.io_loop)
                self.handle_request(stream, address)
            except:
                logging.error("Error in connection callback", exc_info=True)

if __name__ == '__main__':

    def simple_app(environ, start_response):
        """Simplest possible application object"""
        status = '200 OK'
        response_headers = [('Content-type', 'text/plain')]
        start_response(status, response_headers)
        yield 'Hello world!\n'

    server = HTTPServer(simple_app)
    server.bind(8888)
    server.start()
    ioloop.IOLoop.instance().start()
