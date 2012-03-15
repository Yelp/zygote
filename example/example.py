import logging
import time
import tornado.web

start_time = time.time()
log = logging.getLogger('example')
log.debug('started up')

class StatusHandler(tornado.web.RequestHandler):

    def get(self):
        self.content_type = 'text/plain'
        self.write('uptime: %1.3f\n' % (time.time() - start_time))

def initialize(*args, **kwargs):
    pass

def get_application(*args, **kwargs):
    log.debug('creating application for \'example\'')
    return tornado.web.Application([('/', StatusHandler)], debug=False)
