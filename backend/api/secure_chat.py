"""NL 1.0 adapter; execution and independent authentication use the existing kernel."""
from typing import Literal
from uuid import uuid4
import sqlite3
import re

from security.redaction import redact

from fastapi import Depends
from pydantic import Field

from agent.schemas import StrictModel, ToolResult
from agent.llm_client import LLMConfigurationError
from lifecycle.routes import require_task_principal, session_for
from lifecycle.store import TaskStore
from security.schemas import Principal
from security.service import get_security_services
from .runtime import runtime
from .secure import router, SecureTaskResponse, task_response


class SecureChatRequest(StrictModel):
    message: str = Field(min_length=1, max_length=2000)
    intent_mode: Literal['analysis', 'action'] = 'analysis'
    client_message_id: str | None = Field(default=None,pattern=r'^[A-Za-z0-9_-]{8,100}$')


class SecureChatResponse(StrictModel):
    contract_version: Literal['nl-1.0'] = 'nl-1.0'
    action: str
    message: str
    tool_name: str | None = None
    result: ToolResult | None = None
    error_code: str | None = None
    task: SecureTaskResponse | None = None
    turn_id: str | None = None
    displays: list[dict] = Field(default_factory=list)
    context_source: Literal['uploaded','bank'] | None = None
    planning_draft: dict | None = None


@router.post('/chat', response_model=SecureChatResponse)
def chat(session_id: str, body: SecureChatRequest,
         principal: Principal = Depends(require_task_principal)):
    from dialogue.quota import legacy_call
    # Older clients without an ID are new messages each time. Modern clients
    # supply the same ID for retries; uncertain work is never replayed.
    return legacy_call(body.client_message_id,'secure\n'+body.message+'\n'+body.intent_mode,
        lambda:_accepted_chat(session_id,body,principal))


def _accepted_chat(session_id: str, body: SecureChatRequest,principal: Principal):
    """Persist only bounded planning context, outside the executable task store."""
    from planning_drafts import DraftStore, ACTIVE, binding, sync
    from lifecycle.store import TaskError
    from agent.flow_requirements import extract
    from datetime import date,datetime
    session=session_for(session_id)
    with runtime.session_lock(session_id):
        try:
            session.plan_recovery=None
            store=DraftStore()
            selected=None
            if session.planning_draft_id:
                selected=store.load(principal,session.planning_draft_id)
                if selected['status']=='expired':
                    _detach_draft(session)
                    return SecureChatResponse(action='clarification',error_code='draft_expired',
                        message='规划草稿已超时，旧账户、金额和收款人未恢复。请重新明确目标。')
                if (selected['revision']!=session.planning_draft_revision or selected['status'] not in ACTIVE or
                        selected['payload']['resume_binding']!=binding(session_id)):
                    raise TaskError('stale_draft_revision','草稿已变化、取消或过期，请刷新并明确选择有效草稿。')
            # Security redaction is checked before any goal can become durable.
            safe=redact(body.message)
            if safe!=body.message or re.fullmatch(r'\d{6}',body.message.strip()):
                return _dispatch_chat(session_id,body,principal)
            previous=(session.context.query_requirements or {}).get('flow') if selected else None
            from security.config import BUSINESS_TIMEZONE
            flow=extract(body.message,previous['bound_date'] if previous else (
                runtime._secure_agent.today_provider().isoformat() if runtime._secure_agent else
                get_security_services().now_provider().astimezone(BUSINESS_TIMEZONE).date().isoformat()),previous)
            cancel=body.message.strip() in {'取消','取消草稿','取消规划','取消这个草稿'}
            replacement=body.message.startswith(('新任务','开始新任务','换个话题'))
            if replacement and session.lifecycle_id and not session.pending_action and not session.pending_challenge:
                record=TaskStore().load(session.lifecycle_id,principal)
                if record.status in {'paused','completed','cancelled','failed','expired'}:
                    session.lifecycle_id=None;session.lifecycle_revision=None;session.current_task=None
            from agent.query_requirements import complete_other_goal
            if selected and flow is None and complete_other_goal(body.message) and not _slot_answer(body.message,previous):
                replacement=True
            if selected and (cancel or replacement or (flow is not None and flow!=previous and
                    len(flow.get('goals',[]))>=2 and not _slot_answer(body.message,previous))):
                store.update(principal,selected,status='cancelled' if cancel else 'superseded')
                _detach_draft(session)
                selected=None
                if cancel:return SecureChatResponse(action='response',message='规划草稿已取消，未创建或执行资金任务。')
            if not selected and re.fullmatch(r'继续|继续规划|恢复草稿',body.message.strip()):
                drafts=store.list(principal)
                if drafts:return SecureChatResponse(action='clarification',message='请在规划草稿列表明确选择要恢复的草稿；选择只恢复需求，不恢复授权。')
            if selected:
                # CAS claim before any model call. Resume in another session
                # invalidates this revision; consume checks again before start.
                selected=store.update(principal,selected,status='resumed')
                session.planning_draft_revision=selected['revision']
                if re.fullmatch(r'继续|继续规划',body.message.strip()):
                    from planning_drafts import public
                    return SecureChatResponse(action='clarification',message='当前草稿仍需补充待填字段；请明确付款卡、金额、收款人或步骤顺序。',planning_draft=public(selected))
                if flow is None or flow.get('error'):
                    from planning_drafts import public
                    return SecureChatResponse(action='clarification',message=(flow.get('error') if flow else
                        '这条补充不能唯一绑定到草稿字段。请明确付款卡、金额、收款人或步骤顺序；另起目标请说“新任务”。'),planning_draft=public(selected))
            if flow and not flow.get('error') and not session.pending_action and not session.pending_challenge:
                from planning_drafts import missing
                from agent.plan_recovery import clarification
                if missing(flow) or flow.get('ordering')=='ambiguous':
                    session.context.query_requirements=dict(flow=flow,goals=[],months=[],conditions=[])
                    session.context.original_request_date=flow['bound_date']
                    fields=missing(flow) or ['depends_on']
                    text=('规划草稿尚未形成正式任务，请补充：'+ '、'.join(fields)+'。原目标、金额和条件会保留；未授权或执行资金操作。' if missing(flow) else
                        '你希望先查询再准备办理，还是将它们作为互不依赖的操作？请回复“按顺序办理”或“互不依赖”。完整目标会保留。')
                    clarification(session,fields,text)
                    draft=sync(session)
                    return SecureChatResponse(action='clarification',message=text,planning_draft=draft)
            response=_dispatch_chat(session_id,body,principal)
            if flow and not flow.get('error') and response.action=='error' and response.error_code in {
                    'query_scope_mismatch','query_scope_unclear','dependency_order_unclear','dependency_mismatch'}:
                from agent.plan_recovery import clarification
                session.context.query_requirements=dict(flow=flow,goals=[],months=[],conditions=[])
                text='候选计划没有保留完整目标、参数、条件或依赖，已安全拒绝。请回复“按原顺序继续”重新规划；原需求仍保留。'
                clarification(session,['complete_plan'],text)
                response=response.model_copy(update={'action':'clarification','message':text})
            draft=sync(session)
            return response.model_copy(update={'planning_draft':draft})
        except TaskError as exc:
            if exc.code not in {'invalid_draft','draft_not_found','stale_draft_revision','draft_limit'}:
                raise  # Preserve the original business HTTP/error contract.
            _detach_draft(session)
            return SecureChatResponse(action='error',error_code=exc.code,message=exc.message)
        except (sqlite3.Error, OSError):
            # No automatic fallback to executable state when persistence fails.
            _detach_draft(session)
            return SecureChatResponse(action='error',error_code='draft_storage_unavailable',
                message='规划草稿存储暂不可用，本轮未继续规划或执行。请稍后明确选择草稿重试。')


def _slot_answer(text,previous):
    from agent.flow_requirements import extract
    value=extract(text,previous['bound_date'],previous)
    return value is not None and extract(text,previous['bound_date']) is None


def _detach_draft(session):
    session.planning_draft_id=None;session.planning_draft_revision=None
    if session.pending_clarification and session.pending_clarification.kind=='workflow_plan':
        session.pending_clarification=None;session.clarification_expires_at=None
        session.context.reset_scope();session.messages=[]


class DraftRevision(StrictModel):
    revision: int = Field(ge=1,strict=True)


@router.get('/planning-drafts')
def list_planning_drafts(session_id: str,principal: Principal=Depends(require_task_principal)):
    from planning_drafts import DraftStore,public
    return {'drafts':[public(row) for row in DraftStore().list(principal)]}


@router.post('/planning-drafts/{draft_id}/resume')
def resume_planning_draft(session_id: str,draft_id: str,body: DraftRevision,
                          principal: Principal=Depends(require_task_principal)):
    from planning_drafts import DraftStore,ACTIVE,binding,attach,public
    from lifecycle.store import TaskError
    session=session_for(session_id)
    with runtime.session_lock(session_id):
        if session.pending_action or session.pending_challenge or (session.current_task and
                session.current_task.status not in {'completed','failed','cancelled','expired'}):
            raise TaskError('task_in_progress','请先完成或取消当前正式任务。')
        store=DraftStore();row=store.load(principal,draft_id)
        if row['revision']!=body.revision:raise TaskError('stale_draft_revision','草稿已变化，请刷新列表后重新选择。')
        if row['status'] not in ACTIVE:raise TaskError('draft_inactive','草稿已取消、过期或被消费，不能恢复。')
        payload=dict(row['payload'],resume_binding=binding(session_id))
        row=store.update(principal,row,status='resumed',payload=payload)
        attach(session,row)
        return {'draft':public(row),'message':'已选择规划草稿，请补充待填字段；此操作只恢复需求，未恢复任何资金授权。'}


@router.post('/planning-drafts/{draft_id}/cancel')
def cancel_planning_draft(session_id: str,draft_id: str,body: DraftRevision,
                          principal: Principal=Depends(require_task_principal)):
    from planning_drafts import DraftStore,ACTIVE,public
    from lifecycle.store import TaskError
    session=session_for(session_id)
    with runtime.session_lock(session_id):
        store=DraftStore();row=store.load(principal,draft_id)
        if row['revision']!=body.revision:raise TaskError('stale_draft_revision')
        if row['status'] not in ACTIVE:raise TaskError('draft_inactive')
        row=store.update(principal,row,status='cancelled')
        if session.planning_draft_id==draft_id:_detach_draft(session)
        return {'draft':public(row)}


def _dispatch_chat(session_id,body,principal):
    from storage.config import pg02_enabled as enabled
    if enabled():
        from storage.config import pg02_unsupported_request
        if pg02_unsupported_request(body.message):
            return SecureChatResponse(action='error',error_code='pg02_unsupported_business',
                                      message='当前PG支持账单分析、理财、卡片及收款人查询与转账；请求包含未支持业务，整条未执行。')
    session = session_for(session_id)
    with runtime.session_lock(session_id):
        # Security controls precede every semantic route, including direct API calls.
        from security.redaction import redact
        session.conversation_turns=redact(getattr(session,'conversation_turns',[]))
        session.messages=redact(session.messages)
        if re.fullmatch(r'确认(?:转账|操作)\s*\d{6}',body.message.strip()):
            # Kernel validates current binding/revision/expiry. Never model or persist it.
            return _chat(session_id,body,principal)
        safe=redact(body.message)
        if safe!=body.message or re.fullmatch(r'\d{6}',body.message.strip()):
            return SecureChatResponse(action='error',error_code='independent_verification_required',
                message='检测到验证或登录信息，请通过独立验证或登录入口提交；这条消息未发送给模型或保存到聊天。')
        from lifecycle.chat import handle as task_chat
        task_reply=task_chat(body.message,session,principal,body.intent_mode)
        if task_reply is not None:
            return task_reply
        from security.service import get_security_services
        if (session.pending_clarification and session.clarification_expires_at and
            get_security_services().now_provider()>=session.clarification_expires_at):
            if session.lifecycle_id:
                from lifecycle.service import transition
                from lifecycle.models import RevisionRequest
                record=TaskStore().load(session.lifecycle_id,principal)
                transition(session,record.task.task_id,RevisionRequest(revision=record.revision),'pause')
            session.pending_clarification=None
            session.clarification_expires_at=None
            session.context.reset_scope()
            session.messages=[]
            return SecureChatResponse(action='clarification',message='补充信息已超时，旧草稿未继续执行。请重新核对目标；已有任务可明确恢复后重新补充。')
        if session.pending_clarification and session.pending_clarification.kind=='workflow_execution':
            from agent.query_requirements import complete_other_goal
            if not complete_other_goal(body.message) and not body.message.startswith(('新任务','开始新任务','换个话题')):
                if body.intent_mode!='action':
                    return SecureChatResponse(action='error',error_code='explicit_action_required',
                        message='补充资金任务信息请先切换到办理模式；仍需重新预览和明确确认。')
                # Candidate selections are kernel input, never a fresh model
                # decision. Keep the existing task, parameters and dependencies.
                return _chat(session_id,body,principal)
        if session.planning_draft_id:
            if body.intent_mode!='action':
                return SecureChatResponse(action='error',error_code='explicit_action_required',
                    message='补充规划草稿请切换到办理模式；仍需预览和明确确认。')
            return _chat(session_id,body,principal)
        # Unified semantic route comes before the legacy bounded upload adapter.
        # It never writes retractable bill content into persistent chat memory.
        from storage.config import pg02_enabled
        if pg02_enabled():
            agent=runtime._secure_agent
            if agent is None:
                try:
                    from agent.agent import BankingAgent
                    from agent.llm_client import OpenAICompatibleLLMClient
                    from agent.tool_registry import build_default_registry
                    from datetime import datetime
                    from security.config import BUSINESS_TIMEZONE
                    agent=BankingAgent(OpenAICompatibleLLMClient(max_output_tokens=2400,compact_tools=True),
                        registry=build_default_registry(include_analysis=True),llm_max_retries=0,
                        today_provider=lambda:datetime.now(BUSINESS_TIMEZONE).date())
                    runtime._secure_agent=agent
                except LLMConfigurationError:
                    pass
            if agent is not None:
                from conversation import handle as converse
                turn_id=uuid4().hex
                session.conversation_turn_id=turn_id
                reply=converse(body.message,session,principal,agent)
                session.conversation_turn_id=turn_id
                if reply is not None:
                    runtime.set_last_reply(session_id,reply)
                    session.reply_lifecycle_id=None
                    session.conversation_current=True
                    return SecureChatResponse(action=reply.action,message=reply.message,tool_name=reply.tool_name,
                        result=reply.tool_result,error_code=reply.plan_error_code,turn_id=turn_id,
                        displays=_query_displays(reply,turn_id),context_source=getattr(session,'conversation_source',None))
                session.conversation_current=False
                # The preselected business decision is consumed by _chat once.
                if getattr(session,'conversation_predecision',None) is not None:
                    from billvision.chat import is_request as upload_request,handle as upload_handle
                    if upload_request(body.message,session):
                        # Legacy protocol adapter only; normal semantic queries have
                        # already returned above. Consume its one decision, no second NLU.
                        reply=upload_handle(body.message,session,principal,agent)
                        runtime.set_last_reply(session_id,reply);session.reply_lifecycle_id=None
                        return SecureChatResponse(action=reply.action,message=reply.message,tool_name=reply.tool_name,
                            result=reply.tool_result,error_code=reply.plan_error_code,turn_id=turn_id)
        from billvision.chat import is_request as uploaded_bill_request, handle as uploaded_bill_chat
        if uploaded_bill_request(body.message,session) and not getattr(session,'conversation_predecision',None):
            # Retractable external content is never copied to persistent chat history.
            agent=runtime._secure_agent
            if agent is None:
                try:
                    from agent.agent import BankingAgent
                    from agent.llm_client import OpenAICompatibleLLMClient
                    from agent.tool_registry import build_default_registry
                    from datetime import datetime
                    from security.config import BUSINESS_TIMEZONE
                    agent=BankingAgent(OpenAICompatibleLLMClient(max_output_tokens=1800,compact_tools=True),registry=build_default_registry(include_analysis=True),llm_max_retries=0,
                        today_provider=lambda:datetime.now(BUSINESS_TIMEZONE).date())
                    runtime._secure_agent=agent
                except LLMConfigurationError:
                    pass
            reply=uploaded_bill_chat(body.message,session,principal,agent)
            runtime.set_last_reply(session_id,reply)
            session.reply_lifecycle_id=None
            return SecureChatResponse(action=reply.action,message=reply.message,tool_name=reply.tool_name,result=reply.tool_result,error_code=reply.plan_error_code or (reply.tool_result.error_code if reply.tool_result else None))
        session.billvision_last_query=None
        if session.pending_challenge and re.fullmatch(r'\d{6}', body.message.strip()):
            return SecureChatResponse(action='error', error_code='independent_verification_required',
                message='验证码请通过独立验证入口提交；聊天不会保存或处理验证码。')
        response = _chat(session_id, body, principal)
        from conversation import record_business_reply
        record_business_reply(session,body.message,response)
        return response.model_copy(update={'turn_id':getattr(session,'conversation_turn_id',None)})


def _query_displays(reply,turn_id):
    if reply.tool_name!='query_data' or not reply.tool_result or not reply.tool_result.success:
        return []
    from query_presentation import projection
    return [dict(step_id=None,title='查询结果',business=None,
        detail=projection(reply.tool_result.data,turn_id))]


def _chat(session_id, body, principal):
    session = session_for(session_id)
    with runtime.session_lock(session_id):
        agent = runtime._secure_agent
        current = agent.router.now_provider() if agent and hasattr(agent.router, 'now_provider') else None
        from storage.config import pg02_enabled as enabled
        # Inference/analysis cannot consume a previously authorized pending write
        # by sending a confirmation through the analysis-only mode.
        predecision=getattr(session,'conversation_predecision',None)
        from agent.tool_registry import ToolRisk
        reading_decision=bool(predecision and (predecision.action=='response' or
            predecision.action=='tool_call' and agent.registry.get(predecision.tool_name) and
            agent.registry.get(predecision.tool_name).risk==ToolRisk.READ_ONLY))
        if session.pending_action is not None and (body.intent_mode == 'analysis' or reading_decision):
            from agent.session import AgentSession
            from agent.agent import _looks_like_confirmation_attempt, CANCEL_WORDS
            if _looks_like_confirmation_attempt(body.message) or body.message.strip().casefold() in CANCEL_WORDS:
                return SecureChatResponse(action='error', error_code='explicit_action_required',
                    message='当前有待确认操作；请明确选择 action 模式或使用独立任务入口。')
            if agent is None:
                return SecureChatResponse(action='error', error_code='service_unavailable', message='查询服务尚未配置。')
            # Independent read state preserves the live preview and requirements.
            reading = AgentSession(principal=principal, principal_id=principal.principal_id,
                user_id=principal.user_id, session_id=session_id, protocol='v2')
            reading.analysis_only = True
            reading.conversation_predecision=predecision
            session.conversation_predecision=None
            reply = agent.handle(body.message, reading)
            runtime.set_last_reply(session_id, reply)
            session.reply_lifecycle_id = None
            return SecureChatResponse(action=reply.action, message=reply.message,
                tool_name=reply.tool_name, result=reply.tool_result,
                error_code=reply.plan_error_code or (reply.tool_result.error_code if reply.tool_result else None))
        session.analysis_only = body.intent_mode == 'analysis'
        try:
            # Explicit new-task boundary may detach only a paused/terminal task;
            # active previews still require cancellation via the existing kernel.
            if body.message.startswith(('新任务', '换个话题', '开始新任务')):
                if session.lifecycle_id:
                    previous = TaskStore().load(session.lifecycle_id, principal)
                    if (previous.task.status=='waiting_clarification' and not session.pending_action and
                        not session.pending_challenge):
                        from lifecycle.service import transition
                        from lifecycle.models import RevisionRequest
                        previous=transition(session,previous.task.task_id,RevisionRequest(revision=previous.revision),'pause')
                    if previous.status in {'paused','completed','cancelled','failed'} and not session.pending_action and not session.pending_challenge:
                        session.lifecycle_id = None
                        session.lifecycle_revision = None
                        session.current_task = None
                        session.pending_clarification=None
                        session.clarification_expires_at=None
                        session.context.reset_scope()
                        session.messages = []
                elif not session.pending_action and not session.current_task:
                    session.pending_clarification = None
                    session.context.reset_scope()
                    session.messages = []
            reply, session = runtime.handle(session_id, body.message, analysis=True, secure=True)
        except LLMConfigurationError:
            return SecureChatResponse(action='error', error_code='service_unavailable', message='LLM未配置或访问已关闭。')
        finally:
            session.analysis_only = False
        task = None
        response_action=reply.action
        if session.lifecycle_id and session.reply_lifecycle_id == session.lifecycle_id:
            record=TaskStore().load(session.lifecycle_id, principal)
            task = task_response(record, session)
            if record.mode=='single' and record.status=='completed' and reply.action=='task_result':
                response_action='tool_result'  # Preserve the existing single-operation chat contract.
            if reply.requires_confirmation or reply.tool_result and isinstance(reply.tool_result.data,dict) and reply.tool_result.data.get('write_verification')=='passed':
                from conversation import task_event
                task_event(session,TaskStore().load(session.lifecycle_id,principal),'chat_action')
        message = reply.message
        if get_security_services().config.authentication_mode == 'secure':
            message = message.replace('模拟强验证', '独立TOTP验证')
        return SecureChatResponse(action=response_action, message=message,
            tool_name=reply.tool_name, result=reply.tool_result,
            error_code=reply.plan_error_code or (reply.tool_result.error_code if reply.tool_result else None),
            task=task)


