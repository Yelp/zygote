Zygote
======

Zygote is a Python program that assists in running pre-forked Python web
applications. The problem is attempts to solve is the ability to deploy new
code, and have HTTP workers efficiently move over to serving the new code,
without causing any service interruptions.

Background Problem
------------------

It's easier to explain what Zygote does by explaining the background problem and
assumptions it makes. Suppose I have a web application called `blog` that I'm
serving requests from. I have deploy scripts that deploy the code to a directory
like `/var/blog/5d0959` and `/var/blog/d2dd1`, where the part like `5d0959` is
constructed based on the git commit that the deployment is for (this could just
as easily be a CVS/SVN/Mercurial revision, etc.). The active code version is
handled with a symlink such as `/var/blog/current` that points to the actual
currently deployed version of the code.

This is a pretty standard technique -- it allows easily rolling back code (just
change `/var/blog/current` to point at an older revision), and it ensures that
deploys are atomic, since the symlink won't be updated until all code is fully
copied over.

Also, suppose that you are running your web server in a configuration where you
want only at most N processes working. For instance, you may determine that your
webserver can run 100 instances of your application at once, without risk of
swapping.

When deploying a new revision of the code (say you're going from revision `A` to
revision `B`), you start off with 100 copies of `A` and want to end up with 100
copies of `B`. To do this without service interruption, you can't just kill all
100 copies of `A` and start 100 copies of `B` -- then there's a brief period
where no versions of the code are running, or not enough workers are
running. You also can't just start 100 copies of `B` first, and then kill the
copies of `A` -- if you do this you'll end up swapping (or you'll need to have
2x as much RAM in every machine, to account for this). The ideal situation is
where the code transitions over to `B` as web workers servicing `A` requests are
free, so you go from 100 `A` and 0 `B` to 90 `A` and 10 `B`, and then 80 `A` and
20 `B`, etc. Additionally, it's best if you can pre-load the code for a copy,
and then use a pre-fork model to ensure that when you spawn 100 copies of `B`,
you're only importing all of that code one time.

Zygote implements the versioning transition described above, as well as
pre-forking.

How It Works
------------

The concept of zygote processes on Unix systems is not new; see Chromium's
[LinuxZygote](http://code.google.com/p/chromium/wiki/LinuxZygote) wiki page for
a description of how the Chromium browser does a similar thing. The basic idea
is that in a zygote model, you have a process tree that looks something like
this:

    zygote-master
	 \
	  `--- zygote A
      |     `--- worker
      |      --- worker
      |
      `---- zygote B
            `--- worker
             --- worker

When the master zygote process wants to spawn a copy of `B`, it sends an
instruction over a Unix pipe to `zygote B` that says "fork yourself, and run a
new worker". Likewise, if the zygote master thinks that `A` is running too many
workers, it can send `zygote A` an instruction that says "kill one of your
workers". Because the workers are created using the `fork(2)` system call, the
zygotes can import Python modules once and the workers spawned will
automatically have all of the code available to them, initialized and in memory.

Transitioning code from `A` to `B` as described in the previous instruction just
consists of sending these kill/spawn requests to `zygote A` and `zygote B` in
the right order, and at an appropriate speed.

Internally, communication between the master and the zygotes is done using
standard Unix pipes.

If you use a command like `pstree` or `ps -eFH` you can verify that the process
tree looks as expected. Additionally, if you have the `setproctitle` Python
module available, the processes will set their titles such that it's easy to see
what version of the code everything is running.

How to Use It
-------------

To use Zygote, you need to write a module that implements a `get_application`
method. That method takes no arguments, and returns an object that can be used
by a [Tornado](http://www.tornadoweb.org/) HTTPServer object (typically this
would be an instance of `tornado.web.Application`).

After that, an invocation of Zygote would be done like this:

    python -m zygote.main -p 8000 -b ./example example

Let's break that down. The `python -m zygote.main` part instructs Python to run
Zygote's `main` module. The parts after that are options and arguments. The `-p
8000` option instructs Zygote that your application will be served from port
8000. The `-b ./example` option states that the symlink for your application
exists at `./example`. This does not strictly need to be a symlink, but the code
versioning will only work if it is a symlink. The final argument is just
`example` and that states that the module name for the application is `example`.

The example invocation given above will work if you run it from a clone of the
Zygote source code. The `-b` option tells Zygote what to insert into `sys.path`
to make your code runnable, and in the Zygote source tree there's a file named
`example/example.py`. In other words, `example` gets added to `sys.path` and
that makes `example.py` importable by doing `import example`.

Caveats
-------

Currently Zygote only works with [Tornado](http://www.tornadoweb.org/)
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

The zygote master opens an abstract unix domain socket with a name like this:
    '\0' + "zygote_" + pid_of_master
Messages to the master have the following format:
    str(pid_of_sender) + ' ' + msg_type + ' ' + msg_body
The msg_type is a single byte, by convention it corresponds to an actual ASCII
character. See `zygote/message.py` for the different message types.

The master spawns zygotes. A zygote supports two signals. Sending it `SIGTERM`
instructs it to exit. Sending the zygote `SIGUSR1` instructs the zygote to fork
and start a worker process. The worker processes communicate to the zygote
master using the aforementioned abstract unix domain socket.

Sending `SIGINT` or `SIGTERM` to a worker causes it to exit with status 0.

When a worker is spawned, it will send a "spawn" message to the master, signaled
by `S`. The body of the "spawn" message is the PPID of the worker (i.e. the PID
of the zygote that spawned the worker).

When a worker exits, its parent will send an "exit" message to the master,
signaled by `X`. The body of the message will be of the format
`str(pid_of_worker) + ' ' + str(exit_status)`. The master process will decide
whether the zygote should respawn the worker or not (by sending `SIGUSR1` to the
zygote if the worker should be respawned).

When a worker begins processing an HTTP request, it will send a "begin http"
message, signaled by `B`. The body of the message will contain the request
string sent by the client, so it will be something like `GET / HTTP/1.1`.

When a worker finishes processing an HTTP request, it will send an "end http"
message, signaled by `E`. There is no body.

While all of this is going on, the master processes operates a simple state
machine to keep track of the current status of all of the zygotes and worker
processes. It's up to the master process to know when it's safe to gracefully
kill a worker (which it can tell because the last message from the worker was an
`S` or an `E`). It's up to the master process to keep track of how many requests
a worker has processed, and whether that means the worker should be killed (and
respawned). And so on. The implicit goal of this is that all complicated process
management logic should exist in the zygote master; there should be very little
logic in the zygotes, or in the worker children.
