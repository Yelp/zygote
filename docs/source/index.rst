Zygote
======

Zygote is a Python program that assists in running pre-forked Python web
applications. The problem is attempts to solve is the ability to deploy new
code, and have HTTP workers efficiently move over to serving the new code,
without causing any service interruptions.

Let's say you're serving your application, and the currently deployed version is
called `A`. You're trying to deploy a new version of your web app, and that
version is called `B`. The way you want it to work is like this:

* A new Python interpreter `P` starts up, imports code from `B` and does all of
  the static initialization and loads modules. This process should only happen
  once.

* New HTTP workers are created by forking `P`. That way new workers don't need
  to reimport lots of code (so starting a worker is significantly cheaper in
  terms of disk I/O and CPU time), and workers can share static data structures
  (so starting a new worker consumes significantly less memory).

* In progress requests that are being run from the `A` version of the code
  should be allowed to complete, and not be interrupted; deploying new code
  should not cause anyone to get an HTTP 500 response, or even be noticeable by
  users.

* The deploy code needs to be cognizant of how many HTTP workers the system is
  capable of running (usually this means don't run more workers than you have
  RAM allocated for), so if a machine is capable of supporting 200 workers, and
  100 of them are serving requests for `A` at the time of the deploy, at first
  the 100 idle `A` workers can be killed and 100 `B` workers can be spawned,
  and then `A` workers are killed and `B` workers are spawned as the `A`
  workers complete their requests.

This is what Zygote does. Zygote has an embedded HTTP server based on the one
provided by Tornado, but this is complementary to a real, full-fledged HTTP
server like Apache or Nginx -- Zygote's expertise is just in managing Python web
processes. It's OK to run Apache or Nginx in front of Zygote.

Zygote is licensed under the
`Apache Licence, Version 2.0 <http://www.apache.org/licenses/LICENSE-2.0.html>`_. You
should find a copy of this license along with the Zygote source, in the
``LICENSE`` file.

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

(Some other models like HAProxy and Chrome have a slightly different, flatter
process tree, but this is how it works in Zygote).

When the master zygote process wants to spawn a copy of `B`, it sends an
instruction over a Unix pipe to `zygote B` that says "fork yourself, and run a
new worker". Because the workers are created using the `fork(2)` system call,
the zygotes can import Python modules once and the workers spawned will
automatically have all of the code available to them, initialized and in
memory. Not only is this faster, it also saves a lot of memory compared to
reimporting the code multiple times, and having identical pages in memory that
are unshared.

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

To use Zygote, you need to write a module that implements a `get_application`
method. That method takes no arguments, and returns an object that can be used
by a `Tornado <http://www.tornadoweb.org/>`_ HTTPServer object (typically this
would be an instance of `tornado.web.Application`).

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

Process Protocol
----------------

The zygote master opens an abstract unix domain socket with a name like this::

    '\0' + "zygote_" + pid_of_master

Messages to the master have the following format::

    str(pid_of_sender) + ' ' + msg_type + ' ' + msg_body

The ``msg_type`` is a single byte, by convention it corresponds to an actual
ASCII character. See ``zygote/message.py`` for the different message types.

The master spawns zygotes. A zygote supports two signals. Sending it ``SIGTERM``
instructs it to exit. Sending the zygote ``SIGUSR1`` instructs the zygote to
fork and start a worker process. The worker processes communicate to the zygote
master using the aforementioned abstract unix domain socket.

Sending ``SIGINT`` or ``SIGTERM`` to a worker causes it to exit with status 0.

When a worker is spawned, it will send a "spawn" message to the master, signaled
by ``S``. The body of the "spawn" message is the PPID of the worker (i.e. the
PID of the zygote that spawned the worker).

When a worker exits, its parent will send an "exit" message to the master,
signaled by ``X``. The body of the message will be of the format
``str(pid_of_worker) + ' ' + str(exit_status)``. The master process will decide
whether the zygote should respawn the worker or not (by sending ``SIGUSR1`` to
the zygote if the worker should be respawned).

When a worker begins processing an HTTP request, it will send a "begin http"
message, signaled by ``B``. The body of the message will contain the request
string sent by the client, so it will be something like ``GET / HTTP/1.1``.

When a worker finishes processing an HTTP request, it will send an "end http"
message, signaled by ``E``. There is no body.

While all of this is going on, the master processes operates a simple state
machine to keep track of the current status of all of the zygotes and worker
processes. It's up to the master process to know when it's safe to gracefully
kill a worker (which it can tell because the last message from the worker was an
``S`` or an ``E``). It's up to the master process to keep track of how many
requests a worker has processed, and whether that means the worker should be
killed (and respawned). And so on. The implicit goal of this is that all
complicated process management logic should exist in the zygote master; there
should be very little logic in the zygotes, or in the worker children.

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
