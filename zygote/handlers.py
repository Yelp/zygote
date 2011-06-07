import datetime
import os
import time

import tornado.web
import zygote.util

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

class HTMLHandler(tornado.web.RequestHandler):

    def get(self):
        self.render('home.html')

class JSONHandler(tornado.web.RequestHandler):

    def get(self):

        self.zygote_master.zygote_collection.update_meminfo()
        env = self.zygote_master.zygote_collection.to_dict()
        env['basepath'] = self.zygote_master.basepath
        env['time_created'] = self.zygote_master.time_created
        env['time_now'] = datetime.datetime.now()
        env.update(zygote.util.meminfo_fmt())

        self.set_header('Content-Type', 'application/json')
        self.write(json.dumps(env, cls=JSONEncoder, indent=2))

def get_httpserver(io_loop, port, zygote_master):
    JSONHandler.zygote_master = zygote_master
    app = tornado.web.Application([('/', HTMLHandler), ('/json', JSONHandler)],
                                  debug=False,
                                  static_path=os.path.realpath('static'),
                                  template_path=os.path.realpath('templates'))
    http_server = tornado.httpserver.HTTPServer(app, io_loop=io_loop, no_keep_alive=True)
    http_server.listen(port)
    return http_server
