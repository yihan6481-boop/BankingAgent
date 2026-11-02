"""Authorization grants, authorization consumption, idempotency and services.

This module is the security layer that has to sit *between* the policy engine
and the banking services.  Two things matter most:

1. **No reachable "execute without authorization" path.**  Service write
   functions accept a :class:`WriteSecurityContext` bound to a durable
   authorization row.  The row is verified *and consumed* inside the same
   SQLite write transaction in which the business rows change, so an
   authorization can never be spent without the business write and the business
   write can never happen twice from one authorization.
2. **Idempotency keys, authorization ids and execution fields are produced by
   the backend.**  They are never accepted from the LLM, the request body or a
   free-form argument.

The ``confirmed=True`` boolean that V0.6.4 used as the write authorization is
gone: it is no longer sufficient evidence and is no longer accepted.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime

from .config import DEFAULT_SECURITY_CONFIG, SecurityConfig
from .audit import record_write_audit
from .db import write_transaction
from .schemas import (
    AuthorizationContext,
    PermissionLevel,
    Principal,
    StrictSecurityModel,
    compute_action_digest,
)
from pydantic import ConfigDict, Field

AUTHORIZATION_REQUIRED_MESSAGE = "写操作缺少可信的后端授权上下文，已拒绝执行。"


class AuthorizationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AuthorizationGrant(StrictSecurityModel):
    """Reference to a durable, single-use authorization row."""

    authorization_id: str
    principal_id: str
    user_id: int
    session_id: str
    action: str
    action_digest: str
    permission_level: PermissionLevel
    policy_version: str
    decision_id: str
    challenge_id: str | None = None
    strong_auth_method: str = "confirmation"
    idempotency_key: str


class WriteSecurityContext(StrictSecurityModel):
    """Trusted context handed to a service write function."""

    principal: Principal
    session_id: str
    grant: AuthorizationGrant
    request_id: str
    action: str
    policy_version: str
    permission_level: PermissionLevel
    decision_id: str
    challenge_id: str | None = None
    strong_auth_method: str = "confirmation"
    reason_codes: list[str] = Field(default_factory=list)
    task_id: str | None = None
    step_id: str | None = None
    lifecycle_binding: object | None = None
    audit_chain: object | None = None
    audit_recorder: object | None = None
    # 事务内重算权限等级（防止 yellow 等待期间升级为 red）所用的策略引擎
    policy_engine: object | None = None
    write_verification_required: bool = True

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    def start_stage(self, connection, chain, recorder, stage, outcome="ok", **detail):
        """Append one audit record inside the service transaction."""
        if connection is None or chain is None:
            return None
        return record_write_audit(
            self, connection, chain, stage, outcome, detail=detail
        )


# --------------------------------------------------------------------- grant
def issue_authorization(
    principal: Principal,
    session_id: str,
    action: str,
    action_digest: str,
    permission_level: str,
    policy_version: str,
    decision_id: str,
    *,
    now: datetime,
    challenge_id: str | None = None,
    strong_auth_method: str = "confirmation",
    idempotency_key: str | None = None,
) -> AuthorizationGrant:
    authorization_id = f"az_{secrets.token_hex(12)}"
    key = idempotency_key or _new_idempotency_key(action)
    with write_transaction() as connection:
        connection.execute(
            """
            INSERT INTO security_authorizations (
                authorization_id, principal_id, user_id, session_id, action,
                action_digest, permission_level, policy_version, decision_id,
                challenge_id, strong_auth_method, issued_at, consumed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                authorization_id,
                principal.principal_id,
                int(principal.user_id),
                session_id,
                action,
                action_digest,
                permission_level,
                policy_version,
                decision_id,
                challenge_id,
                strong_auth_method,
                now.isoformat(),
            ),
        )
    return AuthorizationGrant(
        authorization_id=authorization_id,
        principal_id=principal.principal_id,
        user_id=int(principal.user_id),
        session_id=session_id,
        action=action,
        action_digest=action_digest,
        permission_level=permission_level,
        policy_version=policy_version,
        decision_id=decision_id,
        challenge_id=challenge_id,
        strong_auth_method=strong_auth_method,
        idempotency_key=key,
    )


def _new_idempotency_key(action: str) -> str:
    return f"{action}_{uuid.uuid4().hex}"


def load_authorization(connection, authorization_id: str):
    return connection.execute(
        "SELECT * FROM security_authorizations WHERE authorization_id = ?",
        (authorization_id,),
    ).fetchone()


def consume_authorization(
    connection,
    context: WriteSecurityContext,
    *,
    action: str,
    action_digest: str,
    now: datetime,
):
    """Verify and consume the grant inside the caller's write transaction.

    Raises :class:`AuthorizationError` when the grant is missing, already
    consumed, bound to another principal/session/action, or stale.  The
    ``UPDATE ... WHERE consumed = 0`` guard makes double consumption impossible
    even under concurrent submissions.
    """
    grant = context.grant
    if hasattr(connection, 'assert_identity'):
        connection.assert_identity(context, now)
    # Revocation/expiry must also hold inside the business transaction, even
    # if an HTTP dependency resolved the Principal before logout acquired lock.
    binding = connection.execute('SELECT * FROM auth_sessions WHERE session_id=?',
        (context.session_id,)).fetchone()
    if binding is not None and (binding['revoked'] or
            datetime.fromisoformat(binding['credential_expires_at']) <= now):
        raise AuthorizationError('credential_expired', '访问凭据已失效，请重新登录并重新确认')
    if binding is not None and binding['protocol'] == 'v2':
        active = connection.execute(
            "SELECT 1 FROM credentials c JOIN principals p ON p.principal_id=c.principal_id "
            "WHERE c.credential_id=? AND c.principal_id=? AND c.revoked=0 AND p.status='active'",
            (binding['credential_id'], context.principal.principal_id),
        ).fetchone()
        if active is None:
            raise AuthorizationError('credential_expired', '凭据或身份已撤销，不能消费旧授权')
    from lifecycle.service import assert_transaction
    assert_transaction(connection, context, action=action, action_digest=action_digest)
    row = load_authorization(connection, grant.authorization_id)
    if row is None:
        raise AuthorizationError(
            "authorization_not_found", AUTHORIZATION_REQUIRED_MESSAGE
        )
    if hasattr(connection,'assert_identity'):
        from datetime import timedelta
        from .service import current_security_config
        config=current_security_config()
        issued=datetime.fromisoformat(row['issued_at'])
        if issued > now or now-issued > timedelta(seconds=config.pending_action_ttl_seconds):
            raise AuthorizationError('authorization_expired','授权已过期，请重新预览确认')
    if int(row["consumed"]) == 1:
        raise AuthorizationError(
            "authorization_already_consumed", "该授权已被使用，不能重复执行"
        )
    if row["principal_id"] != context.principal.principal_id:
        raise AuthorizationError(
            "authorization_principal_mismatch", "授权与当前身份不一致"
        )
    if int(row["user_id"]) != int(context.principal.user_id):
        raise AuthorizationError(
            "authorization_principal_mismatch", "授权与当前身份不一致"
        )
    if row["session_id"] != context.session_id:
        raise AuthorizationError(
            "authorization_session_mismatch", "授权不属于当前会话"
        )
    if row["action"] != action:
        raise AuthorizationError(
            "authorization_action_mismatch", "授权绑定的操作不一致"
        )
    if row["action_digest"] != action_digest:
        raise AuthorizationError(
            "authorization_parameter_changed",
            "授权绑定的操作参数已变化，请重新预览并确认",
        )
    # A verified red Challenge is a one-time proof for this exact operation.
    # Consume it in the same transaction as the authorization and business
    # write; otherwise a verified row could remain reusable after success.
    challenge_id = row["challenge_id"]
    from .service import current_security_config
    config = current_security_config()
    from .payment_auth import enabled as payment_enabled
    payment_mode=payment_enabled()
    if payment_mode and action in {'transfer_money','buy_wealth_product','redeem_wealth_product','report_card_lost','set_daily_limit','cancel_subscription'}:
        if not challenge_id or row['strong_auth_method']!='payment':
            raise AuthorizationError('payment_credential_required','当前版本的写操作必须完成绑定本任务的支付验证')
    if config.authentication_mode == 'secure' and row['permission_level'] == 'red':
        if not challenge_id or row['strong_auth_method'] != ('payment' if payment_mode else 'totp'):
            raise AuthorizationError('independent_factor_required', '安全模式red操作必须经过独立TOTP')
    if challenge_id:
        challenge = connection.execute(
            "SELECT * FROM security_challenges WHERE challenge_id = ?",
            (challenge_id,),
        ).fetchone()
        if challenge is None:
            raise AuthorizationError("challenge_not_found", "验证挑战不存在")
        # Old demo service tests use a generic confirmation grant linked to a
        # mock challenge. Preserve that demo behavior; TOTP and secure mode
        # must always agree with the actual durable authentication method.
        if challenge['method'] != row['strong_auth_method'] and (
                config.authentication_mode == 'secure' or 'totp' in {challenge['method'],row['strong_auth_method']}):
            raise AuthorizationError('challenge_method_mismatch', '验证方式与授权不一致')
        if config.authentication_mode == 'secure' and challenge['method'] != ('payment' if payment_mode else 'totp'):
            raise AuthorizationError('independent_factor_required', '安全模式不接受模拟强验证')
        if challenge['method'] in {'totp','payment'}:
            try:
                from .totp import TOTPService
                TOTPService(config,now_provider=lambda:now).assert_preview(connection, challenge_id, context.principal)
            except Exception as exc:
                from .strong_auth import StrongAuthError
                if isinstance(exc, StrongAuthError):
                    raise AuthorizationError(exc.code, exc.message) from exc
                raise
        if challenge["principal_id"] != context.principal.principal_id:
            raise AuthorizationError("challenge_not_found", "验证挑战不存在")
        if challenge["session_id"] != context.session_id:
            raise AuthorizationError(
                "challenge_session_mismatch", "该验证挑战不属于当前会话"
            )
        if challenge["action"] != action:
            raise AuthorizationError(
                "challenge_action_mismatch", "验证挑战绑定的操作不一致"
            )
        if challenge["action_digest"] != action_digest:
            raise AuthorizationError(
                "challenge_parameter_changed", "操作参数已变化，原验证挑战失效"
            )
        if challenge["status"] != "verified":
            code = (
                "challenge_already_used"
                if challenge["status"] == "consumed"
                else "challenge_not_verified"
            )
            raise AuthorizationError(code, "验证挑战不是可消费的已验证状态")
        if datetime.fromisoformat(challenge["expires_at"]) <= now:
            connection.execute(
                "UPDATE security_challenges SET status = 'expired' "
                "WHERE challenge_id = ? AND status = 'verified'",
                (challenge_id,),
            )
            raise AuthorizationError("challenge_expired", "验证挑战已过期")
        consumed_challenge = connection.execute(
            "UPDATE security_challenges SET status = 'consumed', consumed_at = ? "
            "WHERE challenge_id = ? AND status = 'verified'",
            (now.isoformat(), challenge_id),
        )
        if consumed_challenge.rowcount != 1:
            raise AuthorizationError(
                "challenge_already_used", "该验证挑战已被使用"
            )
    update = connection.execute(
        "UPDATE security_authorizations SET consumed = 1, consumed_at = ? "
        "WHERE authorization_id = ? AND consumed = 0",
        (now.isoformat(), grant.authorization_id),
    )
    if update.rowcount != 1:
        raise AuthorizationError(
            "authorization_already_consumed", "该授权已被使用，不能重复执行"
        )
    return row


def attach_authorization_result(connection, authorization_id: str, business_id: str):
    connection.execute(
        "UPDATE security_authorizations SET business_transfer_id = ? "
        "WHERE authorization_id = ?",
        (business_id, authorization_id),
    )


# -------------------------------------------------------------- test helper
def build_legacy_demo_context(
    action: str,
    arguments: dict,
    *,
    session_id: str | None = None,
    owner_user_id: int | None = None,
    permission_level: str = "yellow",
    config: SecurityConfig | None = None,
    now_provider=None,
):
    """Build a granted context for the **legacy V1 single-user demo scope**.

    Service-level tests and the deterministic demo CLI need to exercise a real
    write without a browser login.  This helper creates the same durable
    authorization row that the normal flow creates, so nothing is bypassed: the
    service still verifies ownership, revalidates business state, consumes the
    authorization and verifies the write result inside one transaction.

    It is bound to the legacy demo principal (``owner_id = 0``) or to the
    default demo user when the caller states which owner scope to use, and it is
    intentionally *not* reachable from any HTTP route or LLM tool.
    """
    from .identity import IdentityService, _utcnow
    from .config import DEMO_USER_ID, LEGACY_DEMO_PRINCIPAL_ID

    config = config or DEFAULT_SECURITY_CONFIG
    now = (now_provider or _utcnow)()
    if now.tzinfo is None or now.utcoffset() is None:
        raise AuthorizationError("invalid_time", "测试授权时钟必须包含时区")

    target_owner = DEMO_USER_ID if owner_user_id is None else int(owner_user_id)
    if target_owner == 0:
        principal = IdentityService(config, now_provider=now_provider).legacy_demo_principal()
    else:
        principal = Principal(
            principal_id=f"legacy-owner-{target_owner}",
            user_id=target_owner,
            display_name=f"本机演示用户 {target_owner}",
            kind="legacy_demo" if target_owner == 0 else "system",
            authenticated_at=now,
        )
    sid = session_id or f"s1_legacy_{secrets.token_hex(8)}"
    digest = compute_action_digest(action, arguments)
    decision_id = f"pd_legacy_{secrets.token_hex(6)}"
    grant = issue_authorization(
        principal,
        sid,
        action,
        digest,
        permission_level,
        config.policy_version,
        decision_id=decision_id,
        now=now,
    )
    from .audit import AuditChain, AuditRecorder

    context = WriteSecurityContext(
        principal=principal,
        session_id=sid,
        grant=grant,
        request_id=f"req_legacy_{secrets.token_hex(6)}",
        action=action,
        policy_version=config.policy_version,
        permission_level=permission_level,
        decision_id=decision_id,
        challenge_id=None,
        strong_auth_method="confirmation",
        reason_codes=["legacy_demo_authorization"],
        audit_chain=AuditChain(),
        audit_recorder=AuditRecorder(now_provider=now_provider),
    )
    return context


def require_context(context) -> WriteSecurityContext:
    if context is None:
        raise AuthorizationError("authorization_required", AUTHORIZATION_REQUIRED_MESSAGE)
    if not isinstance(context, WriteSecurityContext):
        raise AuthorizationError(
            "authorization_required", AUTHORIZATION_REQUIRED_MESSAGE
        )
    return context


__all__ = [
    "AUTHORIZATION_REQUIRED_MESSAGE",
    "AuthorizationError",
    "AuthorizationGrant",
    "WriteSecurityContext",
    "attach_authorization_result",
    "build_legacy_demo_context",
    "consume_authorization",
    "issue_authorization",
    "load_authorization",
    "require_context",
]
