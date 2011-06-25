Zygote
======

Zygote is a Python program that assists in running pre-forked Python web
applications. The problem it attempts to solve is the ability to deploy new
code, and have HTTP workers efficiently move over to serving the new code,
without causing any service interruptions.

Let's say you're serving an application, and the currently deployed version is
called `A`. You're trying to deploy a new version of your web app, and that
version is called `B`. The ideal way this would work is like so:

* A new Python interpreter `P` starts up, imports code from `B` and does all of
  the static initialization and loads modules. This process will only happen
  once.

* New HTTP workers are created by forking `P`. Due to the use of forking, new
  workers don't need to reimport lots of code (so starting a worker is cheap),
  and workers can share static data structures (so starting a new worker
  consumes significantly less memory).

* In progress requests that are being run from the `A` version of the code
  should be allowed to complete, and not be interrupted; deploying new code
  should not cause anyone to get an HTTP 500 response, or even be noticeable by
  users. However, as requests from `A` complete, they should exit and allow `B`
  workers to be spawned.

* The deploy code needs to be cognizant of how many HTTP workers the system is
  capable of running (usually this means don't run more workers than you have
  RAM allocated for), so if a machine is capable of supporting 200 workers, and
  100 of them are serving requests for `A` at the time of the deploy, at first
  the 100 idle `A` workers can be killed and 100 `B` workers can be spawned,
  and then `A` workers are killed and `B` workers are spawned as the `A`
  workers complete their requests.

This is what Zygote does. New code deployments happen more quickly, use less CPU
and disk at startup, and workers use less memory. Zygote has an embedded HTTP
server based on the one provided by Tornado, but this is complementary to a
real, full-fledged HTTP server like Apache or Nginx -- Zygote's expertise is
just in managing Python web processes. It's OK to run Apache or Nginx in front
of Zygote.

Zygote is licensed under the `Apache Licence, Version 2.0
<http://www.apache.org/licenses/LICENSE-2.0.html>`_. You should find a copy of
this license along with the Zygote source, in the ``LICENSE`` file.

How It Works
------------

The concept of "zygote" processes on Unix systems is not new; see Chromium's
`LinuxZygote <http://code.google.com/p/chromium/wiki/LinuxZygote>`_ wiki page for
a description of how they're used in the Chromium browser. In the Zygote process
model there is a process tree that looks something like this::

    zygote-master
     \
      `--- zygote A
      |     `--- worker
      |      --- worker
      |
      `--- zygote B
            `--- worker
             --- worker

(Some other zygote models like those used by HAProxy and Chrome have a slightly
different, flatter process tree, but the diagram shows how it works in Zygote).

When the master zygote process wants to spawn a copy of `B`, it forks, and the
forked process, the `B` zygote, can then fork again to create workers. Because
the workers are created using the `fork(2)` system call, the zygotes can import
Python modules once and the workers spawned will automatically have all of the
code available to them, initialized and in memory. Not only is this faster, it
also saves a lot of memory compared to reimporting the code multiple times, and
having identical pages in memory that are unshared.

Transitioning code from `A` to `B` as described in the previous section consists
of the master killing idle workers and instructing the appropriate zygote to
fork.

Internally, communication between the different processes is done using abstract
unix domain sockets.

If you use a command like ``pstree`` or ``ps -eFH`` you can verify that the process
tree looks as expected. Additionally, if you have the `setproctitle` Python
module available, the processes will set their titles such that it's easy to see
what version of the code everything is running.

How to Use It
-------------

To use Zygote, you need to write a module that implements a `get_application()`
method. That method can take any number of string arguments, and must return an
object that can be used by a `Tornado <http://www.tornadoweb.org/>`_
``HTTPServer`` object (typically this would be an instance of
``tornado.web.Application``). Any extra arguments passed to ``zygote`` on the
command line will be fed in as positional arguments to `get_application()`, so
you can pass in extra data (e.g. the path to a config file) using this
mechanism; however, it is strongly encouraged that any arguments to
`get_application()` be made optional arguments, since the ``zygote`` command
line tool doesn't have any knowledge of the expected arguments and cannot
display useful help or error messages to users.

Your application can be a "pure" Tornado web application, or a WSGI
application. If you're using WSGI, make sure you first wrap the application
using ``tornado.wsgi.WSGIContainer``.

After that, an invocation of Zygote would be done like this::

    python -m zygote.main -p 8000 -b ./example example

Let's break that down. The ``python -m zygote.main`` part instructs Python to
run Zygote's `main` module. The parts after that are options and arguments. The
``-p 8000`` option instructs Zygote that your application will be served from
port 8000. The ``-b ./example`` option states that the symlink for your
application exists at ``./example``. This does not strictly need to be a symlink,
but the code versioning will only work if it is a symlink. The final argument is
just ``example`` and that states that the module name for the application is
``example``.

The example invocation given above will work if you run it from a clone of the
Zygote source code. The ``-b`` option tells Zygote what to insert into `sys.path`
to make your code runnable, and in the Zygote source tree there's a file named
``example/example.py``. In other words, `example` gets added to `sys.path` and
that makes ``example.py`` importable by doing ``import example``.

Caveats
-------

Currently Zygote only works with `Tornado <http://www.tornadoweb.org/>`_
applications. It should be fairly straightforward to get it working with other
WSGI webservers, however. It just requires someone whose willing to roll their
sleeves up and hack on the code a bit.

Your application must be fork-safe to use Zygote. That means that it's best if
creating non-forksafe resources such as database connections is not done as a
side-effect of importing your code, and only done upon initialization of the
code. If you *do* have non-forksafe resources in your code, you need to write
code that reinitializes those resources when the application is instantiated (or
by detecting when the current PID changes).

Zygote supports IPv4 only. Support for IPv6 should be easy to add, if there's a
need.

Testing
-------

There are unit tests, which exist in the ``tests`` directory. You should be able
to run them by invoking ``make test``, e.g.::

    evan@zeno ~/code/zygote (master) $ make test
    tests.test ZygoteTests.test_http_get ... ok in 2.53s
    
    PASSED.  1 test / 1 case: 1 passed (0 unexpected), 0 failed (0 expected).  (Total test time 2.53s)

Some caveats. You need a very recent version of Tornado to run the tests. This
is to force Tornado to use the "simple" http client. Hopefully the API will be
stable going forward from Tornado 0.2.0.

You will also need `Testify <http://pypi.python.org/pypi/testify/>`_ to run the
tests. Any version of Testify should work.
