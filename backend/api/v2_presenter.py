"""HTTP adapter for the independent V2 security protocol (contract 2.0).

The presenter reuses the frozen V1 business payload builders
(``api.presenter.build_ui_result``) so both protocols render the same
``result.kind`` bodies, while adding the structured ``security`` /
``verification`` / ``restriction`` blocks that V2 clients need.
"""

from __future__ import annotations
from task_presentation import step_title

from agent.output_guard import contains_sensitive_result
from agent.schemas import AgentReply, ToolResult
from agent.session import AgentSession

from .presenter import _stable_error_code, build_ui_result, _task_payload
from .v2_schemas import (
    V2Envelope,
    V2ErrorCode,
    UiInteractionV2,
    UiTaskStepV2,
    UiTaskV2,
    RestrictionView,
    SecurityView,
    VerificationView,
    V2Error,
)


def _capability_status(reply: AgentReply | None, session: AgentSession) -> str:
    """Map internal Agent state to a V2 status, including security states."""
    if reply is not None and reply.plan_error_code == 'analysis_entry_required':
        return 'failed'
    pending_challenge = getattr(session, "pending_challenge", None)
    if pending_challenge is not None:
        if getattr(pending_challenge, "status", "pending") == "pending":
            return "need_verification"
    detail_code = None
    if reply is not None:
        detail_code = reply.plan_error_code or (
            reply.tool_result.error_code if reply.tool_result else None
        )
    if detail_code in {"strong_auth_locked"}:
        return "locked"
    if detail_code in {"suspicious_restricted", "tool_circuit_open"}:
        return "restricted"
    if detail_code == "authorization_parameter_changed":
        return "need_confirmation"
    if detail_code in {
        "permission_escalated_to_strong_auth",
        "strong_auth_required_via_v2",
    }:
        return "need_verification"
    if detail_code in {"pending_action_expired", "pending_clarification_expired"}:
        return "expired"
    if session.pending_action is not None and getattr(
        session.pending_action, "requires_strong_auth", False
    ):
        return "need_verification"
    if reply is not None and reply.action == "confirmation_required":
        return "need_confirmation"
    if session.pending_action is not None:
        return "need_confirmation"
    if session.pending_clarification is not None or (
        reply is not None and reply.action == "clarification"
    ):
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
    if reply is None:
        return "ready"
    if reply.action in {"tool_result", "task_result"}:
        return "success" if not reply.tool_result or reply.tool_result.success else "failed"
    if reply.action == "error":
        return "failed"
    return "ready"


def _interaction_v2(session: AgentSession) -> UiInteractionV2:
    pending_challenge = getattr(session, "pending_challenge", None)
    if pending_challenge is not None and getattr(
        pending_challenge, "status", "pending"
    ) == "pending":
        pending = session.pending_action
        confirmation_phrase = pending.confirmation_phrase if pending else None
        return UiInteractionV2(
            type="verification",
            challenge_id=getattr(pending_challenge, "challenge_id", None),
            confirmation_mode="dynamic_phrase" if confirmation_phrase else "none",
            confirmation_phrase=confirmation_phrase,
            expires_at=pending.expires_at.isoformat() if pending else None,
        )
    if session.pending_action is not None:
        pending = session.pending_action
        return UiInteractionV2(
            type="confirmation",
            confirmation_mode=(
                "dynamic_phrase" if pending.confirmation_phrase else "explicit"
            ),
            confirmation_phrase=pending.confirmation_phrase,
            expires_at=pending.expires_at.isoformat(),
        )
    if session.pending_clarification is not None:
        return UiInteractionV2(
            type="clarification",
            missing_fields=list(session.pending_clarification.missing_fields),
            expires_at=(
                session.pending_clarification.expires_at.isoformat()
                if session.pending_clarification.expires_at
                else None
            ),
        )
    return UiInteractionV2()


def _task_v2(session: AgentSession) -> UiTaskV2 | None:
    task = session.current_task
    if task is None:
        return None
    from .presenter import _ui_step_status, _ui_task_status

    steps = []
    for step in task.steps:
        status = _ui_step_status(step.status)
        if status == "waiting_confirmation" and getattr(
            step, "requires_strong_auth", False
        ):
            status = "waiting_verification"
        steps.append(
            UiTaskStepV2(
                step_id=step.step_id,
                title=step_title(step),
                status=status,
                permission_level=getattr(step, "permission_level", None),
                requires_strong_auth=bool(
                    getattr(step, "requires_strong_auth", False)
                ),
                error_code=step.error_code,
            )
        )
    task_status = _ui_task_status(task.status)
    if task_status == "waiting_confirmation" and any(
        step.status == "waiting_verification" for step in steps
    ):
        task_status = "waiting_verification"
    return UiTaskV2(
        task_id=task.task_id,
        status=task_status,
        current_step_id=task.current_step_id,
        steps=steps,
    )


def security_view(decision) -> SecurityView | None:
    if decision is None:
        return None
    return SecurityView(
        decision_id=decision.decision_id,
        policy_version=decision.policy_version,
        permission_level=decision.permission_level,
        allowed=decision.allowed,
        reason_codes=list(decision.reason_codes),
        requires_confirmation=decision.requires_confirmation,
        required_authentication=decision.required_authentication,
        requires_strong_auth=decision.requires_strong_auth,
        action=decision.action,
        summary=decision.summary,
        threshold_cents=decision.threshold_cents,
        projected_total_cents=decision.projected_total_cents,
    )


def verification_view(challenge) -> VerificationView | None:
    if challenge is None:
        return None
    return VerificationView(
        challenge_id=challenge.challenge_id,
        action=challenge.action,
        method="mock_otp" if challenge.method == "mock_otp" else "confirmation",
        status=challenge.status,
        attempts_remaining=max(int(challenge.attempts_remaining), 0),
        max_attempts=max(int(challenge.max_attempts), 1),
        created_at=challenge.created_at,
        expires_at=challenge.expires_at,
        resend_available_at=challenge.resend_available_at,
        masked_delivery_hint=challenge.masked_delivery_hint,
    )


def restriction_view(restriction) -> RestrictionView | None:
    if restriction is None:
        return None
    return RestrictionView(
        kind=restriction.kind,
        scope=restriction.scope,
        reason_code=restriction.reason_code,
        active=restriction.active,
        locked_at=restriction.locked_at,
        unlock_at=restriction.unlock_at,
        retry_after_seconds=restriction.retry_after_seconds,
        detail=restriction.detail,
    )


_V2_KNOWN_ERROR_CODES = frozenset(V2ErrorCode.__args__)


def normalize_v2_error_code(code: str | None) -> str:
    """Guarantee the returned code is a member of ``V2ErrorCode``.

    Any internal code that is not part of the published contract degrades to a
    stable generic code instead of turning into a 500 from Pydantic validation.
    """
    if code and code in _V2_KNOWN_ERROR_CODES:
        return code
    return "operation_failed"


def _v2_error_code(detail_code: str | None, status: str) -> str:
    if detail_code in {
        "challenge_not_found",
        "challenge_not_verified",
        "challenge_expired",
        "challenge_attempts_exhausted",
        "challenge_already_used",
        "challenge_session_mismatch",
        "challenge_parameter_changed",
        "invalid_code",
        "resend_too_soon",
        "strong_auth_locked",
        "suspicious_restricted",
        "tool_circuit_open",
        "permission_denied",
        "query_authorization_required",
        "strong_auth_required_via_v2",
        "authorization_required",
        "authorization_parameter_changed",
        "authorization_already_consumed",
        "permission_escalated_to_strong_auth",
    }:
        if detail_code == "permission_escalated_to_strong_auth":
            return "strong_auth_required_via_v2"
        return detail_code
    mapped = _stable_error_code(detail_code)
    if status == "locked":
        return "strong_auth_locked"
    if status == "restricted":
        return "suspicious_restricted"
    return mapped


def present_v2(
    reply: AgentReply | None,
    session_id: str,
    session: AgentSession,
    *,
    decision=None,
    challenge=None,
    restriction=None,
    force_status: str | None = None,
    message: str | None = None,
    error_code: str | None = None,
    detail_code: str | None = None,
) -> V2Envelope:
    status = force_status or _capability_status(reply, session)
    text = message if message is not None else (reply.message if reply else "")

    result = None
    if reply is not None:
        try:
            result = build_ui_result(reply.tool_name, reply.tool_result)
        except ValueError as exc:
            status = "failed"
            error_code = error_code or "contract_payload_mismatch"
            detail_code = detail_code or "contract_payload_mismatch"
            text = str(exc)

    error = None
    if status in {"failed", "expired", "locked", "restricted"} or error_code:
        code = normalize_v2_error_code(error_code) if error_code else _v2_error_code(
            detail_code
            or (
                reply.plan_error_code
                if reply is not None
                else None
            )
            or (
                reply.tool_result.error_code
                if reply is not None and reply.tool_result
                else None
            ),
            status,
        )
        error = V2Error(
            code=code,
            message=text,
            detail_code=detail_code
            or (
                reply.plan_error_code
                if reply is not None
                else None
            )
            or (
                reply.tool_result.error_code
                if reply is not None and reply.tool_result
                else None
            ),
            reason_codes=list(decision.reason_codes) if decision is not None else [],
            retry_after_seconds=(
                restriction.retry_after_seconds
                if restriction is not None
                else None
            ),
            unlock_at=restriction.unlock_at if restriction is not None else None,
        )

    return V2Envelope(
        session_id=session_id,
        status=status,
        message=text,
        interaction=_interaction_v2(session),
        security=security_view(decision),
        verification=verification_view(challenge),
        restriction=restriction_view(restriction),
        task=_task_v2(session),
        result=result,
        error=error,
    )


__all__ = [
    "present_v2",
    "restriction_view",
    "security_view",
    "verification_view",
]
