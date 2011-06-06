import optparse
import os
import sys

import zygote.master
import zygote.util

if __name__ == '__main__':
    parser = optparse.OptionParser()
    parser.add_option('-b', '--basepath', default=os.environ.get('BASEPATH', ''), help='The basepath to use')
    parser.add_option('--control-port', type='int', default=5100, help='The control port to listen on')
    parser.add_option('-d', '--debug', default=False, action='store_true', help='Enable debugging')
    parser.add_option('-p', '--port', type='int', default=0, help='The port to bind on')
    parser.add_option('-i', '--interface', default='', help='The interface to bind on')
    parser.add_option('--num-workers', type='int', default=8, help='How many workers to run')
    opts, args = parser.parse_args()

    if not opts.basepath:
        parser.error('The `basepath` cannot be empty; specify one with -b or exporting BASEPATH')
        sys.exit(1)
    if not opts.port:
        parser.error('No port was specified')
        sys.exit(1)
    if not len(args) == 1:
        parser.error('Must specify exactly one argument, the module to load')

    module = args.pop()
    zygote.master.main(opts, module)
