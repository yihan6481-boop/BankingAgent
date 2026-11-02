"""Analysis 1.0: strict result kinds alongside unchanged V2 security payloads."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import Field

from analysis_models import (Strict, MonthReportArgs, YearReportArgs, AnomalyArgs,
    CompareArgs, SuitabilityArgs, AnalysisResult, QueryArgs)
from analysis_service import CATALOG, analyze
from analysis_models import ProductComparison
from agent.schemas import AgentReply
from agent.tool_registry import build_default_registry
from agent.tool_router import ToolRouter
from agent.llm_client import LLMConfigurationError
from security.schemas import Principal
from lifecycle.store import TaskStore
from .runtime import runtime
from .v2_routes import require_principal
from .v2_presenter import present_v2
from .v2_schemas import V2Envelope


class MonthlyRequest(Strict):
    tool: Literal['get_monthly_cash_flow_report']
    arguments: MonthReportArgs


class AnnualRequest(Strict):
    tool: Literal['get_annual_cash_flow_report']
    arguments: YearReportArgs


class ClueRequest(Strict):
    tool: Literal['get_expense_clues']
    arguments: AnomalyArgs


class CompareRequest(Strict):
    tool: Literal['compare_wealth_products']
    arguments: CompareArgs


class SuitabilityRequest(Strict):
    tool: Literal['explain_wealth_suitability']
    arguments: SuitabilityArgs


class QueryRequest(Strict):
    tool: Literal['query_financial_records']
    arguments: QueryArgs


ReadRequest = Annotated[MonthlyRequest | AnnualRequest | ClueRequest | CompareRequest | SuitabilityRequest | QueryRequest,
    Field(discriminator='tool')]


class ChatRequest(Strict):
    message: str = Field(min_length=1, max_length=2000)
    intent_mode: Literal['analysis', 'action'] = 'analysis'


class Error(Strict):
    code: Literal['invalid_analysis_arguments', 'unknown_analysis_tool', 'principal_required',
        'card_not_found', 'product_not_found', 'analysis_data_unavailable', 'analysis_snapshot_changed',
        'analysis_verification_failed', 'purchase_fact_mismatch', 'invalid_arguments',
        'tool_execution_error', 'permission_denied', 'query_authorization_required',
        'agent_failed', 'service_unavailable', 'query_scope_unclear']
    message: str


class Problem(Strict):
    code: Literal['invalid_analysis_arguments', 'product_not_found', 'analysis_data_unavailable',
        'session_not_found', 'unauthorized', 'credential_expired', 'session_principal_mismatch']
    message: str


class AnalysisEntry(Strict):
    step_id: str | None = None
    tool: Literal['get_monthly_cash_flow_report', 'get_annual_cash_flow_report',
        'get_expense_clues', 'compare_wealth_products', 'explain_wealth_suitability', 'query_financial_records']
    result: AnalysisResult


class Envelope(Strict):
    contract_version: Literal['analysis-1.0'] = 'analysis-1.0'
    session_id: str
    analyses: list[AnalysisEntry] = Field(default_factory=list, max_length=9)
    business: V2Envelope
    lifecycle_id: str | None = None
    revision: int | None = None
    error: Error | None = None


router = APIRouter(prefix='/api/analysis/sessions/{session_id}', tags=['analysis-1.0'],
    responses={401:{'model':Problem},404:{'model':Problem},422:{'model':Problem}})
public_router = APIRouter(prefix='/api/analysis', tags=['analysis-1.0-public'],
    responses={422:{'model':Problem}})


@public_router.post('/products/compare', response_model=ProductComparison)
def public_comparison(body: CompareArgs):
    from storage.config import pg02_enabled
    if pg02_enabled():
        from storage.application import public_comparison as pg_compare
        return pg_compare(body)
    value = analyze('compare_wealth_products', body)
    if not value['success']:
        raise HTTPException(422, value['error_code'])
    return value['data']


def present_analysis(session_id, session, reply=None, *, direct=None):
    entries = []
    if session.current_task and session.reply_lifecycle_id == session.lifecycle_id:
        for step in session.current_task.steps:
            if step.tool_name in CATALOG and step.status=='completed' and step.result and step.result.success:
                entries.append(AnalysisEntry(step_id=step.step_id, tool=step.tool_name, result=step.result.data))
    target = direct or (reply.tool_result if reply is not None and reply.tool_name in CATALOG else None)
    error = None
    if target is not None:
        if target.success and not any(e.tool==getattr(reply,'tool_name',None) and e.result.model_dump(mode='json')==target.data for e in entries):
            entries.append(AnalysisEntry(tool=reply.tool_name, result=target.data))
        elif not target.success:
            code = target.error_code
            if code not in Error.model_fields['code'].annotation.__args__:
                code = 'agent_failed'
            error = Error(code=code, message=target.message)
    # New facts never enter frozen V1/V2 business payloads.
    legacy_reply = reply
    if reply is not None and reply.tool_name in CATALOG:
        legacy_reply = reply.model_copy(update={'tool_name':None,'tool_result':None})
    business = present_v2(legacy_reply, session_id, session, challenge=session.pending_challenge)
    if error is not None:
        business = present_v2(None, session_id, session, force_status='failed', message=error.message)
    record = TaskStore().load(session.lifecycle_id, session.principal) if getattr(session,'lifecycle_id',None) else None
    return Envelope(session_id=session_id, analyses=entries, business=business,
        lifecycle_id=getattr(session,'lifecycle_id',None), revision=record.revision if record else None, error=error)


def session_for(session_id):
    session=runtime.get_session(session_id)
    if session is None:
        raise HTTPException(404, 'session_not_found')
    return session


@router.post('/read', response_model=Envelope)
def read(session_id: str, body: ReadRequest, principal: Principal=Depends(require_principal)):
    session=session_for(session_id)
    with runtime.session_lock(session_id):
        dispatch=ToolRouter(build_default_registry(include_analysis=True)).dispatch(
            body.tool, body.arguments.model_dump(mode='json'), principal=principal, session=session)
        reply=AgentReply(action='tool_result' if dispatch.result.success else 'error',
            message=dispatch.result.message, tool_name=body.tool, tool_result=dispatch.result)
        runtime.set_last_reply(session_id, reply)
        session.reply_lifecycle_id = None
        return present_analysis(session_id,session,reply)


@router.post('/chat', response_model=Envelope)
def chat(session_id: str, body: ChatRequest, principal: Principal=Depends(require_principal),
         client_message_id: str | None = Header(default=None,alias='X-Client-Message-Id',pattern=r'^[A-Za-z0-9_-]{8,100}$')):
    from dialogue.quota import legacy_call
    return legacy_call(client_message_id,'analysis\n'+body.message+'\n'+body.intent_mode,lambda:_accepted_chat(session_id,body,principal))


def _accepted_chat(session_id,body,principal):
    session=session_for(session_id)
    try:
        with runtime.session_lock(session_id):
            session.analysis_only = body.intent_mode == 'analysis'
            try:
                reply, session=runtime.handle(session_id,body.message,analysis=True)
            finally:
                session.analysis_only = False
    except LLMConfigurationError:
        return Envelope(session_id=session_id,business=present_v2(None,session_id,session,
            force_status='failed',message='LLM未配置'),error=Error(code='service_unavailable',message='LLM未配置'))
    return present_analysis(session_id,session,reply)


@router.get('/state', response_model=Envelope)
def state(session_id: str, principal: Principal=Depends(require_principal)):
    return present_analysis(session_id,session_for(session_id),runtime.get_last_reply(session_id))
