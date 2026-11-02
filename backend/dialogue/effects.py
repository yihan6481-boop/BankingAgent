"""Independent UI approval for destructive transitions; never model authority."""
from contextvars import ContextVar
from contextlib import contextmanager
from foundation.contracts import feedback
from lifecycle.store import TaskError

CONTROLLED={'draft_close','task_cancel','task_pause','task_takeover','task_rollback',
            'task_resume','task_schedule','task_return','task_note',
            'transfer_patch','task_modify','memory_replace','memory_delete','memory_save_business'}
reviewed=ContextVar('dialogue_reviewed_effect',default=False)


def proposal(name):
    return feedback('denied','explicit_action_required',data={
        'message':'此操作需要在下方核对具体目标及参数后点击应用；模型文本或资料不提供该授权。',
        'scope':'no mutation performed'},next_actions=['review_exact_operation_in_ui'])


@contextmanager
def explicit_review():
    token=reviewed.set(True)
    try:yield
    finally:reviewed.reset(token)


def apply(p,sid,cid,tid,call_id):
    """CAS reservation precedes the effect. Unknown is never automatically retried."""
    from .store import DialogueStore,transaction
    from psycopg.types.json import Jsonb
    from .links import tool_scope
    from .tools import execute
    from .projection import project
    store=DialogueStore();call=store.call_read(p,cid,tid,call_id)
    if call['name'] not in CONTROLLED or (call.get('result') or {}).get('code')!='explicit_action_required':
        raise TaskError('operation_not_reviewable',status=409)
    with transaction() as c:
        store.load(p,cid,c,True)
        row=c.execute('SELECT resolution FROM pg02.dialogue_calls WHERE turn_id=%s AND call_id=%s FOR UPDATE',(tid,call_id)).fetchone()
        resolution=row['resolution']
        if resolution:
            if resolution.get('kind')=='explicit_action' and resolution.get('state')=='completed':return resolution['result']
            raise TaskError('original_action_outcome_unknown',status=409)
        unresolved=c.execute("SELECT 1 FROM pg02.dialogue_calls a JOIN pg02.dialogue_turns t ON t.id=a.turn_id WHERE t.dialogue_id=%s AND a.arguments->>'reference'=%s AND a.resolution->>'kind'='explicit_action' AND a.resolution->>'state'='unknown' LIMIT 1",(cid,call['arguments']['reference'])).fetchone()
        if unresolved:raise TaskError('original_action_outcome_unknown',status=409)
        c.execute('UPDATE pg02.dialogue_calls SET resolution=%s WHERE turn_id=%s AND call_id=%s',
                  (Jsonb(dict(kind='explicit_action',state='unknown')),tid,call_id))
    try:
        with explicit_review(),tool_scope(cid,tid,call_id):result=project(execute(call['name'],call['arguments'],p,sid))
    except TaskError as exc:result=feedback('denied',exc.code)
    except Exception:raise TaskError('original_action_outcome_unknown',status=409) from None
    with transaction() as c:
        c.execute('UPDATE pg02.dialogue_calls SET resolution=%s WHERE turn_id=%s AND call_id=%s',
                  (Jsonb(dict(kind='explicit_action',state='completed',result=result)),tid,call_id))
    return result
