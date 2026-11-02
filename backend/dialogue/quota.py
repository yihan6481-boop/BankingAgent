"""Account/day admission; authenticated PG context is the only owner source.

Every entry point takes this transaction lock before checking account-wide IDs.
Slot uniqueness/checks also enforce the hard 200 maximum at the DB boundary.
No model-call, worker, resume or safe-operation code reserves a new admission.
"""
from datetime import datetime, timedelta, timezone
from contextvars import ContextVar
from zoneinfo import ZoneInfo
import hashlib
from uuid import uuid4
from psycopg.types.json import Jsonb
from lifecycle.store import TaskError
from storage.pg02 import current_context
from .store import transaction

SHANGHAI=ZoneInfo('Asia/Shanghai')
LIMIT=200
legacy_admitted=ContextVar('legacy_llm_admitted',default=False)

def now():return datetime.now(timezone.utc)

def clock():
    instant=now()
    if instant.tzinfo is None:raise TaskError('quota_clock_invalid',status=503)
    local=instant.astimezone(SHANGHAI)
    reset=datetime.combine(local.date()+timedelta(days=1),datetime.min.time(),SHANGHAI)
    return instant,local.date(),reset.isoformat()

def lock(c):
    # Identity is verified by the HTTP middleware / principal scope, never body.
    owner=current_context().owner_uuid
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,1700200))',(str(owner),))

def existing(c,client_id):
    return c.execute('SELECT * FROM pg02.daily_message_admissions WHERE owner_uuid=core.require_owner() AND client_message_id=%s',(client_id,)).fetchone()

def summary(c,day=None,reset=None):
    if day is None:_,day,reset=clock()
    used=c.execute('SELECT count(*) n FROM pg02.daily_message_admissions WHERE owner_uuid=core.require_owner() AND quota_day=%s',(day,)).fetchone()['n']
    return dict(limit=LIMIT,used=used,remaining=max(0,LIMIT-used),date=day.isoformat(),timezone='Asia/Shanghai',resets_at=reset)

class QuotaExceeded(TaskError):
    def __init__(self,value):
        self.quota=value
        super().__init__('daily_message_quota_exhausted',
            '今日已用200条，剩余0条；北京时间 '+value['resets_at']+' 重置。已有任务核验、确认、取消和记忆删除仍可使用。',status=429)

def admit(c,client_id,input_hash,*,cid=None,tid=None,legacy_reference=None):
    instant,day,reset=clock()
    value=summary(c,day,reset)
    if value['remaining']==0:raise QuotaExceeded(value)
    c.execute('''INSERT INTO pg02.daily_message_admissions
        (client_message_id,accepted_at,quota_day,slot,input_hash,dialogue_id,turn_id,legacy_reference)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
        (client_id,instant,day,value['used']+1,input_hash,cid,tid,legacy_reference))

def status():
    with transaction() as c:return summary(c)

def legacy_begin(client_id,text):
    hashed=hashlib.sha256(text.encode()).hexdigest()
    with transaction() as c:
        lock(c);old=existing(c,client_id)
        if old:
            if old['input_hash']!=hashed or not old['legacy_reference']:raise TaskError('client_message_conflict',status=409)
            if old['legacy_response'] is None:
                raise TaskError('message_outcome_unknown','原消息已接纳，结果尚未核实；请查询原任务状态，不能自动重发。',status=409)
            return old['legacy_response']
        admit(c,client_id,hashed,legacy_reference='legacy_'+uuid4().hex)
    return None

def legacy_finish(client_id,response):
    with transaction() as c:
        c.execute('UPDATE pg02.daily_message_admissions SET legacy_response=%s WHERE owner_uuid=core.require_owner() AND client_message_id=%s AND legacy_reference IS NOT NULL',
            (Jsonb(response),client_id))

def legacy_call(client_id,text,callback):
    """Preserve the old endpoint's envelope/status, never replay uncertain work."""
    import json
    from fastapi.responses import JSONResponse
    client_id=client_id or 'legacy_'+uuid4().hex
    saved=legacy_begin(client_id,text)
    if saved is not None:return JSONResponse(saved['content'],status_code=saved['http_status'])
    token=legacy_admitted.set(True)
    try:response=callback()
    finally:legacy_admitted.reset(token)
    if isinstance(response,JSONResponse):content=json.loads(response.body);status_code=response.status_code
    else:content=response.model_dump(mode='json');status_code=200
    legacy_finish(client_id,dict(content=content,http_status=status_code))
    return response
