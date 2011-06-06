class Message(object):

    # sent by master to zygote
    SPAWN_CHILD = 's'   # spawn a new child
    KILL_CHILD  = 'k'   # kill a child
    REPORT      = 'r'   # send child report (Note: also sent by zygotes to master)
    EXIT        = 'x'   # exit

    # sent by zygote to master
    CHILD_CREATED = 'c' # a child was created
    CHILD_DIED    = 'd' # a child died or was killed
