#!/usr/bin/env python
#
# Copyright 2011, Yelp Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import optparse
import os
import sys

import zygote.master
import zygote.util

def main():
    usage = 'usage: %prog -b <basepath> -m <module> -p <port> [module_args...]'
    parser = optparse.OptionParser(usage=usage)
    parser.add_option('-b', '--basepath', default=os.environ.get('BASEPATH', ''), help='The basepath to use')
    parser.add_option('--control-port', type='int', default=5100, help='The control port to listen on')
    parser.add_option('-d', '--debug', default=False, action='store_true', help='Enable debugging')
    parser.add_option('-n', '--name', default=None, help='The name of the application to set in proctitle, otherwise use the app module.')
    parser.add_option('--version', default=None, help='The version of the application to set in proctitle.')
    parser.add_option('-m', '--module', default=None, help='The name of the module holding get_application()')
    parser.add_option('-p', '--port', type='int', default=0, help='The port to bind on')
    parser.add_option('-i', '--interface', default='', help='The interface to bind on')
    parser.add_option('--num-workers', type='int', default=8, help='How many workers to run')
    parser.add_option('--max-requests', type='int', default=None, help='The maximum number of requests a child can run')
    parser.add_option('--zygote-base', default=None, help='The base path to the zygote')
    parser.add_option('--cert', default=None, help='Certificate to use for HTTPS traffic')
    parser.add_option('--key', default=None, help='Private key for HTTPS traffic')
    parser.add_option('--cacerts', default=None, help='File containing a list of root certificates')
    parser.add_option(
        '--control-socket',
        dest='control_socket_path',
        default=os.path.join(zygote.util.get_rundir(), "zygote_master.sock"),
        help='The socket to control zygote master at run time'
    )

    opts, args = parser.parse_args()

    if not opts.basepath:
        parser.error('The `basepath` cannot be empty; specify one with -b or exporting BASEPATH')
        sys.exit(1)
    if not opts.port:
        parser.error('No port was specified')
        sys.exit(1)
    if not opts.module:
        parser.error('You must specify a module argument using -m')
        sys.exit(1)

    zygote.master.main(opts, args)

if __name__ == '__main__':
    main()
