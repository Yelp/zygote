from __future__ import with_statement

import datetime
import os
import socket
import time
import traceback

import tornado.web
from tornado.wsgi import WSGIApplication
import zygote.util

from .httpd import HTTPServer

try:
    import simplejson as json
except ImportError:
    import json

class JSONEncoder(json.JSONEncoder):

    def default(self, obj):
        if hasattr(obj, 'to_dict'):
            return obj.to_dict()
        elif type(obj) is datetime.datetime:
            return time.mktime(obj.timetuple()) + obj.microsecond / 1e6
        else:
            return super(JSONEncoder, self).default(obj)

class RequestHandler(tornado.web.RequestHandler):

    def get_error_html(self, status_code, **kwargs):
        if 500 <= status_code <= 599:
            self.set_header('Content-Type', 'text/plain')
            return traceback.format_exc()
        else:
            return super(RequestHandler, self).get_error_html(status_code, **kwargs)

class TemplateHandler(RequestHandler):

    def get(self):
        self.set_header('Content-Type', 'text/plain')
        self.set_header('Cache-Control', 'max-age=0')
        static_path = self.application.settings['static_path']
        with open(os.path.join(static_path, 'template.html')) as template:
            self.write(template.read())

class HTMLHandler(RequestHandler):

    def get(self):
        self.render('home.html')

class JSONHandler(RequestHandler):

    def get(self):

        self.zygote_master.zygote_collection.update_meminfo()
        env = self.zygote_master.zygote_collection.to_dict()
        env['hostname'] = socket.gethostname()
        env['interface'], env['port'] = self.application.settings['worker_sockname']
        env['pid'] = os.getpid()
        env['basepath'] = self.zygote_master.basepath
        env['time_created'] = self.zygote_master.time_created
        env.update(zygote.util.meminfo_fmt())

        self.set_header('Content-Type', 'application/json')
        self.write(json.dumps(env, cls=JSONEncoder, indent=2))

def get_httpserver(io_loop, port, zygote_master, zygote_base=None):
    if zygote_base is not None:
        static_path = os.path.realpath(os.path.join(zygote_base, 'static'))
        template_path = os.path.realpath(os.path.join(zygote_base, 'templates'))
    else:
        static_path = os.path.realpath('static')
        template_path = os.path.realpath('templates')

    JSONHandler.zygote_master = zygote_master
    app = WSGIApplication([('/', HTMLHandler),
                           ('/json', JSONHandler),
                           ('/template', TemplateHandler)],
                          debug=False,
                          static_path=static_path,
                          template_path=template_path)
    app.settings['worker_sockname'] = zygote_master.sock.getsockname()
    http_server = HTTPServer(app, io_loop=io_loop, keep_alive=False)
    http_server.bind(port)
    http_server.start()
    return http_server
