from __future__ import annotations
from task_presentation import step_title

from pydantic import ValidationError

from agent.schemas import AgentReply, ToolResult
from agent.session import AgentSession

from .schemas import (
    CancelSubscriptionCompletedResult,
    CancelSubscriptionPreviewResult,
    CardListResult,
    CardLostCompletedResult,
    CardLostPreviewResult,
    CategorySpendingResult,
    DailyLimitCompletedResult,
    DailyLimitPreviewResult,
    MonthlySpendingResult,
    RecipientListResult,
    SubscriptionCostResult,
    SubscriptionListResult,
    TransactionListResult,
    TransferCompletedResult,
    TransferPreviewResult,
    UiEnvelope,
    UiError,
    UiInteraction,
    UiTask,
    UiTaskStep,
    WealthHoldingListResult,
    WealthProductListResult,
    WealthPurchaseCompletedResult,
    WealthPurchasePreviewResult,
    WealthRedemptionCompletedResult,
    WealthRedemptionPreviewResult,
    WealthRiskProfileResult,
)


_CARD_STATUS = {"正常": "normal", "已挂失": "lost", "已冻结": "frozen", "normal": "normal", "lost": "lost", "frozen": "frozen"}
_SUB_STATUS = {"有效": "active", "已停止": "cancelled", "active": "active", "cancelled": "cancelled"}
_SWITCH = {"已开启": True, "已关闭": False, True: True, False: False, 1: True, 0: False}


_UI_TASK_STATUS = {
    "planned": "planned",
    "running": "running",
    "waiting_clarification": "waiting_clarification",
    "waiting_confirmation": "waiting_confirmation",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "expired": "expired",
}
_UI_STEP_STATUS = {
    "pending": "pending",
    "running": "running",
    "waiting_clarification": "waiting_clarification",
    "waiting_confirmation": "waiting_confirmation",
    "completed": "completed",
    "failed": "failed",
    "skipped": "skipped",
    "cancelled": "cancelled",
}

def _ui_task_status(value):
    # UI contract is intentionally independent from internal Agent enums.
    # Unknown future transient Agent states degrade to `running` instead of
    # silently expanding the public contract.
    return _UI_TASK_STATUS.get(str(value), "running")

def _ui_step_status(value):
    return _UI_STEP_STATUS.get(str(value), "running")


def _task_payload(session: AgentSession):
    task = session.current_task
    if task is None:
        return None
    return UiTask(
        task_id=task.task_id,
        status=_ui_task_status(task.status),
        current_step_id=task.current_step_id,
        steps=[
            UiTaskStep(
                step_id=step.step_id,
                title=step_title(step),
                tool_name=step.tool_name,
                status=_ui_step_status(step.status),
                error_code=step.error_code,
            )
            for step in task.steps
        ],
    )


def _interaction(session: AgentSession):
    if session.pending_action is not None:
        pending = session.pending_action
        return UiInteraction(
            type="confirmation",
            confirmation_mode=("dynamic_phrase" if pending.confirmation_phrase else "explicit"),
            confirmation_phrase=pending.confirmation_phrase,
            expires_at=pending.expires_at.isoformat(),
        )
    if session.pending_clarification is not None:
        return UiInteraction(
            type="clarification",
            missing_fields=list(session.pending_clarification.missing_fields),
            expires_at=(
                session.pending_clarification.expires_at.isoformat()
                if session.pending_clarification.expires_at
                else None
            ),
        )
    return UiInteraction()


def _normalize_card(item):
    item = dict(item)
    item["status"] = _CARD_STATUS.get(item.get("status"), item.get("status"))
    item["overseas_enabled"] = _SWITCH.get(item.get("overseas_enabled"), item.get("overseas_enabled"))
    item["online_payment_enabled"] = _SWITCH.get(item.get("online_payment_enabled"), item.get("online_payment_enabled"))
    return item


def _normalize_subscription(item):
    item = dict(item)
    item["status"] = _SUB_STATUS.get(item.get("status"), item.get("status"))
    return item


_INTERNAL_DATA_KEYS = {
    # V0.7.0 内部字段：只用于安全层/核验，绝不出现在公开协议里
    "_idempotency_key",
    "write_verification",
    "amount_cents",
}


def _strip_internal_fields(value):
    """Remove backend-internal execution fields before contract validation.

    ``amount_cents`` is a public debug field for ``transfer_preview`` only, so it
    is re-added there explicitly by ``TransferPreviewData``; everywhere else
    (wealth previews, all completed payloads) it must not leak into the frozen
    contract.
    """
    if isinstance(value, dict):
        return {
            key: _strip_internal_fields(item)
            for key, item in value.items()
            if key not in _INTERNAL_DATA_KEYS
        }
    if isinstance(value, list):
        return [_strip_internal_fields(item) for item in value]
    return value


def build_ui_result(tool_name: str | None, tool_result: ToolResult | None):
    if tool_result is None or tool_result.status != "success" or tool_name is None:
        return None
    data = _strip_internal_fields(tool_result.data)
    message = tool_result.message
    try:
        if tool_name == "get_monthly_spending":
            return MonthlySpendingResult(message=message, data=data)
        if tool_name == "get_category_spending":
            return CategorySpendingResult(message=message, data=data)
        if tool_name == "get_transactions_by_month":
            return TransactionListResult(message=message, data=data or [])
        if tool_name == "get_cards":
            return CardListResult(message=message, data=[_normalize_card(x) for x in (data or [])])
        if tool_name == "get_active_subscriptions":
            return SubscriptionListResult(message=message, data=[_normalize_subscription(x) for x in (data or [])])
        if tool_name == "get_monthly_subscription_cost":
            normalized = dict(data or {})
            normalized["subscriptions"] = [_normalize_subscription(x) for x in normalized.get("subscriptions", [])]
            return SubscriptionCostResult(message=message, data=normalized)
        if tool_name == "get_transfer_recipients":
            return RecipientListResult(message=message, data=data or [])
        if tool_name == "get_risk_profile":
            return WealthRiskProfileResult(message=message, data=data)
        if tool_name == "get_wealth_products":
            return WealthProductListResult(message=message, data=data or [])
        if tool_name == "get_wealth_holdings":
            return WealthHoldingListResult(message=message, data=data or [])
        if tool_name == "cancel_subscription":
            normalized = _normalize_subscription(data or {})
            if normalized.get("requires_confirmation") is True:
                return CancelSubscriptionPreviewResult(message=message, data=normalized)
            return CancelSubscriptionCompletedResult(message=message, data=normalized)
        if tool_name == "report_card_lost":
            normalized = dict(data or {})
            if normalized.get("requires_confirmation") is True:
                return CardLostPreviewResult(message=message, data=normalized)
            normalized["status"] = _CARD_STATUS.get(normalized.get("status"), normalized.get("status"))
            return CardLostCompletedResult(message=message, data=normalized)
        if tool_name == "set_daily_limit":
            if isinstance(data, dict) and data.get("requires_confirmation") is True:
                return DailyLimitPreviewResult(message=message, data=data)
            return DailyLimitCompletedResult(message=message, data=data)
        if tool_name == "transfer_money":
            if isinstance(data, dict) and data.get("requires_confirmation") is True:
                # Contract 1.1 明确允许 transfer_preview 携带后端确认事实字段
                preview_data = dict(data)
                if tool_result.data and "amount_cents" in tool_result.data:
                    preview_data["amount_cents"] = tool_result.data["amount_cents"]
                return TransferPreviewResult(message=message, data=preview_data)
            return TransferCompletedResult(message=message, data=data)
        if tool_name == "buy_wealth_product":
            if isinstance(data, dict) and data.get("requires_confirmation") is True:
                return WealthPurchasePreviewResult(message=message, data=data)
            return WealthPurchaseCompletedResult(message=message, data=data)
        if tool_name == "redeem_wealth_product":
            if isinstance(data, dict) and data.get("requires_confirmation") is True:
                return WealthRedemptionPreviewResult(message=message, data=data)
            return WealthRedemptionCompletedResult(message=message, data=data)
    except ValidationError as exc:
        raise ValueError(f"UI payload contract mismatch for {tool_name}: {exc}") from exc
    raise ValueError(f"UI payload contract missing for tool: {tool_name}")


def _stable_error_code(detail_code: str | None):
    """把后端内部 detail_code 收敛成稳定 UI 错误码。

    必须有界：返回值只能落在 `UiEnvelopeErrorCode` 内，否则构造 `UiError` 会失败。
    语义上必须区分开的是「写后核验失败」（可能已提交，需人工核对）、
    「重复操作」和「工具执行失败」，它们不能和普通 operation_failed 混在一起。
    """
    code = (detail_code or "").lower()
    if code in {"pending_action_expired", "pending_clarification_expired"} or "expired" in code:
        return "interaction_expired"
    if code == "agent_error":
        return "agent_error"
    if code.endswith("_not_found") or code in {"unknown_tool"}:
        return "not_found"
    if "insufficient" in code:
        return "insufficient_funds"
    if "exceeded" in code and "limit" in code:
        return "limit_exceeded"
    if code in {"high_risk_not_supported", "risk_mismatch", "recipient_untrusted"}:
        return "risk_rejected"
    if "verification" in code:
        # 写后核验失败：操作可能已经提交，需要人工核对，系统不会自动重试。
        return "verification_failed"
    if "idempotency" in code:
        return "duplicate_operation"
    if code in {"tool_execution_error", "tool_failed"} or "tool_execution" in code:
        return "tool_execution_failed"
    if (
        code.startswith("invalid_")
        or "validation" in code
        or "mismatch" in code
        or "condition" in code
        or "argument" in code
        or (code.endswith("_required") and code != "confirmation_required")
        or (("dependency" in code) and code != "dependency_failed")
        or code in {"step_limit", "write_step_limit", "duplicate_step_id", "domain_mismatch"}
    ):
        return "validation_failed"
    if code in {"llm_not_configured", "llm_configuration_error"}:
        return "service_unavailable"
    if code:
        return "operation_failed"
    return "agent_error"


def _status(reply: AgentReply, session: AgentSession):
    detail_code = reply.plan_error_code or (reply.tool_result.error_code if reply.tool_result else None)
    if detail_code == 'analysis_entry_required':
        return 'failed'
    if detail_code in {"pending_action_expired", "pending_clarification_expired"}:
        return "expired"
    if session.pending_action is not None or reply.action == "confirmation_required":
        return "need_confirmation"
    if session.pending_clarification is not None or reply.action == "clarification":
        return "need_info"
    task = session.current_task
    if task is not None:
        if task.status in {"planned", "running"}:
            return "executing"
        if task.status == "waiting_confirmation":
            return "need_confirmation"
        if task.status == "waiting_clarification":
            return "need_info"
        if task.status == "completed":
            return "success"
        if task.status == "cancelled":
            return "cancelled"
        if task.status == "expired":
            return "expired"
        if task.status == "failed":
            return "failed"
    if reply.action in {"tool_result", "task_result"}:
        return "success" if not reply.tool_result or reply.tool_result.status == "success" else "failed"
    if reply.action == "error":
        return "failed"
    return "ready"


def present(reply: AgentReply, session_id: str, session: AgentSession):
    status = _status(reply, session)
    result = None
    error = None
    try:
        result = build_ui_result(reply.tool_name, reply.tool_result)
    except ValueError as exc:
        status = "failed"
        error = UiError(code="contract_payload_mismatch", detail_code="contract_payload_mismatch", message=str(exc))

    if status in {"failed", "expired"} and error is None:
        detail_code = (
            reply.plan_error_code
            or (reply.tool_result.error_code if reply.tool_result else None)
            or ("interaction_expired" if status == "expired" else None)
        )
        error = UiError(
            code=_stable_error_code(detail_code),
            detail_code=detail_code,
            message=reply.message,
        )
    return UiEnvelope(
        session_id=session_id,
        status=status,
        message=reply.message,
        interaction=_interaction(session),
        task=_task_payload(session),
        result=result,
        error=error,
    )


def present_state(session_id: str, session: AgentSession, last_reply: AgentReply | None = None):
    if last_reply is not None:
        envelope = present(last_reply, session_id, session)
        return envelope.model_copy(update={"message": "当前会话状态"})

    task = session.current_task
    if session.pending_action is not None:
        status = "need_confirmation"
    elif session.pending_clarification is not None:
        status = "need_info"
    elif task is None:
        status = "ready"
    elif task.status in {"planned", "running"}:
        status = "executing"
    elif task.status == "waiting_confirmation":
        status = "need_confirmation"
    elif task.status == "waiting_clarification":
        status = "need_info"
    elif task.status == "completed":
        status = "success"
    elif task.status == "cancelled":
        status = "cancelled"
    elif task.status == "expired":
        status = "expired"
    elif task.status == "failed":
        status = "failed"
    else:
        status = "ready"
    return UiEnvelope(
        session_id=session_id,
        status=status,
        message="当前会话状态",
        interaction=_interaction(session),
        task=_task_payload(session),
    )
