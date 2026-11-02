"""Confirmed execution extracted unchanged from the V2 HTTP adapter.
All authorization, parameter binding, escalation and atomic verification remain
in the security gateway. This module returns neutral facts, never HTTP types.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
from security.audit import AuditChain
from security.execution import AuthorizationGrant
from security.runners import ActionPlanner
from security.service import get_security_services
from security.strong_auth import StrongAuthError
_CONFIRM_TTL_SECONDS = 120
@dataclass
class ConfirmedResult:
    status: str
    message: str
    decision: object = None
    challenge: object = None
    error_code: str | None = None
    detail_code: str | None = None
    data: object = None

def outcome_feedback(_reply, _sid, _session, *, decision=None, challenge=None,
        force_status='failed', message='', error_code=None, detail_code=None):
    return ConfirmedResult(force_status,message,decision,challenge,error_code,detail_code)

def _outcome_to_response(sid,session,outcome,principal,*,action):
    return ConfirmedResult('success' if outcome.success else 'failed',outcome.message,
        outcome.decision,error_code=outcome.error_code,data=outcome.data)

def _bind_preview(session, action: str, plan) -> None:
    """Store the exact normalized operation shown to this authenticated session."""
    now = get_security_services().now_provider().replace(microsecond=0)
    session.pending_revision += 1
    if session.pending_action is not None:
        session.pending_action.revision = session.pending_revision
    session.request_action = action
    session.request_action_digest = plan.decision.action_digest
    session.request_arguments = dict(plan.arguments)
    session.request_idempotency_key = plan.idempotency_key
    session.request_signature = plan.decision.action_digest
    session.request_expires_at = now + timedelta(seconds=_CONFIRM_TTL_SECONDS)


def _clear_preview_binding(session) -> None:
    session.request_action = None
    session.request_action_digest = None
    session.request_arguments = None
    session.request_idempotency_key = None
    session.request_signature = None
    session.request_expires_at = None


def _amount_constraint_conflict(plan, constraints):
    if not constraints:
        return None
    from decimal import Decimal
    from agent.constraints import check_amount

    public_amount = plan.arguments.get("amount")
    if public_amount is None and isinstance(plan.arguments.get("amount_cents"), int):
        public_amount = Decimal(plan.arguments["amount_cents"]) / Decimal("100")
    return check_amount(
        {} if public_amount is None else {"amount": public_amount}, constraints
    )


def _preserve_constraints_after_mismatch(session, pending) -> None:
    if pending is None or not pending.constraints:
        return
    from agent.schemas import ConstraintDraft

    session.constraint_draft = ConstraintDraft(
        tool_name=pending.tool_name,
        arguments=dict(pending.requested_arguments),
        constraints=list(pending.constraints),
        reason="authorization_parameter_changed",
    )


def _invalidate_preview(session_id: str, session, principal) -> None:
    """Make a mismatched/replaced preview unreachable while retaining constraints."""
    services = get_security_services()
    pending = getattr(session, "pending_action", None)
    _preserve_constraints_after_mismatch(session, pending)
    challenge = getattr(session, "pending_challenge", None)
    if challenge is not None:
        try:
            services.strong_auth.cancel(principal, session_id, challenge.challenge_id)
        except StrongAuthError:
            pass
    session.pending_action = None
    session.pending_challenge = None
    _clear_preview_binding(session)


def _changed_operation_response(session_id, session, decision=None):
    return outcome_feedback(
        None, session_id, session, decision=decision,
        force_status="need_confirmation",
        message="操作参数已变化，旧预览、确认和验证均已失效；请重新预览并确认。",
        error_code="authorization_parameter_changed",
        detail_code="authorization_parameter_changed",
    )


def _execute_confirmed(session_id: str, session, plan, principal, *, action: str,
                       confirm_text: str | None = None):
    from lifecycle.service import validate_preview
    validate_preview(session, session.pending_action)
    services = get_security_services()
    decision = plan.decision

    # The active server-side preview is the authority.  Replanning the current
    # body is only a revalidation step; it can never replace this binding.
    pending = getattr(session, "pending_action", None)
    if pending is not None and pending.tool_name != action:
        _invalidate_preview(session_id, session, principal)
        return _changed_operation_response(session_id, session, decision)
    now = get_security_services().now_provider()
    if pending is not None and now >= pending.expires_at:
        _invalidate_preview(session_id, session, principal)
        return outcome_feedback(
            None, session_id, session, decision=decision,
            force_status="expired",
            message="该操作确认已过期，请重新预览并确认。",
            error_code="authorization_required",
            detail_code="pending_action_expired",
        )

    bound_digest = (
        pending.action_digest
        if pending is not None
        else getattr(session, "request_action_digest", None)
    )
    bound_arguments = (
        dict(pending.execution_arguments)
        if pending is not None
        else dict(getattr(session, "request_arguments", None) or {})
    )
    if (
        not bound_digest
        or getattr(session, "request_action", action) not in {None, action}
        or decision.action_digest != bound_digest
        or dict(plan.arguments) != bound_arguments
    ):
        _invalidate_preview(session_id, session, principal)
        return _changed_operation_response(session_id, session, decision)

    # 动态六位确认短语保留其"确认"作用：它不替代 OTP，但必须被满足。
    if pending is not None and pending.confirmation_phrase:
        normalized = (confirm_text or "").strip(" \t\r\n。．.!！?？,，;；:：")
        if normalized.casefold() != pending.confirmation_phrase.casefold():
            return outcome_feedback(
                None, session_id, session, decision=decision,
                force_status="need_confirmation",
                message=(
                    "确认短语不匹配，操作未执行。"
                    f"请回传：{pending.confirmation_phrase}"
                ),
                error_code="validation_failed",
                detail_code="confirmation_mismatch",
            )

    challenge = getattr(session, "pending_challenge", None)
    if pending is not None and pending.constraints:
        conflict = _amount_constraint_conflict(plan, pending.constraints)
        if conflict is not None:
            # V2 错误码保持有界：沿用既有 limit_exceeded，细节放 detail_code。
            return outcome_feedback(
                None, session_id, session, decision=decision,
                force_status="failed", message=conflict[1],
                error_code="limit_exceeded", detail_code=conflict[0],
            )
    if decision.requires_strong_auth:
        if challenge is None:
            if pending is not None and not pending.requires_strong_auth:
                # Trusted facts changed while a yellow preview waited.  Keep
                # the same operation binding but require a fresh red challenge
                # and a fresh confirmation phrase before any execution.
                escalated = services.gateway.authorize_write(
                    action, principal, session_id, plan.arguments,
                    chain=session.audit_chain or AuditChain(),
                    idempotency_key=plan.idempotency_key,
                    decision=decision,
                )
                if escalated.challenge is not None:
                    pending.permission_level = decision.permission_level
                    pending.decision_id = decision.decision_id
                    pending.policy_version = decision.policy_version
                    pending.action_digest = decision.action_digest
                    pending.requires_strong_auth = True
                    pending.authorization_id = None
                    pending.confirmation_phrase = (
                        f"确认转账 {secrets.randbelow(1_000_000):06d}"
                        if action == "transfer_money"
                        else f"确认操作 {secrets.randbelow(1_000_000):06d}"
                    )
                    pending.confirmation_phrase = session.reserve_confirmation_phrase(
                        pending.confirmation_phrase
                    )
                    pending.created_at = now.replace(microsecond=0)
                    pending.expires_at = pending.created_at + timedelta(
                        seconds=_CONFIRM_TTL_SECONDS
                    )
                    session.pending_challenge = escalated.challenge
                    _bind_preview(session, action, plan)
                    return outcome_feedback(
                        None, session_id, session, decision=decision,
                        challenge=escalated.challenge,
                        force_status="need_verification",
                        message=(
                            "等待期间权限已升级；旧确认未执行。"
                            "请完成新的模拟强验证并使用新确认短语。"
                        ),
                        error_code="strong_auth_required_via_v2",
                        detail_code="permission_escalated_to_strong_auth",
                    )
            return outcome_feedback(
                None, session_id, session, decision=decision,
                force_status="need_verification",
                message="该操作需要模拟强验证，请先创建并完成验证挑战。",
                error_code="challenge_not_verified",
                detail_code="challenge_not_verified",
            )
        view = services.strong_auth.load_view(challenge.challenge_id)
        if view is None or view.status != "verified":
            return outcome_feedback(
                None, session_id, session, decision=decision, challenge=view,
                force_status="need_verification",
                message="验证码尚未通过校验，操作未执行。",
                error_code="challenge_not_verified",
                detail_code="challenge_not_verified",
            )
        outcome = services.gateway.grant_for_verified_challenge(
            principal,
            session_id,
            challenge.challenge_id,
            action,
            ActionPlanner(services.config).final_arguments(action, plan.arguments),
            idempotency_key=plan.idempotency_key,
        )
        if outcome.authorization is None:
            return outcome_feedback(
                None, session_id, session, decision=outcome.decision,
                challenge=outcome.challenge, force_status="failed",
                message=outcome.message,
                error_code=outcome.error_code,
                detail_code=outcome.error_code,
            )
        authorization = outcome.authorization
    else:
        # ``confirmed=true`` is not an authorization factory.  A yellow action
        # must consume the one-time grant created with the matching preview.
        if (
            pending is None
            or pending.authorization_id is None
            or pending.requires_strong_auth
            or pending.permission_level == "red"
        ):
            return outcome_feedback(
                None, session_id, session, decision=decision,
                force_status="need_confirmation",
                message="缺少与当前预览绑定的有效确认授权，操作未执行。",
                error_code="authorization_required",
                detail_code="authorization_required",
            )
        authorization = AuthorizationGrant(
            authorization_id=pending.authorization_id,
            principal_id=principal.principal_id,
            user_id=int(principal.user_id),
            session_id=session_id,
            action=action,
            action_digest=pending.action_digest,
            permission_level=pending.permission_level or decision.permission_level,
            policy_version=pending.policy_version or decision.policy_version,
            decision_id=pending.decision_id or decision.decision_id,
            challenge_id=None,
            strong_auth_method="confirmation",
            idempotency_key=str(
                pending.execution_arguments.get("idempotency_key") or ""
            ),
        )
    if authorization is None:
        return outcome_feedback(
            None, session_id, session, decision=decision, force_status="failed",
            message="无法建立可信授权上下文，操作未执行。",
            error_code="authorization_required",
            detail_code="authorization_required",
        )

    session.pending_challenge = None
    session.pending_action = None
    _clear_preview_binding(session)

    def runner(context):
        from security.runners import get_runner

        runner_impl = get_runner(action)
        if runner_impl is None:
            return {"success": False, "message": "未知写操作", "error_code": "unknown_action"}
        return runner_impl.execute(dict(plan.arguments), context)

    outcome = services.gateway.execute_authorized(
        action,
        principal,
        session_id,
        authorization,
        ActionPlanner(services.config).final_arguments(action, plan.arguments),
        runner=runner,
        chain=session.audit_chain or AuditChain(),
        expected_digest=bound_digest,
    )
    return _outcome_to_response(session_id, session, outcome, principal, action=action)


