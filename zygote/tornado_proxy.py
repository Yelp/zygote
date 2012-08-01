"""
Wrapper for tornado.httpserver.HTTPServer which understands the HAProxy
PROXY protocol for passing remote address information. For more information
about this protocol, see

    http://haproxy.1wt.eu/download/1.5/doc/proxy-protocol.txt

This code should be compatible with Tornado 1.1 through Tornado 2.2.1. It
currently only supports IPv4.

Copyright (c) 2012 Yelp, Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may
not use this file except in compliance with the License. You may obtain
a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
License for the specific language governing permissions and limitations
under the License.
"""

import errno
import functools
import logging
import socket

import tornado
from tornado import iostream
from ._httpserver import HTTPServer, HTTPConnection


def _get_proxy(content, after, io_loop):
    """Read PROXY information from the given IOStream, then call the given
    callback function.

    The callback will be passed a single argument, a tuple of the address as
    would've been returned from accept() (that is to say, ('a.b.c.d',
    80))"""
    content = content.rstrip("\r\n")
    fields = content.split(" ")
    assert fields[0] == "PROXY", "Invalid PROXY line"
    assert fields[1] == "TCP4", "Only IPv4 is currently supported"
    source_address = fields[2]
    source_port = int(fields[4])
    # bounce this through the IOLoop for dumb reasons
    after((source_address, source_port))


class _ProxyWrappedHTTPServerTornadoOne(HTTPServer):
    """
    Wrapper for tornado.httpserver.HTTPServer supporting
    the PROXY protocol and compatible with Tornado 1.x
    """
    def __init__(self, *args, **kwargs):
        if kwargs.get('ssl_options', None) is not None:
            raise ValueError("Cannot use SSL with ProxyWrappedHTTPServer")
        return super(_ProxyWrappedHTTPServerTornadoOne, self).__init__(*args, **kwargs)

    def _handle_events(self, fd, events):
        while True:
            try:
                connection, address = self._socket.accept()
            except socket.error, e:
                if e[0] in (errno.EWOULDBLOCK, errno.EAGAIN):
                    return
                raise
            assert self.ssl_options is None, "SSL Not supported in wrapped servers"
            try:
                stream = iostream.IOStream(connection, io_loop=self.io_loop)
                stream.read_until("\r\n", functools.partial(_get_proxy, after=lambda address: HTTPConnection(stream,
                    address, self.request_callback, self.no_keep_alive,
                    self.xheaders, close_callback=self._close_callback, headers_callback=self._headers_callback), io_loop=self.io_loop))
            except:
                logging.error("Error in connection callback", exc_info=False)


class _ProxyWrappedHTTPServerTornadoTwo(HTTPServer):
    """
    Wrapper for tornado.httpserver.HTTPServer supporting
    the PROXY protocol and compatible with Tornado 2.x
    """
    def __init__(self, *args, **kwargs):
        if kwargs.get('ssl_options', None) is not None:
            raise ValueError("Cannot use SSL with ProxyWrappedHTTPServer")
        return super(_ProxyWrappedHTTPServerTornadoTwo, self).__init__(*args, **kwargs)

    def handle_stream(self, stream, _):
        stream.read_until("\r\n", functools.partial(_get_proxy, after=lambda address: HTTPConnection(stream,
            address, self.request_callback, self.no_keep_alive,
            self.xheaders, close_callback=self._close_callback, header_callback=self._headers_callback), io_loop=self.io_loop))


if tornado.version_info < (2, 0, 0):
    ProxyWrappedHTTPServer = _ProxyWrappedHTTPServerTornadoOne
else:
    ProxyWrappedHTTPServer = _ProxyWrappedHTTPServerTornadoTwo
