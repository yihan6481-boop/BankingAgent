"""The single security execution entry point.

Every write path — the Agent's single Tool, the Workflow engine, the V2 API,
the CLI and the service-level tests — goes through
:class:`SecurityExecutionGateway`.  There is exactly one implementation of
"preview -> confirm -> strong auth -> revalidate -> execute -> verify" so the
paths cannot drift apart and no reachable "execute without authorization" copy
can exist.

Reads also pass through :meth:`SecurityExecutionGateway.authorize_read`, which
applies the same policy engine (the official task exception makes subscription
queries ``yellow`` even though they are read-only).
"""

from __future__ import annotations

from datetime import datetime

from .audit import AuditChain, AuditRecorder, record_write_audit, sanitize_detail
from .config import DEFAULT_SECURITY_CONFIG, SecurityConfig
from .execution import (
    AuthorizationError,
    AuthorizationGrant,
    WriteSecurityContext,
    consume_authorization,
    issue_authorization,
)
from .ownership import OwnershipError
from .policy import PolicyError, RiskEngine, business_date
from .restrictions import SecurityRestrictions
from .schemas import (
    PermissionLevel,
    PolicyDecision,
    Principal,
    compute_action_digest,
)
from .strong_auth import StrongAuthError, StrongAuthService

# 业务拒绝：永远不能靠强验证绕过，也不会被计为攻击或工具故障
BUSINESS_REFUSAL_CODES = {
    "insufficient_balance",
    "card_unavailable",
    "card_not_found",
    "recipient_not_found",
    "recipient_disabled",
    "recipient_untrusted",
    "recipient_ambiguous",
    "recipient_mismatch",
    "below_minimum",
    "risk_mismatch",
    "product_not_found",
    "product_inactive",
    "holding_not_found",
    "holding_redeemed",
    "amount_exceeds_principal",
    "daily_limit_exceeded",
    "user_cancelled",
}

# Missing first-time factor state is recoverable: the preview must be kept so the
# user can bind the authenticator from the operation card and then re-read the
# original task. It is not a business failure and never means the write ran.
FACTOR_PREREQUISITE_CODES = {"factor_not_bound", "factor_not_confirmed"}


class GatewayError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 400, decision=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.decision = decision


class ActionOutcome:
    """Normalized outcome of one gateway call."""

    __slots__ = (
        "action", "stage", "decision", "challenge", "authorization", "data",
        "message", "error_code", "required_authentication", "restrictions",
        "request_id", "verification_status", "result",
    )

    def __init__(
        self,
        action: str,
        stage: str,
        *,
        decision: PolicyDecision | None = None,
        challenge=None,
        authorization=None,
        data=None,
        message: str = "",
        error_code: str | None = None,
        restrictions=None,
        request_id: str | None = None,
        verification_status: str = "not_run",
        result=None,
    ):
        self.action = action
        self.stage = stage
        self.decision = decision
        self.challenge = challenge
        self.authorization = authorization
        self.data = data
        self.message = message
        self.error_code = error_code
        self.restrictions = restrictions
        self.request_id = request_id
        self.verification_status = verification_status
        self.result = result

    @property
    def success(self) -> bool:
        return self.stage in {"completed", "read_ok"}

    def as_payload(self) -> dict:
        payload = {
            "stage": self.stage,
            "action": self.action,
            "message": self.message,
        }
        if self.decision is not None:
            payload["security"] = self.decision.safe_for_llm
        if self.challenge is not None:
            payload["verification"] = self.challenge.model_dump(mode="json")
        if self.error_code is not None:
            payload["error_code"] = self.error_code
        return payload


class SecurityExecutionGateway:
    """Unified authorize/execute entry point for reads and writes."""

    def __init__(
        self,
        config: SecurityConfig | None = None,
        *,
        policy: RiskEngine | None = None,
        strong_auth: StrongAuthService | None = None,
        restrictions: SecurityRestrictions | None = None,
        now_provider=None,
    ):
        from .identity import _utcnow

        self.config = config or DEFAULT_SECURITY_CONFIG
        self.now_provider = now_provider or _utcnow
        self.policy = policy or RiskEngine(self.config, now_provider=now_provider)
        self.strong_auth = strong_auth or StrongAuthService(
            self.config, now_provider=now_provider
        )
        self.restrictions = restrictions or SecurityRestrictions(
            self.config, now_provider=now_provider
        )
        self.audit = AuditRecorder(now_provider=now_provider)

    # ------------------------------------------------------------------ time
    def _now(self) -> datetime:
        value = self.now_provider()
        if value.tzinfo is None or value.utcoffset() is None:
            raise GatewayError("invalid_time", "安全时钟必须包含时区", status=500)
        return value.replace(microsecond=0)

    def new_chain(self, request_id: str | None = None) -> AuditChain:
        return AuditChain(request_id)

    # ---------------------------------------------------------------- reads
    def authorize_read(
        self,
        action: str,
        principal: Principal,
        arguments: dict | None = None,
        *,
        chain: AuditChain | None = None,
        session_id: str | None = None,
    ) -> ActionOutcome:
        arguments = dict(arguments or {})
        chain = chain or self.new_chain()
        try:
            decision = self.policy.evaluate(action, principal, arguments)
        except PolicyError as error:
            self._audit(chain, "permission_decision", "denied", principal,
                        session_id, action=action, error_code=error.code)
            return ActionOutcome(
                action, "denied", message=error.message, error_code=error.code,
                request_id=chain.request_id,
            )
        if not decision.allowed:
            return ActionOutcome(
                action, "denied", decision=decision,
                message="权限策略拒绝执行", error_code="permission_denied",
                request_id=chain.request_id,
            )
        self._audit(
            chain, "permission_decision", "ok", principal, session_id,
            action=action, decision=decision,
        )
        if decision.requires_confirmation:
            return ActionOutcome(
                action, "need_confirmation", decision=decision,
                message="该查询需要明确授权后才能返回业务数据",
                error_code="query_authorization_required",
                request_id=chain.request_id,
            )
        return ActionOutcome(
            action, "read_ok", decision=decision,
            message="只读查询无需额外授权", request_id=chain.request_id,
        )

    # --------------------------------------------------------------- writes
    def authorize_write(
        self,
        action: str,
        principal: Principal,
        session_id: str,
        arguments: dict | None = None,
        *,
        chain: AuditChain | None = None,
        task_id: str | None = None,
        step_id: str | None = None,
        idempotency_key: str | None = None,
        decision: PolicyDecision | None = None,
    ) -> ActionOutcome:
        """Policy decision + confirmation/strong-auth requirement for a write."""
        arguments = dict(arguments or {})
        chain = chain or self.new_chain()
        if principal is None:
            return ActionOutcome(
                action, "denied", message="缺少服务器端身份",
                error_code="principal_required", request_id=chain.request_id,
            )
        try:
            decision = decision or self.policy.evaluate(action, principal, arguments)
        except PolicyError as error:
            self._audit(chain, "permission_decision", "denied", principal,
                        session_id, action=action, error_code=error.code)
            return ActionOutcome(
                action, "denied", message=error.message, error_code=error.code,
                request_id=chain.request_id,
            )

        self._audit(chain, 'permission_decision', 'ok', principal, session_id,
                    action=action, decision=decision, task_id=task_id, step_id=step_id)
        suspicious = self.restrictions.suspicious_restriction(
            principal, session_id=session_id
        )
        if suspicious is not None and suspicious.active:
            self._audit(
                chain, "restriction", "denied", principal, session_id,
                action=action, decision=decision,
                error_code="suspicious_restricted",
            )
            return ActionOutcome(
                action, "restricted", decision=decision,
                message="检测到可疑请求，敏感执行已被暂时限制",
                error_code="suspicious_restricted", restrictions=suspicious,
                request_id=chain.request_id,
            )

        if decision.requires_strong_auth:
            lock = self.restrictions.strong_auth_lock(principal, session_id=session_id)
            if lock is not None and lock.active:
                self._audit(
                    chain, "restriction", "denied", principal, session_id,
                    action=action, decision=decision,
                    error_code="strong_auth_locked",
                )
                return ActionOutcome(
                    action, "locked", decision=decision,
                    message="强验证已锁定，请稍后重试",
                    error_code="strong_auth_locked", restrictions=lock,
                    request_id=chain.request_id,
                )
            try:
                from .payment_auth import enabled as payment_enabled
                mobile_kwargs={'mobile_arguments':arguments} if payment_enabled() else {}
                view, _code = self.strong_auth.create_challenge(
                    principal, session_id, action, decision.action_digest,
                    decision.decision_id, decision.policy_version, decision.permission_level,**mobile_kwargs)
            except StrongAuthError as error:
                if error.code in FACTOR_PREREQUISITE_CODES:
                    # Keep the trusted preview: first-time enrollment happens from
                    # the operation card, then the user re-reads the original task.
                    self._audit(
                        chain, "strong_auth", "blocked", principal, session_id,
                        action=action, decision=decision, error_code=error.code,
                        detail={"reason": "factor_enrollment_required",
                                "preview_kept": True},
                    )
                    return ActionOutcome(
                        action, "need_strong_auth", decision=decision,
                        message=(
                            error.message if payment_enabled() else
                            "该操作需要独立TOTP验证，但当前账号尚未完成验证器"
                            "绑定/确认。请先在安全面板完成绑定，然后返回原任务重新确认；"
                            "本次未执行任何银行操作。"
                        ),
                        error_code=error.code, request_id=chain.request_id,
                    )
                return ActionOutcome(action, 'denied', decision=decision,
                    message=error.message, error_code=error.code, request_id=chain.request_id)
            self._audit(
                chain, "strong_auth", "pending", principal, session_id,
                action=action, decision=decision, challenge_id=view.challenge_id,
                task_id=task_id, step_id=step_id,
                detail={"required_authentication": view.method},
            )
            return ActionOutcome(
                action, "need_strong_auth", decision=decision, challenge=view,
                message="该操作需要模拟强验证，请通过独立模拟通道获取验证码",
                request_id=chain.request_id,
            )

        if decision.requires_confirmation:
            authorization = issue_authorization(
                principal,
                session_id,
                action,
                decision.action_digest,
                decision.permission_level,
                decision.policy_version,
                decision.decision_id,
                now=self._now(),
                idempotency_key=idempotency_key,
            )
            self._audit(
                chain, "confirmation", "pending", principal, session_id,
                action=action, decision=decision,
                authorization_id=authorization.authorization_id,
                task_id=task_id, step_id=step_id,
                detail={"required_authentication": "confirmation"},
            )
            return ActionOutcome(
                action, "need_confirmation", decision=decision,
                authorization=authorization,
                message="该操作需要明确确认", request_id=chain.request_id,
            )

        authorization = issue_authorization(
            principal,
            session_id,
            action,
            decision.action_digest,
            decision.permission_level,
            decision.policy_version,
            decision.decision_id,
            now=self._now(),
            idempotency_key=idempotency_key,
        )
        return ActionOutcome(
            action, "authorized", decision=decision, authorization=authorization,
            message="权限策略允许自动执行", request_id=chain.request_id,
        )

    # -------------------------------------------------- authorization grant
    def grant_for_verified_challenge(
        self,
        principal: Principal,
        session_id: str,
        challenge_id: str,
        action: str,
        arguments: dict,
        *,
        chain: AuditChain | None = None,
        task_id: str | None = None,
        step_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> ActionOutcome:
        """Re-check the policy after strong auth, then issue the authorization.

        Strong authentication is never a blanket pass: the business facts and
        the permission policy are recomputed, and an escalation during a pending
        action stops execution and asks for a fresh strong verification.
        """
        chain = chain or self.new_chain()
        view = self.strong_auth.load_view(challenge_id)
        if view is None:
            return ActionOutcome(
                action, "denied", message="验证挑战不存在",
                error_code="challenge_not_found", request_id=chain.request_id,
            )
        if view.status != "verified":
            return ActionOutcome(
                action, "need_strong_auth", challenge=view,
                message="验证码尚未通过校验",
                error_code="challenge_not_verified", request_id=chain.request_id,
            )
        try:
            decision = self.policy.evaluate(action, principal, arguments)
        except PolicyError as error:
            return ActionOutcome(
                action, "denied", message=error.message, error_code=error.code,
                request_id=chain.request_id,
            )
        if not self._digest_matches_challenge(challenge_id, decision.action_digest):
            self.strong_auth.cancel(principal, session_id, challenge_id)
            return ActionOutcome(
                action, "denied", decision=decision,
                message="操作参数已变化，原验证挑战失效，请重新预览并确认",
                error_code="challenge_parameter_changed",
                request_id=chain.request_id,
            )
        from .payment_auth import enabled as payment_enabled
        if payment_enabled():
            try:self.strong_auth.payment_auth.assert_requirements(challenge_id,decision,arguments)
            except StrongAuthError as error:
                return ActionOutcome(action,'denied',message=error.message,error_code=error.code,request_id=chain.request_id)
        authorization = issue_authorization(
            principal,
            session_id,
            action,
            decision.action_digest,
            decision.permission_level,
            decision.policy_version,
            decision.decision_id,
            now=self._now(),
            challenge_id=challenge_id,
            strong_auth_method=view.method,
            idempotency_key=idempotency_key,
        )
        self._audit(
            chain, "strong_auth", "ok", principal, session_id, action=action,
            decision=decision, challenge_id=challenge_id,
            authorization_id=authorization.authorization_id,
            authentication_method=view.method, task_id=task_id, step_id=step_id,
        )
        return ActionOutcome(
            action, "authorized", decision=decision, challenge=view,
            authorization=authorization,
            message="强验证通过，授权已发放并绑定本次操作",
            request_id=chain.request_id,
        )

    def _digest_matches_challenge(self, challenge_id: str, digest: str) -> bool:
        from .db import read_connection

        with read_connection() as connection:
            row = connection.execute(
                "SELECT action_digest FROM security_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
        return row is not None and row["action_digest"] == digest

    # ------------------------------------------------------------- execute
    def execute_authorized(
        self,
        action: str,
        principal: Principal,
        session_id: str,
        authorization: AuthorizationGrant,
        arguments: dict,
        *,
        runner,
        chain: AuditChain | None = None,
        task_id: str | None = None,
        step_id: str | None = None,
        expected_digest: str | None = None,
        action_snapshot: dict | None = None,
    ) -> ActionOutcome:
        """Run one authorized write through the single security entry point.

        ``runner`` is called with ``(connection, context)`` and must perform the
        business write **inside the caller's transaction**, then return the
        service result dict.  The gateway has already verified that the
        authorization digest matches the arguments it was issued for; any
        mismatch is a parameter change and is refused before the runner runs.
        """
        chain = chain or self.new_chain()
        digest = compute_action_digest(action, arguments)
        if expected_digest is not None and digest != expected_digest:
            return ActionOutcome(
                action, "denied",
                message="授权绑定的操作参数已变化，请重新预览并确认",
                error_code="authorization_parameter_changed",
                request_id=chain.request_id,
            )
        if authorization.action_digest != digest:
            return ActionOutcome(
                action, "denied",
                message="授权绑定的操作参数已变化，请重新预览并确认",
                error_code="authorization_parameter_changed",
                request_id=chain.request_id,
            )

        context = WriteSecurityContext(
            principal=principal,
            session_id=session_id,
            grant=authorization,
            request_id=chain.request_id,
            action=action,
            policy_version=authorization.policy_version,
            permission_level=authorization.permission_level,
            decision_id=authorization.decision_id,
            challenge_id=authorization.challenge_id,
            strong_auth_method=authorization.strong_auth_method,
            task_id=task_id,
            step_id=step_id,
            audit_chain=chain,
            audit_recorder=self.audit,
            policy_engine=self.policy,
        )
        try:
            from lifecycle.service import prepare_write
            from lifecycle.store import TaskError
            prepare_write(context, arguments)
            result = runner(context)
        except TaskError:
            # Lifecycle conflicts are safe HTTP conflicts, never masked as a write failure.
            raise
        except AuthorizationError as error:
            self._audit(
                chain, "revalidation", "denied", principal, session_id,
                action=action, authorization_id=authorization.authorization_id,
                error_code=error.code, detail={"revalidation": error.code},
            )
            return ActionOutcome(
                action, "denied", message=error.message, error_code=error.code,
                request_id=chain.request_id,
            )
        except OwnershipError as error:
            self._audit(
                chain, "revalidation", "denied", principal, session_id,
                action=action, authorization_id=authorization.authorization_id,
                error_code=error.code,
            )
            return ActionOutcome(
                action, "denied", message=error.message, error_code=error.code,
                request_id=chain.request_id,
            )
        except Exception as error:  # pragma: no cover - defensive
            self._audit(
                chain, "execution", "failed", principal, session_id,
                action=action, authorization_id=authorization.authorization_id,
                error_code="execution_exception",
                detail={"reason": type(error).__name__},
            )
            return ActionOutcome(
                action, "failed", message="写操作执行异常，业务数据未被视为成功修改",
                error_code="execution_exception", request_id=chain.request_id,
            )

        from lifecycle.service import after_business
        after_business()
        if not isinstance(result, dict) or result.get("success") is not True:
            code = (result or {}).get("error_code") or "operation_failed"
            message = (result or {}).get("message") or "写操作未成功"
            outcome = ActionOutcome(
                action, "failed", message=message, error_code=code,
                data=(result or {}).get("data"), result=result,
                request_id=chain.request_id,
            )
            return outcome

        verified = self.verify_write_result(action, result)
        outcome = ActionOutcome(
            action, "completed", data=result.get("data"),
            message=result.get("message") or "操作成功",
            request_id=chain.request_id,
            verification_status="passed" if verified else "failed",
            result=result,
        )
        if not verified:
            outcome.stage = "submitted_unverified"
            outcome.message = (
                "操作可能已经提交，需要人工核对；系统不会自动重试，也不会新扣一次。"
            )
            outcome.error_code = "verification_failed"
        self._audit(
            chain, "execution", "ok", principal, session_id, action=action,
            authorization_id=authorization.authorization_id,
        )
        self._audit(
            chain, "verification", "ok" if verified else "failed", principal,
            session_id, action=action,
            authorization_id=authorization.authorization_id,
            error_code=None if verified else "verification_failed",
            detail={"write_verification": "passed" if verified else "failed"},
        )
        self._audit(
            chain, "final_outcome", "ok" if verified else "failed", principal,
            session_id, action=action,
            authorization_id=authorization.authorization_id,
            error_code=None if verified else "verification_failed",
        )
        return outcome

    def verify_write_result(self, action: str, result: dict) -> bool:
        """Post-write verification: the service already re-read the rows.

        A service that reports ``success`` must also report that it re-read the
        persisted state (``write_verification``), otherwise the gateway refuses
        to call the operation verified.
        """
        if not isinstance(result, dict):
            return False
        data = result.get("data")
        if not isinstance(data, dict):
            return False
        return data.get("write_verification") == "passed"

    # ------------------------------------------------------------- helpers
    def _audit(
        self,
        chain: AuditChain | None,
        stage,
        outcome,
        principal: Principal | None,
        session_id: str | None,
        *,
        action=None,
        decision=None,
        challenge_id=None,
        authorization_id=None,
        authentication_method=None,
        error_code=None,
        task_id=None,
        step_id=None,
        detail=None,
    ):
        if chain is None:
            return None
        try:
            return self.audit.record(
                chain,
                stage,
                outcome,
                principal=principal,
                session_id=session_id,
                task_id=task_id,
                step_id=step_id,
                action=action,
                policy_version=(
                    decision.policy_version if decision is not None else None
                ),
                permission_level=(
                    decision.permission_level if decision is not None else None
                ),
                decision_id=(
                    decision.decision_id if decision is not None else None
                ),
                challenge_id=challenge_id,
                authorization_id=authorization_id,
                authentication_method=authentication_method,
                reason_codes=(
                    list(decision.reason_codes) if decision is not None else []
                ),
                error_code=error_code,
                detail=detail,
            )
        except Exception:
            # 关键审计事实已经在业务事务内写入；这里失败不能伪装成成功，
            # 但也不能让响应路径崩溃。调用方通过 audit 表核对。
            return None


__all__ = [
    "ActionOutcome",
    "BUSINESS_REFUSAL_CODES",
    "GatewayError",
    "SecurityExecutionGateway",
]
