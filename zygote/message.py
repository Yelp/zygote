import os

class Message(object):

    CANARY_INIT           = 'I'

    CREATE_WORKER         = 'C'
    KILL_WORKERS          = 'L'
    SHUT_DOWN             = 'K'

    WORKER_START          = 'S'
    WORKER_EXIT           = 'X'
    WORKER_EXIT_INIT_FAIL = 'Y'

    HTTP_BEGIN            = 'B'
    HTTP_END              = 'E'

    @classmethod
    def emit(cls, body):
        return '%d %s %s' % (os.getpid(), cls.msg_type, body)

    @classmethod
    def parse(cls, msg):
        pid, type, body = msg.split(' ', 2)
        pid = int(pid)
        if type == cls.CREATE_WORKER:
            return MessageCreateWorker(pid, body)
        elif type == cls.KILL_WORKERS:
            return MessageKillWorkers(pid, body)
        elif type == cls.CANARY_INIT:
            return MessageCanaryInit(pid, body)
        elif type == cls.WORKER_START:
            return MessageWorkerStart(pid, body)
        elif type == cls.WORKER_EXIT:
            return MessageWorkerExit(pid, body)
        elif type == cls.HTTP_BEGIN:
            return MessageHTTPBegin(pid, body)
        elif type == cls.HTTP_END:
            return MessageHTTPEnd(pid, body)
        elif type == cls.WORKER_EXIT_INIT_FAIL:
            return MessageWorkerExitInitFail(pid, body)
        elif type == cls.SHUT_DOWN:
            return MessageShutDown(pid, body)
        else:
            assert False

    def __init__(self, pid):
        self.pid = int(pid)

class MessageCanaryInit(Message):

    msg_type = Message.CANARY_INIT

    def __init__(self, pid, body):
        assert body == ''
        super(MessageCanaryInit, self).__init__(pid)

class MessageCreateWorker(Message):

    msg_type = Message.CREATE_WORKER

    def __init__(self, pid, body):
        assert body == ''
        super(MessageCreateWorker, self).__init__(pid)

class MessageKillWorkers(Message):

    msg_type = Message.KILL_WORKERS

    def __init__(self, pid, body):
        super(MessageKillWorkers, self).__init__(pid)
        self.num_workers_to_kill = int(body)

class MessageWorkerStart(Message):

    msg_type = Message.WORKER_START

    def __init__(self, pid, body):
        super(MessageWorkerStart, self).__init__(pid)
        created, ppid = body.split(' ')
        self.time_created = int(created)
        self.worker_ppid = int(ppid)

class MessageWorkerExit(Message):

    msg_type = Message.WORKER_EXIT

    def __init__(self, pid, body):
        super(MessageWorkerExit, self).__init__(pid)
        child_pid, status = body.split()
        self.payload = body
        self.child_pid = int(child_pid)
        self.status = int(status)

class MessageWorkerExitInitFail(Message):

    msg_type = Message.WORKER_EXIT_INIT_FAIL

    def __init__(self, pid, body):
        super(MessageWorkerExitInitFail, self).__init__(pid)
        child_pid, status = body.split()
        self.payload = body
        self.child_pid = int(child_pid)
        self.status = int(status)

class MessageHTTPBegin(Message):

    msg_type = Message.HTTP_BEGIN

    def __init__(self, pid, body):
        super(MessageHTTPBegin, self).__init__(pid)
        self.remote_ip, self.http_line = body.split(' ', 1)

class MessageHTTPEnd(Message):

    msg_type = Message.HTTP_END

    def __init__(self, pid, body):
        super(MessageHTTPEnd, self).__init__(pid)
        assert body == '' # just ignore the body, it should be empty

class MessageShutDown(Message):

    msg_type = Message.SHUT_DOWN

    def __init__(self, pid, body):
        super(MessageShutDown, self).__init__(pid)
        assert body == '' # just ignore the body, it should be empty


class ControlMessage(object):
    """Control Message type used by zygote master"""

    UNKNOWN = 'I'
    SCALE_WORKERS = 'S'

    @classmethod
    def emit(cls, body):
        return '%s %s' % (cls.msg_type, body)

    @classmethod
    def parse(cls, msg):
        type, body = msg.split(' ', 1)
        if type == cls.SCALE_WORKERS:
            return ControlMessageScaleWorkers(body)
        else:
            return ControlMessageUnknown(body)

class ControlMessageUnknown(ControlMessage):

    msg_type = ControlMessage.UNKNOWN

    def __init__(self, body):
        super(ControlMessageUnknown, self).__init__()

class ControlMessageScaleWorkers(ControlMessage):

    msg_type = ControlMessage.SCALE_WORKERS

    def __init__(self, body):
        super(ControlMessageScaleWorkers, self).__init__()
        self.num_workers = int(body)
