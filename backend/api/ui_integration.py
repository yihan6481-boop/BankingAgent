"""Additive UI adapter. Frozen APIs and the execution kernel stay unchanged.

State reads never resume tasks, issue previews, verify a factor or execute a
tool. Bound confirmation refuses a stale card before entering the existing
secure lifecycle confirmation route.
"""
from typing import Literal

from fastapi import Depends
from pydantic import Field, JsonValue

from agent.schemas import StrictModel
from lifecycle.models import TaskConfirmRequest
from lifecycle.routes import require_task_principal, session_for, perform
from lifecycle.store import TaskStore, TaskError
from lifecycle.service import public
from security.schemas import Principal
from security.service import get_security_services
from security.redaction import redact
from .runtime import runtime
from .secure import router, FactorStatus, SecureTaskResponse, task_response
from .schemas import UiResult
from .presenter import build_ui_result
from .read import READ_SPECS


BUSINESS_TOOLS = set(READ_SPECS) | {
    'cancel_subscription', 'report_card_lost', 'set_daily_limit',
    'transfer_money', 'buy_wealth_product', 'redeem_wealth_product',
}


class DisplayResult(StrictModel):
    step_id: str | None = None
    title: str
    business: UiResult | None = None
    detail: JsonValue | None = None


class UiState(StrictModel):
    contract_version: Literal['ui-integration-1.0'] = 'ui-integration-1.0'
    display_name: str
    status: Literal['ready', 'need_info', 'need_confirmation', 'need_verification',
                    'executing', 'success', 'cancelled', 'expired', 'failed',
                    'paused', 'scheduled', 'manual']
    message: str
    missing_fields: list[str] = Field(default_factory=list)
    task: SecureTaskResponse | None = None
    results: list[DisplayResult] = Field(default_factory=list)
    factor: FactorStatus
    conversation_id: str | None = None
    memory_status: str
    turn_id: str | None = None


class BoundConfirm(TaskConfirmRequest):
    preview_id: str = Field(pattern=r'^[a-f0-9]{64}$')


def display_result(tool, result, title, step_id=None, summary=False):
    if result is None or not result.success:
        return None
    from agent.output_guard import contains_sensitive_result
    from read_models import verify_read
    if not verify_read(tool, result.data) or contains_sensitive_result(result.data, tool_name=tool):
        return None
    if tool=='query_financial_records' and summary:
        # Keep the full verified evidence in the existing tool HTTP result.
        # The summary query's UI projection must not list every transaction.
        from agent.agent import BankingAgent
        return DisplayResult(step_id=step_id,title='账单查询汇总',
            detail={'说明':BankingAgent._deterministic_fallback(tool,result)})
    if tool in BUSINESS_TOOLS:
        business = build_ui_result(tool, result)
        return DisplayResult(step_id=step_id, title=title, business=business)
    # Analysis/history already passed their source contract and output guard.
    # Keep a strict JSON tree rather than importing arbitrary execution fields.
    return DisplayResult(step_id=step_id, title=title, detail=redact(result.data))


@router.get('/ui-state', response_model=UiState)
def state(session_id: str, principal: Principal = Depends(require_task_principal)):
    session = session_for(session_id)
    with runtime.session_lock(session_id):
        reply = runtime.get_last_reply(session_id)
        visible = session.reply_lifecycle_id == session.lifecycle_id or session.pending_action is not None
        record = TaskStore().load(session.lifecycle_id, principal) if session.lifecycle_id and visible else None
        # Do not call task_response for a failed record: that function raises
        # the historical business error. State must still expose the failure.
        task = (SecureTaskResponse(task=public(record))
                if record and record.status == 'failed' else
                SecureTaskResponse.model_validate(task_response(record, session)) if record else None)
        results = []
        summary=bool((session.context.query_requirements or {}).get('financial',{}).get('presentation',{}).get('brief'))
        if record and session.reply_lifecycle_id == record.task.task_id:
            for step in record.task.steps:
                if step.status == 'completed':
                    item = display_result(step.tool_name, step.result, step.purpose, step.step_id,summary=summary)
                    if item:
                        results.append(item)
        if reply and reply.tool_result and reply.tool_result.success:
            # Workflow summaries are not business payloads. Previews and
            # stand-alone reads are rendered through the existing presenter.
            if reply.tool_name in BUSINESS_TOOLS or reply.tool_name in {
                'get_monthly_cash_flow_report', 'get_annual_cash_flow_report',
                'get_expense_clues', 'compare_wealth_products', 'explain_wealth_suitability',
                'query_financial_records',
            }:
                item = display_result(reply.tool_name, reply.tool_result, reply.tool_result.message,summary=summary)
                if item and not any(x.business == item.business and x.detail == item.detail for x in results):
                    results.append(item)
        status = 'ready'
        if record:
            status = {'active': 'executing', 'waiting': 'need_confirmation',
                      'completed': 'success', 'cancelled': 'cancelled',
                      'failed': 'failed', 'paused': 'paused',
                      'scheduled': 'scheduled', 'manual': 'manual'}[record.status]
        preview = task.preview if task else None
        challenge = task.challenge if task else None
        now = get_security_services().strong_auth.now_provider()
        if preview and preview.expires_at <= now:
            status = 'expired'
        elif preview:
            status = 'need_confirmation'
            if session.pending_action and session.pending_action.requires_strong_auth:
                if challenge is None or challenge.status != 'verified':
                    status = 'need_verification'
        missing = []
        if session.pending_clarification and not getattr(session,'conversation_current',False):
            missing = list(session.pending_clarification.missing_fields)
            if not preview:
                status = 'need_info'
        return UiState(display_name=principal.display_name, status=status,
            message=reply.message if reply else '当前会话状态已同步。',
            missing_fields=missing, task=task, results=results,
            factor=FactorStatus.model_validate(get_security_services().totp.status(principal)),
            conversation_id=None,
            turn_id=getattr(session,'conversation_turn_id',None),
            memory_status='retired')


@router.post('/ui/tasks/{task_id}/confirm', response_model=SecureTaskResponse)
def confirm(session_id: str, task_id: str, body: BoundConfirm,
            principal: Principal = Depends(require_task_principal)):
    session = session_for(session_id)
    with runtime.session_lock(session_id):
        record = TaskStore().load(task_id, principal)
        if record.revision != body.revision:
            raise TaskError('stale_revision', '任务已变化，请查看最新预览。')
        if session.lifecycle_id != task_id:
            raise TaskError('stale_preview', '该确认卡不属于当前任务。')
        current = SecureTaskResponse.model_validate(task_response(record, session))
        if current.preview is None or current.preview.preview_id != body.preview_id:
            raise TaskError('stale_preview', '确认卡已失效，请查看最新预览。')
        if current.preview.expires_at <= get_security_services().strong_auth.now_provider():
            raise TaskError('pending_action_expired', '确认预览已过期，请重新预览。')
        # Existing route performs expiry, revision, TOTP, ownership and atomic
        # single-use authorization checks; never execute through the presenter.
        request = TaskConfirmRequest(revision=body.revision, text=body.text, reason=body.reason)
        return perform(session_id, task_id, request, 'confirm', principal)
