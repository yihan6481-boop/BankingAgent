"""Pause flag shared with the persistent human support channel."""
from .payment_auth import enabled
from .db import write_transaction,read_connection
from assistance.service import SupportView

def status(sid):
    from assistance.service import status as current
    return current(sid)

def request(sid,paused=True):
    if not enabled():return status(sid)
    from assistance.service import request as start,end
    return start(sid) if paused else end(sid,resume=True)

def assert_new_automation(sid):
    if enabled() and status(sid)['automation_paused']:
        from lifecycle.store import TaskError
        error=TaskError('automation_paused_for_support',status=409);error.preparation_no_task=True;raise error
