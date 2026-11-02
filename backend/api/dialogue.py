"""R2 authenticated HTTP/UI adapter; fixed engine, no legacy chat fallback."""
from typing import Literal
from fastapi import APIRouter,Depends,Header,HTTPException,Query,Request,Body
from pydantic import Field
from agent.schemas import StrictModel
from security.schemas import Principal
from security.service import get_security_services
from lifecycle.store import TaskStore,TaskError
from lifecycle.views import task_response
from lifecycle.application import perform
from lifecycle.models import RevisionRequest,TaskConfirmRequest
from execution.sessions import session_for,session_lock
from foundation.gateway import run
from foundation.contracts import Call
from dialogue.store import DialogueStore,transaction
from dialogue.loop import DialogueLoop
from dialogue.provider import DeepSeek
from dialogue.tools import clean_snapshot,task_allowed
from dialogue.projection import project,safe_text,user_text
from dialogue.state import snapshot
from security.totp import independent_protocol
from storage.pg02 import verified_session
import secrets


async def protocol():
    token=independent_protocol.set(True)
    try:yield
    finally:independent_protocol.reset(token)


def require_task_principal(session_id:str,authorization:str|None=Header(None)):
    if not authorization or not authorization.startswith('Bearer ') or not secrets.compare_digest(authorization[7:],session_id):raise HTTPException(401,'unauthorized')
    from security.identity import IdentityError
    try:context,row=verified_session(session_id,get_security_services().now_provider())
    except IdentityError as exc:raise HTTPException(401,exc.code) from None
    from execution.sessions import restore_principal
    return restore_principal(context,row)


router=APIRouter(prefix='/api/secure/sessions/{session_id}',dependencies=[Depends(protocol)],tags=['r2-dialogue'])

provider_factory=DeepSeek

@router.get('/message-quota')
def message_quota(session_id:str,request:Request,p:Principal=Depends(require_task_principal)):
    if request.query_params:raise HTTPException(422,'unknown_quota_query_fields')
    from dialogue.quota import status
    result=status()
    # The 200-message daily admission quota and the paid-call budget are two
    # independent layers; expose the budget without ever resetting its ledger.
    from dialogue.budget import Budget,BudgetError
    try:result['model_budget']=Budget().status()
    except BudgetError as error:result['model_budget']=dict(configured=False,reason=str(error))
    return result

class Send(StrictModel):
    client_message_id:str=Field(pattern=r'^[A-Za-z0-9_-]{8,100}$')
    message:str=Field(min_length=1,max_length=16000)

class Confirm(TaskConfirmRequest):preview_id:str=Field(pattern=r'^[a-f0-9]{64}$')
class Action(RevisionRequest):action:Literal['pause','resume','cancel']


@router.post('/dialogues')
def create_dialogue(session_id:str,p:Principal=Depends(require_task_principal)):
    return {'id':DialogueStore().create(p,session_id),'engine':'r2'}

@router.get('/dialogues')
def list_dialogues(session_id:str,p:Principal=Depends(require_task_principal)):
    with transaction() as c:
        rows=c.execute('SELECT id,created_at FROM pg02.dialogues WHERE owner_id=%s AND principal_id=%s ORDER BY created_at DESC LIMIT 30',(p.user_id,p.principal_id)).fetchall()
    return {'dialogues':rows}


def view(p,sid,cid,history_limit=50,before=None):
    store=DialogueStore();conversation=store.load(p,cid)
    state=snapshot(p,cid)
    turns,page=store.history(p,cid,limit=history_limit,before=before)
    from dialogue.context import MODEL_WINDOW_TURNS
    page['model_window_turns']=MODEL_WINDOW_TURNS
    from recall_bridge.service import configured
    if configured():
        from recall_bridge.context_guard import answer_view,unverified_answer
        projected=[]
        for turn in turns:
            try:answer=answer_view(p,turn)
            except Exception:
                answer=unverified_answer(turn)
            projected.append(dict(turn,answer=answer))
        turns=projected
    def rendered(t):
        from datetime import datetime,timezone
        lease=conversation.get('lease_until')
        recoverable=t['status']=='running' and (not lease or lease<=datetime.now(timezone.utc))
        from dialogue.projection import model_arguments
        calls=store.calls(t['id']);operations=[]
        from dialogue.read_results import effective,authorization_view,evidence_revision
        authorizations=[v for call in calls if (v:=authorization_view(call,p,cid,t['id'])) is not None]
        revision=evidence_revision(calls,p,cid,t['id'])
        unknown=any(call['status']!='completed' or call.get('outcome')=='unknown' and not call.get('resolution') for call in calls)
        blocked=t.get('usage',{}).get('authorization_answer_blocked') or {}
        requested=any((call.get('resolution') or {}).get('continuation_requested') for call in calls)
        answer_pending=bool(requested and revision and t.get('usage',{}).get('authorization_answer',{}).get('revision')!=revision and blocked.get('revision')!=revision and not unknown)
        from dialogue.business import READS
        query_calls=[call for call in calls if call['name'] in READS|{'cards_balance','recipient_candidates','query_execute','result_read','call_result_read'}]
        query_states=[(effective(call,p,cid,t['id']).get('result') or {}).get('status') for call in query_calls]
        effective_results={call['call_id']:project(effective(call,p,cid,t['id']).get('result')) for call in calls}
        calls=[dict(c,arguments=model_arguments(c['name'],c['arguments']),result=project(c.get('result')),
                    effective_result=effective_results[c['call_id']],
                    name=safe_text(c['name']),call_id=safe_text(c['call_id']),
                    resolution=dict(c['resolution'],result=project(c['resolution'].get('result'))) if c.get('resolution') else None) for c in calls]
        answer=safe_text(t['answer'])
        from dialogue.answer_contract import historical_display
        answer=historical_display(p,cid,t,answer)
        if t.get('usage',{}).get('r4_automatic_deferred'):
            answer+='\n\n本轮包含记忆更正或删除，未重复自动整理该轮原话；其他新背景请明确说“记住”。'
        if configured():
            from recall_bridge.answer_contract import states
            try:operations=states(p,calls)
            except Exception:operations=[dict(availability='unknown',reason='memory_metadata_unavailable')]
        answer_view=dict(body=answer)
        # Only split an exact, server-authored fallback. Never infer trust from
        # arbitrary model headings, or restore parts invalidated by memory reads.
        parts=t.get('usage',{}).get('r4_answer_parts') or []
        if t['error_code'] and t.get('usage',{}).get('answer_fallback')=='classified_failure_with_verified_evidence' and parts:
            texts=[safe_text(part['text']) for part in parts]
            if '\n\n'.join(texts)==answer:answer_view=dict(body='\n\n'.join(texts[:-1]),notice=texts[-1])
        return dict(id=t['id'],client_message_id=t['client_id'],number=t['number'],message=user_text(t['user_text']),
            answer=answer,answer_view=answer_view,status=t['status'],recoverable=recoverable,error_code=t['error_code'],progress=t['progress'],calls=calls,memory_operations=operations,
            query_authorizations=authorizations,authorization_answer_pending=answer_pending,
            authorization_answer_notice=('请先核实本轮结果未知的原操作，再整理答复。' if unknown and requested else '相关长期资料已变化，本次未自动继续答复。原查询结果仍可查看，银行数据需要时可重新只读查询。' if blocked.get('revision')==revision and blocked.get('reason')=='memory_changed_during_answer' else None),
            query_status=('partial' if 'completed' in query_states and any(v!='completed' for v in query_states) else 'available' if 'completed' in query_states else 'pending' if any(q['state']=='pending' for q in authorizations) else 'failed' if query_states else 'none'),
            can_rephrase=t['status']=='failed' and not unknown and not t['usage'].get('answer_retry') and t['error_code'] in {'answer_contract_invalid','answer_semantically_unverified','semantic_review_unavailable','semantic_review_protocol_invalid','semantic_review_budget_exhausted','answer_correction_service_failure','model_timeout','model_http_429','loop_budget_exhausted','no_progress','dialogue_service_failure'})
    from dialogue.quota import status
    return dict(id=cid,engine='r2',state=project(state),turns=[rendered(t) for t in turns],
                history=page,message_quota=status())



@router.get('/dialogues/{cid}')
def get_dialogue(session_id:str,cid:str,history_limit:int=Query(50,ge=1,le=200),
                 before:int|None=Query(None,ge=1),p:Principal=Depends(require_task_principal)):
    DialogueStore().rebind(p,cid,session_id)
    return view(p,session_id,cid,history_limit=history_limit,before=before)


class ApplyOperation(StrictModel):pass


@router.post('/dialogues/{cid}/turns/{tid}/operations/{call_id}/apply')
def apply_operation(session_id:str,cid:str,tid:str,call_id:str,body:ApplyOperation,p:Principal=Depends(require_task_principal)):
    from dialogue.effects import apply
    with session_lock(session_id):
        apply(p,session_id,cid,tid,call_id)
    return view(p,session_id,cid)


def message_refusal(p,cid,body,exc):
    """Bind a proven admission refusal; an existing/unknown turn stays protected."""
    from fastapi.responses import JSONResponse
    state='unknown'
    try:
        with transaction() as c:
            DialogueStore().load(p,cid,c)
            existing=c.execute('SELECT id FROM pg02.dialogue_turns WHERE dialogue_id=%s AND client_id=%s',(cid,body.client_message_id)).fetchone()
            state='existing_turn' if existing else 'not_accepted'
    except Exception:pass  # Missing storage/ownership evidence is never refusal proof.
    message={'automation_paused_for_support':'人工服务已暂停AI回复，请先在人工服务页面核对并恢复。','dialogue_busy':'当前对话有原轮次正在处理，请稍后再发送。'}.get(exc.code,exc.message)
    return JSONResponse(status_code=exc.status,content=dict(code=exc.code,message=message,data=dict(request_state=state,dialogue_id=cid,client_message_id=body.client_message_id)))


def message_preflight(p,session_id,cid,body):
    """A bound refusal before generation is distinct from an unknown submit."""
    try:
        DialogueStore().rebind(p,cid,session_id)
        from security.support import assert_new_automation
        with session_lock(session_id):assert_new_automation(session_id)
    except TaskError as exc:return message_refusal(p,cid,body,exc)
    return None


@router.post('/dialogues/{cid}/messages')
def send(session_id:str,cid:str,body:Send,p:Principal=Depends(require_task_principal)):
    refusal=message_preflight(p,session_id,cid,body)
    if refusal is not None:return refusal
    try:DialogueLoop(provider_factory()).run(p,session_id,cid,body.client_message_id,body.message)
    except TaskError as exc:return message_refusal(p,cid,body,exc)
    return view(p,session_id,cid)


@router.post('/dialogues/{cid}/messages/stream')
def stream_message(session_id:str,cid:str,body:Send,request:Request,p:Principal=Depends(require_task_principal)):
    """Real provider streaming is buffered behind the whole-answer review.

    Only reviewed, redacted, owner-scoped view data crosses this transport.
    Disconnecting never resubmits a business call; GET/same client ID recovers it.
    """
    import asyncio,json
    from fastapi.responses import StreamingResponse
    refusal=message_preflight(p,session_id,cid,body)
    if refusal is not None:return refusal

    def generate():
        provider=provider_factory()
        if isinstance(provider,DeepSeek):provider.streaming=True
        DialogueLoop(provider).run(p,session_id,cid,body.client_message_id,body.message)

    def frame(kind,**value):
        return 'data:'+json.dumps(dict(type=kind,client_message_id=body.client_message_id,**value),ensure_ascii=False,separators=(',',':'))+'\n\n'

    async def events():
        # to_thread copies the authenticated contextvars used by PG/RLS. A
        # disconnected subscriber is detached; the original bounded turn runs on.
        work=asyncio.create_task(asyncio.to_thread(generate))
        work.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
        yield frame('accepted',publication='reviewed_segments')
        try:
            while not work.done():
                if await request.is_disconnected():return
                await asyncio.wait({work},timeout=8)
                if not work.done():
                    require_task_principal(session_id,'Bearer '+session_id)
                    yield ': waiting for verified answer\n\n'
            await work
            require_task_principal(session_id,'Bearer '+session_id)
            value=await asyncio.to_thread(view,p,session_id,cid)
            turn=next((t for t in value['turns'] if t['client_message_id']==body.client_message_id),None)
            if turn and turn['status']!='running':
                # Paragraph boundaries keep Markdown, emoji, amounts and tails
                # intact. This is post-review delivery, not token latency.
                paragraphs=turn['answer'].split('\n\n');text='';sequence=0
                for index,paragraph in enumerate(paragraphs):
                    text+=('\n\n' if index else '')+paragraph;sequence+=1
                    require_task_principal(session_id,'Bearer '+session_id)
                    yield frame('answer',sequence=sequence,text=text,done=index==len(paragraphs)-1,publication='reviewed_segments')
                    if index<min(7,len(paragraphs)-1):await asyncio.sleep(.02)
            yield frame('complete',dialogue=value,publication='reviewed_segments')
        except asyncio.CancelledError:raise
        except TaskError as exc:
            try:require_task_principal(session_id,'Bearer '+session_id)
            except HTTPException:
                yield frame('error',code='authentication_expired',message='登录凭据已失效，请重新登录后读取原状态。');return
            refusal=await asyncio.to_thread(message_refusal,p,cid,body,exc)
            yield frame('error',**json.loads(refusal.body),status=refusal.status_code)
        except Exception:
            # Details stay in the turn's classified error; never serialize an
            # exception carrying headers, tool parameters or model internals.
            yield frame('error',code='stream_delivery_failed',message='本轮回复接收未完成，请核实原轮次；不会自动重放业务。')

    return StreamingResponse(events(),media_type='text/event-stream',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no','X-Content-Type-Options':'nosniff'})


@router.post('/dialogues/{cid}/cancel')
def cancel(session_id:str,cid:str,p:Principal=Depends(require_task_principal)):
    DialogueStore().cancel(p,cid)
    return {'message':'已请求停止等待；已经完成的业务操作不会撤销，请同步真实状态。'}


@router.post('/dialogues/{cid}/turns/{tid}/resume')
def resume_turn(session_id:str,cid:str,tid:str,p:Principal=Depends(require_task_principal)):
    DialogueStore().rebind(p,cid,session_id)
    from security.support import assert_new_automation
    with session_lock(session_id):
        assert_new_automation(session_id)
    DialogueLoop(provider_factory()).run(p,session_id,cid,'','',resume_turn=tid)
    return view(p,session_id,cid)


class QueryScope(StrictModel):
    call_id:str=Field(min_length=1,max_length=100)
    scope_id:str=Field(pattern=r'^[a-f0-9]{64}$')

class AuthorizeQueries(StrictModel):
    queries:list[QueryScope]=Field(default_factory=list,max_length=8)


def continue_authorized(p,session_id,cid,tid):
    from dialogue.read_results import evidence_revision
    s=DialogueStore();s.load(p,cid)
    with transaction() as c:
        turn=c.execute('SELECT * FROM pg02.dialogue_turns WHERE id=%s AND dialogue_id=%s',(tid,cid)).fetchone()
    if not turn:raise TaskError('turn_not_found',status=404)
    revision=evidence_revision(s.calls(tid),p,cid,tid)
    if revision:
        try:DialogueLoop(provider_factory()).run(p,session_id,cid,'','',resume_turn=tid,answer_request_id='authorization_'+revision,authorization_revision=revision)
        except TaskError as exc:
            # A later active question keeps its lease. The pending original
            # answer can be continued when idle, without another query/grant.
            if exc.code not in {'dialogue_busy','answer_retry_requires_verification','memory_changed_during_answer'}:raise
            if exc.code!='dialogue_busy':
                from psycopg.types.json import Jsonb
                with transaction() as c:
                    s.load(p,cid,c)
                    c.execute('UPDATE pg02.dialogue_turns SET usage=usage || %s WHERE id=%s AND dialogue_id=%s',(Jsonb(dict(authorization_answer_blocked=dict(revision=revision,reason=exc.code))),tid,cid))


def view_with_turn(p,sid,cid,tid):
    value=view(p,sid,cid)
    if not any(t['id']==tid for t in value['turns']):
        with transaction() as c:
            DialogueStore().load(p,cid,c)
            turn=c.execute('SELECT number FROM pg02.dialogue_turns WHERE id=%s AND dialogue_id=%s',(tid,cid)).fetchone()
        if turn:value['turns']=[*view(p,sid,cid,history_limit=1,before=turn['number']+1)['turns'],*value['turns']]
    return value


@router.post('/dialogues/{cid}/turns/{tid}/reads/{call_id}/authorize')
def authorize_read(session_id:str,cid:str,tid:str,call_id:str,body:AuthorizeQueries|None=Body(None),p:Principal=Depends(require_task_principal)):
    # UI-only explicit consent for the exact saved query. No model authorize tool.
    from api.read import get_read_only_service
    from api.schemas import ReadRequest
    from foundation.contracts import feedback
    from psycopg.types.json import Jsonb
    from datetime import datetime,timezone
    from dialogue.read_results import binding,eligible,authorization_view,checked_result
    s=DialogueStore();s.rebind(p,cid,session_id)
    selected=body.queries if body and body.queries else [QueryScope(call_id=call_id,scope_id=binding(p,cid,tid,s.call_read(p,cid,tid,call_id)))]
    if call_id not in {q.call_id for q in selected} or len({q.call_id for q in selected})!=len(selected):raise TaskError('invalid_query_authorization')
    # Serialize across sessions/tabs using the owned conversation row. All
    # scopes are checked before reading any, so a mismatched batch is atomic.
    with session_lock(session_id),transaction() as c:
        s.load(p,cid,c,True)
        calls=[]
        for query in selected:
            a=c.execute('SELECT a.* FROM pg02.dialogue_calls a JOIN pg02.dialogue_turns t ON t.id=a.turn_id WHERE t.dialogue_id=%s AND t.id=%s AND a.call_id=%s FOR UPDATE OF a',(cid,tid,query.call_id)).fetchone()
            if not a:raise TaskError('reference_not_found',status=404)
            if not eligible(a) or not secrets.compare_digest(query.scope_id,binding(p,cid,tid,a)):raise TaskError('query_scope_mismatch')
            state=authorization_view(a,p,cid,tid)['state']
            if state not in {'pending','authorized'}:raise TaskError('query_authorization_'+state)
            calls.append(a)
        for a in calls:
            if a.get('resolution'):continue  # Exact successful cached snapshot.
            out=None;failure_kind=None
            try:out=get_read_only_service().run(session_id,ReadRequest(tool=a['name'],arguments=a['arguments'],authorize_query=True),principal=p)
            except Exception as exc:failure_kind=type(exc).__name__
            require_task_principal(session_id,'Bearer '+session_id)
            now=datetime.now(timezone.utc).isoformat()
            if out is None or out.error or out.result is None:
                resolution=dict(kind='authorized_query',state='failed',binding=binding(p,cid,tid,a),authorized_at=now,
                    result=feedback('service_failure','authorized_query_failed'),failure_phase='authorized_query',failure_kind=failure_kind,
                    error_code=safe_text(out.error.detail_code or out.error.code) if out is not None and out.error else 'query_service_failure' if failure_kind else 'query_result_missing')
            else:
                result=feedback(data=out.result.model_dump(mode='json')|{'queried_at':now,'historical':True})
                resolution=dict(kind='authorized_query',state='completed',binding=binding(p,cid,tid,a),authorized_at=now,continuation_requested=True,result=project(result))
                if checked_result(dict(a,resolution=resolution),p,cid,tid) is None:
                    resolution=dict(kind='authorized_query',state='failed',binding=binding(p,cid,tid,a),authorized_at=now,error_code='query_result_schema_invalid',failure_phase='authorized_query',result=feedback('service_failure','authorized_query_failed'))
            c.execute('UPDATE pg02.dialogue_calls SET resolution=%s WHERE turn_id=%s AND call_id=%s',(Jsonb(resolution),tid,a['call_id']))
    continue_authorized(p,session_id,cid,tid)
    return view_with_turn(p,session_id,cid,tid)


@router.post('/dialogues/{cid}/turns/{tid}/authorized-answer')
def authorized_answer(session_id:str,cid:str,tid:str,p:Principal=Depends(require_task_principal)):
    DialogueStore().rebind(p,cid,session_id)
    continue_authorized(p,session_id,cid,tid)
    return view_with_turn(p,session_id,cid,tid)


class Rephrase(StrictModel):
    request_id:str=Field(pattern=r'^[A-Za-z0-9_-]{8,100}$')


@router.post('/dialogues/{cid}/turns/{tid}/answer')
def rephrase_answer(session_id:str,cid:str,tid:str,body:Rephrase,p:Principal=Depends(require_task_principal)):
    DialogueStore().rebind(p,cid,session_id)
    loop=DialogueLoop(provider_factory())
    result=loop.run(p,session_id,cid,'','',resume_turn=tid,answer_request_id=body.request_id)
    value=view(p,session_id,cid)
    if not any(t['id']==tid for t in value['turns']):
        old=view(p,session_id,cid,history_limit=1,before=result['number']+1)
        value['turns']=[*old['turns'],*value['turns']]
    return value


@router.get('/dialogues/{cid}/ui-state')
def ui_state(session_id:str,cid:str,p:Principal=Depends(require_task_principal)):
    from recall_bridge.service import configured
    DialogueStore().load(p,cid)
    session=session_for(session_id)
    with session_lock(session_id):
        refs={r['reference'] for r in DialogueStore().links(p,cid)}
        record=task_allowed(p,session.lifecycle_id) if session.lifecycle_id and session.lifecycle_id in refs else None
        if record is None:
            linked=[task_allowed(p,r['reference']) for r in DialogueStore().links(p,cid) if r['kind']=='task']
            active=[r for r in linked]
            active=[r for r in active if r.status not in {'completed','cancelled','failed'}]
            if len(active)==1:record=active[0]
            elif not active and linked:record=linked[-1]
        # A restored dialogue can select a task while the session is bound to a
        # different quick task. Never borrow that task's preview or challenge.
        from lifecycle.service import matches_preview
        pending=session.pending_action
        bound=bool(record and session.lifecycle_id==record.task.task_id
                   and record.status in {'active','waiting'} and pending
                   and matches_preview(record,pending.lifecycle_binding))
        task=task_response(record,session if bound else None) if record else None
        factor=get_security_services().totp.status(p)
        from api.ui_integration import display_result
        results=[]
        if record:
            for step in record.task.steps:
                if step.status == 'completed':
                    item=display_result(step.tool_name,step.result,step.purpose,step.step_id)
                    if item is not None:results.append(item.model_dump(mode='json'))
        status={'active':'executing','waiting':'need_confirmation','completed':'success',
                'cancelled':'cancelled','failed':'failed','paused':'paused',
                'scheduled':'scheduled','manual':'manual'}.get(record.status,'ready') if record else 'ready'
        if task and task['preview']:
            preview=task['preview'];challenge=task.get('challenge')
            from datetime import datetime
            if preview.get('expires_at') and datetime.fromisoformat(preview['expires_at'].replace('Z','+00:00'))<=get_security_services().strong_auth.now_provider():
                status='expired'
            elif preview.get('required_authentication')=='totp' and (not challenge or challenge['status']!='verified'):
                status='need_verification'
            else:status='need_confirmation'
        return dict(contract_version='ui-integration-1.0',display_name=p.display_name,status=status,
                    message='当前真实业务状态',missing_fields=[],task=task,results=results,factor=factor,conversation_id=cid,
                    memory_status='enabled' if configured() else 'retired')


@router.post('/dialogues/{cid}/tasks/{tid}/confirm')
def confirm(session_id:str,cid:str,tid:str,body:Confirm,p:Principal=Depends(require_task_principal)):
    require_dialogue_task(p,cid,tid);session=session_for(session_id)
    with session_lock(session_id):
        record=task_allowed(p,tid)
        if record.revision!=body.revision:raise TaskError('stale_revision')
        if session.lifecycle_id!=tid:raise TaskError('stale_preview')
        current=task_response(record,session)
        if not current['preview'] or current['preview']['preview_id']!=body.preview_id:raise TaskError('stale_preview')
        return perform(session_id,tid,TaskConfirmRequest(revision=body.revision,text=body.text), 'confirm',p)


@router.post('/dialogues/{cid}/tasks/{tid}/action')
def task_action(session_id:str,cid:str,tid:str,body:Action,p:Principal=Depends(require_task_principal)):
    require_dialogue_task(p,cid,tid);task_allowed(p,tid)
    return perform(session_id,tid,RevisionRequest(revision=body.revision),body.action,p)


def require_dialogue_task(p,cid,tid):
    DialogueStore().load(p,cid)
    if not any(r['kind']=='task' and r['reference']==tid for r in DialogueStore().links(p,cid)):
        raise TaskError('task_not_found',status=404)
