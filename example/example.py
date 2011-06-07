import logging
import time
import tornado.web

start_time = time.time()
log = logging.getLogger('example')
log.debug('started up')

class StatusHandler(tornado.web.RequestHandler):

    def get(self):
        time.sleep(0.1) # to make this easier to see in the process manager
        self.content_type = 'text/plain'
        self.write('uptime: %1.3f\n' % (time.time() - start_time))

def get_application():
    log.debug('creating application for \'example\'')
    return tornado.web.Application([('/', StatusHandler)], debug=False)
