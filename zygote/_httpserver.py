import tornado

if tornado.version_info >= (2,1,0):
    from ._httpserver_2 import HTTPServer, HTTPConnection, HTTPRequest
    HTTPServer = HTTPServer
else:
    from ._httpserver_1 import HTTPServer, HTTPConnection, HTTPRequest
    HTTPServer = HTTPServer
