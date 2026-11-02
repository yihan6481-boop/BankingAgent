"""Persistent support. Seat tokens never grant payment or customer identity."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
import json, os, secrets
from pydantic import BaseModel, ConfigDict
from typing import Literal
from lifecycle.store import TaskError, TaskStore
from storage.pg02 import current_context, store, verified_session, principal_scope
from security.redaction import redact

class SupportView(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    kind:Literal['human_service']='human_service'
    state:Literal['idle','waiting','connected','no_agents','disconnected','ended']='idle'
    automation_paused:bool=False
    human_connected:bool=False
    case_id:str|None=None

def now(): return datetime.now(timezone.utc)
def hashed(value): return sha256(value.encode()).hexdigest()

@contextmanager
def connection(token=None):
    with store()._pool.connection() as c,c.transaction():
        c.execute("SELECT set_config('banking.owner_id',%s,true)",(str(current_context().owner_uuid) if token is None else '',))
        c.execute("SELECT set_config('assistance.token',%s,true)",(token or '',))
        yield c

def available(c,seat=None):
    return bool(c.execute('SELECT 1 FROM assistance.seat_sessions WHERE expires_at>now() AND heartbeat>now()-interval \'45 seconds\' AND online AND (%s::text IS NULL OR seat=%s) LIMIT 1',(seat,seat)).fetchone())

def case_view(row,c):
    if not row:return SupportView().model_dump()
    state=row['state']
    if state=='waiting' and not available(c):state='no_agents'
    if state=='connected' and not available(c,row['seat']):state='disconnected'
    return SupportView(state=state,case_id=str(row['id']),automation_paused=True,human_connected=state=='connected').model_dump()

def status(sid):
    from security.payment_auth import enabled
    if not enabled():return SupportView().model_dump()
    with connection() as c:
        # Schema29 has the original authoritative pause flag, but predates the
        # optional persistent seat channel. Never turn a missing modern table
        # into a default "unpaused" state, nor ignore damaged modern schemas.
        if c.execute("SELECT to_regclass('assistance.cases') AS table_name").fetchone()['table_name'] is None:
            version=c.execute('SELECT max(version) AS version FROM ops.schema_migrations').fetchone()['version']
            if version!=29:raise TaskError('support_state_unavailable','暂时无法核实人工服务暂停状态，本次消息未受理。请稍后重试。',status=503)
            paused=c.execute('SELECT paused FROM mobile_security.support_requests WHERE session_id=%s',(sid,)).fetchone()
            value=SupportView(automation_paused=bool(paused and paused['paused'])).model_dump()
            return dict(value,support_available=False)
        row=c.execute('SELECT * FROM assistance.cases WHERE session_id=%s ORDER BY created_at DESC LIMIT 1',(sid,)).fetchone()
        value=case_view(row,c)
        paused=c.execute('SELECT paused FROM mobile_security.support_requests WHERE session_id=%s',(sid,)).fetchone()
        value['automation_paused']=bool(paused and paused['paused'])
        return value

def request(sid):
    from execution.sessions import session_lock,session_for
    with session_lock(sid),connection() as c:
        owner=current_context().owner_uuid
        row=c.execute("SELECT * FROM assistance.cases WHERE session_id=%s AND state!='ended' FOR UPDATE",(sid,)).fetchone()
        if row is None:
            tid=session_for(sid).lifecycle_id
            row=c.execute("INSERT INTO assistance.cases(id,owner_uuid,session_id,state,task_id) VALUES(%s,%s,%s,'waiting',%s) RETURNING *",(uuid4(),owner,sid,tid)).fetchone()
        c.execute('''INSERT INTO mobile_security.support_requests(session_id,paused) VALUES(%s,true)
          ON CONFLICT(owner_uuid,session_id) DO UPDATE SET paused=true,updated_at=now()''',(sid,))
        return case_view(row,c)

def customer_case(c,sid,case_id=None):
    row=c.execute('SELECT * FROM assistance.cases WHERE session_id=%s AND (%s::uuid IS NULL OR id=%s::uuid) ORDER BY created_at DESC LIMIT 1 FOR UPDATE',(sid,case_id,case_id)).fetchone()
    if not row:raise TaskError('support_case_not_found',status=404)
    return row

def messages(c,row):
    rows=c.execute('SELECT id,sender,text,created_at FROM (SELECT id,sender,text,created_at FROM assistance.messages WHERE case_id=%s ORDER BY id DESC LIMIT 200) q ORDER BY id',(row['id'],)).fetchall()
    return [dict(r,text=redact(r['text'])) for r in rows]

def customer_view(sid):
    with connection() as c:
        row=customer_case(c,sid)
        return dict(**case_view(row,c),messages=messages(c,row),task_id=row['task_id'])

def append(c,row,sender,text,client_id):
    if row['state']=='ended':raise TaskError('support_ended',status=409)
    from dialogue.projection import user_text
    clean=user_text(text).strip()
    if not clean:raise TaskError('empty_message',status=422)
    old=c.execute('SELECT text FROM assistance.messages WHERE case_id=%s AND sender=%s AND client_id=%s',(row['id'],sender,client_id)).fetchone()
    if old and old['text']!=clean:raise TaskError('support_message_conflict',status=409)
    c.execute('INSERT INTO assistance.messages(case_id,owner_uuid,sender,client_id,text) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(case_id,sender,client_id) DO NOTHING',(row['id'],row['owner_uuid'],sender,client_id,clean))

def customer_message(sid,text,client_id,case_id=None):
    with connection() as c:
        row=customer_case(c,sid,case_id)
        append(c,row,'customer',text,client_id)
        return dict(**case_view(row,c),messages=messages(c,row),task_id=row['task_id'])

def end(sid,resume=False):
    from execution.sessions import session_lock
    with session_lock(sid),connection() as c:
        row=customer_case(c,sid)
        if row['state']!='ended':
            append(c,row,'system','人工服务已结束。请核对原任务；点击“恢复 AI 助手”后继续。',str(uuid4()))
            c.execute("UPDATE assistance.cases SET state='ended',ended_at=now() WHERE id=%s",(row['id'],))
        if resume:c.execute('UPDATE mobile_security.support_requests SET paused=false,updated_at=now() WHERE session_id=%s',(sid,))
    return status(sid)

def seat_config():
    path=Path(os.getenv('BA_SUPPORT_CREDENTIAL_FILE','')).resolve()
    root=Path(__file__).resolve().parents[2]
    if path.is_relative_to(root) or not path.is_file():raise TaskError('support_not_configured',status=503)
    return json.loads(path.read_text('utf8'))

def login(username,password,source):
    cfg=seat_config()
    from security.identity import verify_password
    # Uniform account/IP throttle, no credential or IP text in audit.
    key=hashed(username+':'+source)
    with store()._pool.connection() as c,c.transaction():
        c.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(key,))
        row=c.execute('SELECT * FROM assistance.login_attempts WHERE key=%s',(key,)).fetchone()
        if row and row['started_at']>now()-timedelta(minutes=5) and row['failures']>=5:raise TaskError('seat_login_cooldown',status=429)
        valid=secrets.compare_digest(username,cfg['username']) and verify_password(password,cfg['salt'],cfg['password_hash'],cfg['iterations'])
        if not valid:
            c.execute('''INSERT INTO assistance.login_attempts VALUES(%s,1,now()) ON CONFLICT(key) DO UPDATE SET
              failures=CASE WHEN assistance.login_attempts.started_at<now()-interval '5 minutes' THEN 1 ELSE assistance.login_attempts.failures+1 END,
              started_at=CASE WHEN assistance.login_attempts.started_at<now()-interval '5 minutes' THEN now() ELSE assistance.login_attempts.started_at END''',(key,))
        else:
            token=secrets.token_urlsafe(40)
            c.execute('DELETE FROM assistance.login_attempts WHERE key=%s',(key,))
            c.execute('UPDATE assistance.seat_sessions SET online=false,expires_at=now() WHERE seat=%s',(cfg['username'],))
            c.execute('INSERT INTO assistance.seat_sessions(token_hash,seat,expires_at) VALUES(%s,%s,%s)',(hashed(token),cfg['username'],now()+timedelta(hours=8)))
    if not valid:raise TaskError('seat_unauthorized',status=401)
    return dict(token=token,seat=cfg['username'])

def authenticate(token):
    digest=hashed(token)
    with store()._pool.connection() as c:
        row=c.execute('SELECT seat FROM assistance.seat_sessions WHERE token_hash=%s AND expires_at>now()',(digest,)).fetchone()
    if not row:raise TaskError('seat_unauthorized',status=401)
    return digest,row['seat']

def seat_presence(auth,online):
    token,seat=auth
    with connection(token) as c:c.execute('UPDATE assistance.seat_sessions SET heartbeat=now(),online=%s WHERE token_hash=%s',(online,token))
    return dict(online=online,seat=seat)

def queue(auth):
    token,seat=auth
    with connection(token) as c:
        return dict(cases=[dict(id=str(r['id']),state=r['state'],created_at=r['created_at'],assigned_to_me=r['seat']==seat,has_task=bool(r['task_id'])) for r in c.execute("SELECT * FROM assistance.cases WHERE state='waiting' OR (state='connected' AND seat=%s) ORDER BY created_at LIMIT 100",(seat,)).fetchall()])

def owned_case(c,case_id,seat):
    row=c.execute('SELECT * FROM assistance.cases WHERE id=%s FOR UPDATE',(case_id,)).fetchone()
    if not row or row['seat']!=seat or row['state']!='connected':raise TaskError('seat_case_forbidden',status=403)
    return row

def accept(auth,case_id):
    token,seat=auth
    with connection(token) as c:
        if not available(c,seat):raise TaskError('seat_offline',status=409)
        row=c.execute("UPDATE assistance.cases SET state='connected',seat=%s WHERE id=%s AND state='waiting' RETURNING *",(seat,case_id)).fetchone()
        if row is None:
            row=owned_case(c,case_id,seat)
        else:append(c,row,'system','客服已接入。AI 已暂停自动办理；已提交交易继续核实原结果。',str(uuid4()))
    return seat_view(auth,case_id)

def seat_view(auth,case_id):
    token,seat=auth
    with connection(token) as c:
        row=owned_case(c,case_id,seat)
        audit=c.execute('SELECT seat,tool,task_id,outcome,created_at FROM assistance.audit WHERE case_id=%s ORDER BY id DESC LIMIT 30',(row['id'],)).fetchall()
        return dict(id=str(row['id']),state=case_view(row,c)['state'],messages=messages(c,row),task_id=row['task_id'],audit=audit)

def seat_message(auth,case_id,text,client_id):
    token,seat=auth
    with connection(token) as c:append(c,owned_case(c,case_id,seat),'seat',text,client_id)
    return seat_view(auth,case_id)

def seat_end(auth,case_id):
    token,seat=auth
    with connection(token) as c:
        row=owned_case(c,case_id,seat)
        append(c,row,'system','客服已结束服务。请核对原任务；点击“恢复 AI 助手”后继续。',str(uuid4()))
        c.execute("UPDATE assistance.cases SET state='ended',ended_at=now() WHERE id=%s",(row['id'],))
    return dict(state='ended',automation_paused=True)

def _tool(auth,case_id,name):
    """Only linked-task inspection and re-preview, no arbitrary arguments or SQL."""
    token,seat=auth
    with connection(token) as c:row=dict(owned_case(c,case_id,seat))
    if name not in {'task_status','task_verify','task_resume'}:raise TaskError('seat_tool_forbidden',status=403)
    if not row['task_id']:raise TaskError('support_no_related_task',status=409)
    outcome='failed'
    try:
        from security.service import get_security_services
        from execution.sessions import restore_principal
        from security.totp import independent_protocol
        context,credential=verified_session(row['session_id'],get_security_services().now_provider())
        if str(context.owner_uuid)!=str(row['owner_uuid']):raise TaskError('seat_case_forbidden',status=403)
        with principal_scope(context):
            p=restore_principal(context,credential)
            from security.tool_guard import guarded_tool_call
            from lifecycle.service import public
            def operation():
                record=TaskStore().load(row['task_id'],p)
                if name=='task_resume':
                    from lifecycle.application import perform
                    from lifecycle.models import RevisionRequest
                    flag=independent_protocol.set(True)
                    try:perform(row['session_id'],row['task_id'],RevisionRequest(revision=record.revision),'resume',p)
                    finally:independent_protocol.reset(flag)
                    record=TaskStore().load(row['task_id'],p)
                    from foundation.contracts import feedback
                    if any(o.state in {'unknown','inflight'} for o in record.operations.values()):
                        return feedback('commit_unknown','commit_unknown',data=public(record))
                    return feedback('business_condition','task_resume_failed',data=None) if record.status=='failed' else feedback(data=public(record))
                from foundation.gateway import run
                from foundation.contracts import Call
                return run(Call(operation='task.verify' if name=='task_verify' else 'task.read',reference=row['task_id']),p,row['session_id'])
            result=guarded_tool_call(name,operation,read_only=name!='task_resume')
            verdict=tool_result(result,row['task_id'])
            outcome=verdict['tool_status']+(':'+verdict['code'] if verdict['code']!='ok' else '')
            if verdict['tool_status']!='completed':return verdict
            # Never send preview IDs, challenges, auth or credential-bearing task JSON.
            record=TaskStore().load(row['task_id'],p)
            value=dict(status=record.status,revision=record.revision,task_id=record.task.task_id,
                       steps=[dict(tool=s.tool_name,status=s.status) for s in record.task.steps],
                       tool_status='completed',code='ok',
                       message='已恢复原待办，请由用户核对预览并完成支付验证与最终确认。' if name=='task_resume' else '已读取并核实原任务当前状态。付款仍由用户完成支付验证与最终确认。')
            receipts=[]
            with connection() as c:
                for step in record.task.steps:
                    if step.tool_name!='transfer_money':continue
                    args=step.arguments
                    recipient=c.execute('SELECT name FROM pg02.recipients WHERE recipient_id=%s',(args.get('recipient_id'),)).fetchone() if args.get('recipient_id') else None
                    receipts.append(dict(recipient=redact(recipient['name'] if recipient else str(args.get('recipient_name','待核实收款人'))),amount=str(args.get('amount','—')),status=step.status))
            value['receipts']=receipts
            outcome='completed'
            return value
    except TaskError as exc:
        outcome='rejected:'+exc.code
        raise
    except Exception as exc:
        from storage.errors import CommitUnknown
        unknown=isinstance(exc,CommitUnknown) or getattr(exc,'pg02_commit_unknown',False)
        verdict=tool_result(dict(status='commit_unknown' if unknown else 'service_failure',code='commit_unknown' if unknown else 'service_unavailable'),row['task_id'])
        outcome=verdict['tool_status']+':'+verdict['code']
        return verdict
    finally:
        with connection(token) as c:c.execute('INSERT INTO assistance.audit(case_id,owner_uuid,seat,tool,task_id,outcome) VALUES(%s,%s,%s,%s,%s,%s)',(row['id'],row['owner_uuid'],seat,name,row['task_id'],outcome))


def tool_result(result,task_id):
    """Positive protocol check shared by all three seat operations, fail closed."""
    from security.tool_guard import outcome as protection_outcome
    category,code=protection_outcome(result)
    status=result.get('status') if isinstance(result,dict) else None
    code=redact(str(code or 'tool_result_invalid'))
    if category=='outcome_unknown':state='unknown'
    elif (isinstance(result,dict) and result.get('protocol')=='tool-foundation-1' and status in {'completed','read_ok'} and code=='ok'
          and isinstance(result.get('data'),dict) and isinstance(result['data'].get('task'),dict)
          and result['data']['task'].get('task_id')==task_id):state='completed'
    elif code=='tool_circuit_open':state='circuit_open'
    elif status=='conflict':state='conflict'
    elif status in {'denied','rejected','unsupported','invalid_parameters','ambiguous','missing_information','business_condition'}:state='rejected'
    elif category=='infrastructure_failed' or status in {'service_failure','internal_error'}:state='service_failure'
    else:state='unverified'
    messages={'completed':'工具已完成。','unknown':'原交易结果仍待核实。只能查询原任务，不能重建付款或更换幂等键。','circuit_open':'依赖暂时熔断，本次没有完成核实。稍后重新查询原任务。','conflict':'原任务状态有冲突，本次未完成核实。请先重新读取原任务。','rejected':'本次工具请求未获执行，尚未核实原任务。请核对原任务及服务权限。','service_failure':'服务暂不可用，本次没有完成核实。稍后重新查询原任务。','unverified':'工具返回未通过协议校验，尚未完成核实。请保留原任务。'}
    data=result.get('data') if isinstance(result,dict) else None
    retry_after=data.get('retry_after_seconds') if isinstance(data,dict) else None
    return dict(tool_status=state,status='unknown' if state=='unknown' else 'unverified',task_id=task_id,code=code,message=messages[state],receipts=[],retry_allowed=state in {'circuit_open','conflict','service_failure'},retry_after_seconds=retry_after if isinstance(retry_after,(int,float)) and not isinstance(retry_after,bool) else None)

def tool(auth,case_id,name):
    from execution.sessions import session_lock
    token,seat=auth
    with connection(token) as c:row=owned_case(c,case_id,seat)
    # Revalidate assignment inside the same business lock used by end/resume and
    # AI tools; a stale seat request cannot act after the customer returns to AI.
    with session_lock(row['session_id']):return _tool(auth,case_id,name)
