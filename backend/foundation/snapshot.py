"""Bounded, read-through R2 snapshot. Association metadata is the only new state."""
from datetime import datetime, timezone
import hashlib
import re
from lifecycle.store import db, TaskError, TaskStore
from lifecycle.service import public as task_public
from .drafts import GoalStore, public, inspect

TERMINAL={'completed','cancelled','failed'}
def session_reference(sid): return hashlib.sha256(sid.encode()).hexdigest()

def resolve(principal,reference):
    if reference.startswith('tg_'):
        value=GoalStore().load(principal,reference)
        if value['kind']=='result':
            return dict(kind='result',reference=reference,status=value['status'],revision=value['revision'],
                produced_at=value['fields'].get('produced_at'),source=value['fields'].get('source','legacy_verified_query'),
                historical=True,next_actions=['result.read'])
        return dict(**public(value),**inspect(principal,value))
    record=TaskStore().load(reference,principal)
    unknown=any(o.state in {'unknown','inflight'} for o in record.operations.values())
    actions=(['task.verify'] if unknown else ['task.read'] if record.status in TERMINAL else
        ['task.read','task.cancel','task.modify'] +
        (['task.resume'] if record.status in {'paused','active'} else
         ['task.pause','secure.confirm'] if record.status=='waiting' else []))
    return dict(kind='task',reference=reference,**task_public(record),next_actions=actions,
        execution_outcome='unknown' if unknown else record.status)

def associate(principal,sid,reference):
    value=resolve(principal,reference)
    with db() as c:
        c.execute('INSERT INTO session_links(owner_id,principal_id,session_hash,reference,kind,focused,associated_at) '
            'VALUES(?,?,?,?,?,false,?) ON CONFLICT(owner_id,principal_id,session_hash,reference) DO NOTHING',
            (principal.user_id,principal.principal_id,session_reference(sid),reference,value['kind'],datetime.now(timezone.utc).isoformat()))

def set_focus(principal,sid,reference):
    if reference: associate(principal,sid,reference)
    with db() as c:
        # Serialize simultaneous focus changes for this owner using the existing UoW.
        c.execute('BEGIN IMMEDIATE')
        params=(principal.user_id,principal.principal_id,session_reference(sid))
        c.execute('UPDATE session_links SET focused=false WHERE owner_id=? AND principal_id=? AND session_hash=?',params)
        if reference:
            c.execute('UPDATE session_links SET focused=true WHERE owner_id=? AND principal_id=? AND session_hash=? AND reference=?',(*params,reference))

def snapshot(principal,sid,*,limit=8,session_hash=None):
    if not isinstance(limit,int) or isinstance(limit,bool) or not 1<=limit<=30: raise TaskError('invalid_limit')
    key=session_hash or session_reference(sid)
    if not re.fullmatch('[0-9a-f]{64}',key): raise TaskError('invalid_session_reference')
    with db() as c:
        rows=c.execute('SELECT reference,focused,associated_at FROM session_links WHERE owner_id=? AND principal_id=? AND session_hash=? '
            'ORDER BY focused DESC,associated_at DESC,reference LIMIT ?',
            (principal.user_id,principal.principal_id,key,limit+1)).fetchall()
    # No association can be read from another owner, even given a session hash.
    if session_hash and not rows: raise TaskError('reference_not_found',status=404)
    items=[]; focus=None
    for row in rows[:limit]:
        value=resolve(principal,row['reference'])
        value['associated_at']=row['associated_at']
        if row['focused']: focus=value
        items.append(value)
    from .context_contracts import ContextSnapshot
    return ContextSnapshot.model_validate(dict(protocol='r1-context-1',session_reference=key,user_id=principal.user_id,
        focus=focus,items=items,unfinished=[dict(reference=v['reference'],kind=v['kind'],status=v.get('status'),revision=v.get('revision')) for v in items if v['kind']!='result' and v.get('status') not in TERMINAL|{'closed','snapshot'}],truncated=len(rows)>limit,limit=limit,
        server_time=datetime.now(timezone.utc).isoformat(),business_timezone='Asia/Shanghai',
        scope='read-through state; focus is not transaction authorization; results are historical')).model_dump(mode='json')
