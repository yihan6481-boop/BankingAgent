"""Closed structured gateway. No SQL, NL interpretation or state mutations."""
from .contracts import Call
from . import service

def capabilities():
    return {'protocol':'tool-foundation-1','arguments_schema':Call.model_json_schema(),
        'operations':list(Call.model_fields['operation'].annotation.__args__),
        'authorization':'server-authenticated context; tools never confirm or submit funds'}

def run(call,principal,session_id):
    call=Call.model_validate(call)
    from storage.config import pg02_enabled
    from lifecycle.store import TaskError
    if not pg02_enabled():
        raise TaskError('postgres_required',status=503)
    from storage.pg02 import current_context
    from .contracts import feedback
    context=current_context()
    if (context.session_id != session_id or context.principal_id != principal.principal_id
            or context.user_id != principal.user_id):
        return feedback('denied','session_principal_mismatch')
    from execution.sessions import session_lock
    with session_lock(session_id):
        return service.run(call,principal,session_id)
