import logging
import time
import tornado.web

start_time = time.time()
log = logging.getLogger('example')
log.debug('started up')

class StatusHandler(tornado.web.RequestHandler):

    def get(self):
        log.debug('get request to /status')
        self.content_type = 'text/plain'
        self.response.write('uptime: %1.3f\n' % (time.time() - start_time))

def get_application():
    log.debug('creating application for \'example\'')
    return tornado.web.Application([('/status', StatusHandler)], debug=False)
