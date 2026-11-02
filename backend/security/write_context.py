"""Shared authorization/audit plumbing for the banking service write paths.

The services are the last line of defence: even a call that somehow reaches a
service function directly must present a durable, single-use authorization
grant bound to the acting principal, the session and the exact action
parameters.  Consumption happens *inside* the service's ``BEGIN IMMEDIATE``
transaction, so:

* an authorization cannot be spent without the business write, and
* the business write cannot happen twice from one authorization.
"""

from __future__ import annotations

import secrets
from datetime import datetime

from .audit import AuditChain, AuditFailure, AuditRecorder, record_write_audit
from .execution import (
    AUTHORIZATION_REQUIRED_MESSAGE,
    AuthorizationError,
    WriteSecurityContext,
    consume_authorization,
)
from .schemas import Principal, compute_action_digest
from utils import make_result


class SecurityAuditContext:
    """Audit fields attached to one confirmed write stage."""

    __slots__ = (
        "action", "policy_version", "permission_level", "decision_id",
        "challenge_id", "strong_auth_method", "reason_codes", "task_id",
        "step_id", "request_id",
    )

    def __init__(
        self,
        action: str,
        *,
        policy_version: str | None = None,
        permission_level: str | None = None,
        decision_id: str | None = None,
        challenge_id: str | None = None,
        strong_auth_method: str | None = None,
        reason_codes: list[str] | None = None,
        task_id: str | None = None,
        step_id: str | None = None,
        request_id: str | None = None,
    ):
        self.action = action
        self.policy_version = policy_version
        self.permission_level = permission_level
        self.decision_id = decision_id
        self.challenge_id = challenge_id
        self.strong_auth_method = strong_auth_method
        self.reason_codes = list(reason_codes or [])
        self.task_id = task_id
        self.step_id = step_id
        self.request_id = request_id or f"req_{secrets.token_hex(10)}"


def authorization_failure(message: str | None = None, code: str | None = None) -> dict:
    result = make_result(
        False, message or AUTHORIZATION_REQUIRED_MESSAGE, None
    )
    result["error_code"] = code or "authorization_required"
    return result


def require_write_context(context) -> WriteSecurityContext:
    if context is None or not isinstance(context, WriteSecurityContext):
        raise AuthorizationError(
            "authorization_required", AUTHORIZATION_REQUIRED_MESSAGE
        )
    return context


def assert_write_authorization(
    connection,
    context,
    action: str,
    arguments: dict,
    *,
    now: datetime,
):
    """Verify + consume the grant in the caller's open transaction."""
    require_write_context(context)
    from .service import current_security_config
    config = current_security_config()
    if config.authentication_mode == 'secure':
        from .policy import RiskEngine
        policy = context.policy_engine or RiskEngine(config)
        current = policy.evaluate(action, context.principal, arguments)
        row = connection.execute('SELECT * FROM security_authorizations WHERE authorization_id=?',
                                 (context.grant.authorization_id,)).fetchone()
        from .payment_auth import enabled as payment_enabled
        method='payment' if payment_enabled() else 'totp'
        if current.requires_strong_auth and (row is None or not row['challenge_id'] or row['strong_auth_method'] != method):
            raise AuthorizationError('independent_factor_required', '安全模式red操作必须经过独立TOTP')
        if payment_enabled() and current.requires_strong_auth:
            from .service import get_security_services
            from .strong_auth import StrongAuthError
            try:get_security_services().payment_auth.assert_requirements(row['challenge_id'],current,arguments,connection)
            except StrongAuthError as error:raise AuthorizationError(error.code,error.message) from None
    digest = compute_action_digest(action, arguments)
    consume_authorization(
        connection, context, action=action, action_digest=digest, now=now
    )
    return context.grant


class PermissionEscalated(Exception):
    """The permission level rose after the user's confirmation/verification."""

    def __init__(self, message: str, decision=None):
        super().__init__(message)
        self.code = "permission_escalated"
        self.message = message
        self.decision = decision


def assert_permission_not_escalated(context, action: str, arguments: dict, policy):
    """Re-check the policy inside the transaction and refuse an escalation.

    Requirement: a yellow action that becomes red while waiting (for example
    because another session pushed the daily cumulative transfer total over the
    threshold) must **stop** and ask for strong verification again.  It must
    never silently execute under the older, weaker authorization.
    """
    from .policy import PolicyError, level_rank

    grant = getattr(context, "grant", None)
    if grant is None or policy is None:
        return
    try:
        current = policy.evaluate(action, context.principal, arguments)
    except PolicyError:
        # 缺少安全事实时 fail closed
        raise PermissionEscalated(
            "无法重新确认权限等级，已停止执行；请重新预览并确认"
        )
    if level_rank(current.permission_level) > level_rank(grant.permission_level):
        raise PermissionEscalated(
            "等待确认期间权限等级已升级（"
            f"{grant.permission_level} -> {current.permission_level}），"
            "已停止执行；请重新预览、确认并完成强验证",
            current,
        )


def write_audit(
    context,
    connection,
    stage: str,
    outcome: str,
    *,
    error_code: str | None = None,
    detail: dict | None = None,
):
    """Append a write-stage audit record inside the business transaction."""
    chain = getattr(context, "audit_chain", None)
    if chain is None or connection is None:
        raise AuditFailure(
            "audit_context_missing",
            "缺少必需安全审计上下文，拒绝敏感执行",
        )
    return record_write_audit(
        context,
        connection,
        chain,
        stage,
        outcome,
        error_code=error_code,
        detail=detail,
    )


def mark_verified(data):
    """Attach the post-write verification marker the gateway requires."""
    if isinstance(data, dict):
        data = dict(data)
        data["write_verification"] = "passed"
    return data


def owner_scope(principal: Principal | None) -> int:
    from .config import DEMO_USER_ID, LEGACY_DEMO_USER_ID

    if principal is None:
        # 无登录的 V1 Demo 走隔离的 legacy demo owner scope（0）。
        return LEGACY_DEMO_USER_ID
    return int(principal.user_id)


__all__ = [
    "SecurityAuditContext",
    "assert_write_authorization",
    "authorization_failure",
    "mark_verified",
    "owner_scope",
    "require_write_context",
    "write_audit",
]
