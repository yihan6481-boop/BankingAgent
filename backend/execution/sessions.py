"""One registry for transient execution bindings, shared with the HTTP host."""
from threading import RLock
from lifecycle.store import TaskError
sessions = {}
locks = {}
lock = RLock()
def session_for(sid):
    with lock:
        value = sessions.get(sid)
    if value is None: raise TaskError('session_not_found', status=404)
    return value
def session_lock(sid):
    with lock: return locks.setdefault(sid,RLock())
def assert_owner(session, principal):
    if (session.principal is None or session.principal_id != principal.principal_id
            or session.user_id != principal.user_id):
        raise TaskError('session_principal_mismatch',status=403)

def restore_principal(context,row):
    """Restore no authorization: accepts only a verified server credential."""
    from storage.pg02 import VerifiedContext
    from security.schemas import Principal
    from execution.context import ExecutionContext
    from datetime import datetime
    if type(context) is not VerifiedContext:raise TaskError('session_principal_mismatch',status=403)
    with lock:
        session=sessions.get(context.session_id)
        if session is None:
            principal=Principal(principal_id=context.principal_id,user_id=context.user_id,
                display_name=row['display_name'],kind=row['kind'],credential_id=context.credential_id,
                authenticated_at=datetime.fromisoformat(row['created_at']))
            session=ExecutionContext(principal=principal,principal_id=principal.principal_id,
                user_id=principal.user_id,session_id=context.session_id,protocol='v2')
            sessions[context.session_id]=session
    if session.protocol!='v2' or session.principal is None or session.principal.principal_id!=context.principal_id or session.user_id!=context.user_id:raise TaskError('session_principal_mismatch',status=401)
    return session.principal
