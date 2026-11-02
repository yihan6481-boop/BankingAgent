"""V0.7.0 independent security protocol routes: ``/api/v2``.

Design notes
------------
* V2 sessions are created from a verified credential; the identity is stored
  server-side and the request body can never supply ``user_id``.
* Deterministic ``/read`` keeps using the independent read catalog and real
  result verification.  Read-only queries that the policy classifies as
  ``yellow`` (the official subscription-query exception) create a read-only
  authorization and reveal no business data until it is confirmed; after
  confirmation the **same** read catalog runs again.
* Deterministic write endpoints (``/transfer``, ``/cancel-subscription``,
  ``/report-card-lost``, ``/daily-limit``, ``/wealth/purchase``,
  ``/wealth/redemption``) exist so the full
  preview -> confirm -> strong auth -> revalidate -> execute -> verify flow can
  be completed **without an LLM key**.  ``/chat`` keeps the existing
  "unavailable when not configured" semantics.
* Strong verification never goes through the LLM and the code never appears in
  any response.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from agent.llm_client import LLMConfigurationError
from security.audit import AuditChain, AuditRecorder
from security.execution import AuthorizationGrant, WriteSecurityContext
from security.identity import IdentityError
from security.mock_inbox import inbox
from security.policy import ACTION_POLICIES, PolicyError
from security.runners import ActionPlanner
from security.schemas import Principal
from security.service import get_security_services
from security.strong_auth import StrongAuthError

from .read import READ_SPECS, get_read_only_service
from .runtime import runtime
from .schemas import ReadRequest
from .v2_presenter import (
    normalize_v2_error_code,
    present_v2,
    restriction_view,
    security_view,
    verification_view,
)
from .v2_schemas import (
    CONTRACT_VERSION_V2,
    CapabilitiesResponseV2,
    CapabilityV2,
    ChallengeStatusResponse,
    CreateSessionRequest,
    HealthResponseV2,
    LoginRequest,
    LoginResponse,
    PermissionRequirement,
    PrincipalView,
    SecurityStatusResponse,
    SendChallengeRequest,
    V2ChatRequest,
    V2ConfirmRequest,
    V2Envelope,
    V2Error,
    V2ReadRequest,
    VerifyChallengeRequest,
    BreakerView,
    ApiProblemV2,
)

router = APIRouter(prefix="/api/v2", tags=["v2-security"])

_BREAKER_TOOLS = (
    "transfer_money",
    "buy_wealth_product",
    "redeem_wealth_product",
    "report_card_lost",
    "set_daily_limit",
    "cancel_subscription",
)


def problem(status_code: int, code: str, message: str):
    return JSONResponse(
        status_code=status_code,
        content=ApiProblemV2(code=code, message=message).model_dump(mode="json"),
    )


def _v2_error(status: str, code: str, message: str, **extra) -> V2Envelope:
    envelope = V2Envelope(
        session_id=extra.pop("session_id", ""),
        status=status,
        message=message,
        interaction=__import__("api.v2_schemas", fromlist=["UiInteractionV2"]).UiInteractionV2(),
        error=V2Error(code=code, message=message, **extra),
    )
    return envelope


# --------------------------------------------------------------- dependency
def require_principal(
    session_id: str,
    authorization: str | None = Header(default=None),
) -> Principal:
    """Resolve the server-side principal for a V2 session.

    The authenticated source is the ``Authorization: Bearer <token>`` header;
    the token is the opaque, random session credential issued at login and bound
    to the session row.  A ``user_id`` in a body is rejected elsewhere as an
    injected internal field, so identity can never be asserted by the caller.
    """
    services = get_security_services()
    binding = services.identity.get_session(session_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token or not secrets.compare_digest(token, binding.session_id):
        raise HTTPException(status_code=401, detail="unauthorized")
    principal = services.identity.resolve_principal(session_id)
    if principal is None:
        services.identity.revoke_session(session_id)
        runtime.discard_session(session_id)
        raise HTTPException(status_code=401, detail="credential_expired")
    if binding.protocol != 'v2':
        raise HTTPException(status_code=401, detail='unauthorized')
    session = runtime.get_session(session_id)
    if session is not None:
        session.principal = principal
        session.principal_id = principal.principal_id
        session.user_id = principal.user_id
    return principal


# ------------------------------------------------------------------- health
@router.get("/health", response_model=HealthResponseV2)
def health_v2():
    services = get_security_services()
    return HealthResponseV2(
        status="ok",
        backend_version="0.9.1",
        contract_version=CONTRACT_VERSION_V2,
        policy_version=services.config.policy_version,
        llm_configured=runtime.llm_configured,
        demo_login_enabled=services.config.demo_login_enabled,
        mock_otp_inbox_enabled=services.config.mock_otp_inbox_enabled,
    )


@router.get("/capabilities", response_model=CapabilitiesResponseV2)
def capabilities_v2():
    response = CapabilitiesResponseV2(
        capabilities=[
            CapabilityV2(id="auth", name="模拟登录与服务器端身份", enabled=True),
            CapabilityV2(id="session", name="绑定凭据的会话", enabled=True),
            CapabilityV2(id="bill", name="账单分析", enabled=True),
            CapabilityV2(id="card", name="银行卡管理", enabled=True),
            CapabilityV2(id="subscription", name="订阅与代扣", enabled=True),
            CapabilityV2(id="transfer", name="智能转账", enabled=True),
            CapabilityV2(id="wealth", name="模拟理财", enabled=True),
            CapabilityV2(id="workflow", name="跨场景任务", enabled=True),
            CapabilityV2(id="security", name="统一权限与安全状态", enabled=True),
            CapabilityV2(id="strong_auth", name="模拟强验证闭环", enabled=True),
        ]
    )
    from storage.config import pg02_enabled
    if pg02_enabled():
        from storage.pg02 import readiness
        supported={'auth','session','card','transfer','workflow','security','strong_auth'}
        if readiness()['pg03_bill_ready']: supported.add('bill')
        if readiness()['pg04_wealth_ready']: supported.add('wealth')
        if readiness()['application_ready']: supported.add('subscription')
        for capability in response.capabilities:
            capability.enabled=capability.id in supported
    return response


@router.get("/security/permission-requirements", response_model=list[PermissionRequirement])
def permission_requirements():
    return [
        PermissionRequirement(
            action=name,
            permission_level=policy.base_level,
            requires_confirmation=policy.requires_confirmation,
            required_authentication=(
                "mock_otp"
                if policy.requires_strong_auth
                else ("confirmation" if policy.requires_confirmation else "none")
            ),
            requires_strong_auth=policy.requires_strong_auth,
        )
        for name, policy in sorted(ACTION_POLICIES.items())
    ]


# --------------------------------------------------------------------- auth
@router.post("/auth/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request):
    services = get_security_services()
    if not services.config.demo_login_enabled:
        return problem(403, "unauthorized", "模拟登录已关闭")
    from security.login_limit import reserve
    source = request.client.host if request.client else 'unknown'
    wait = reserve(source, body.username)
    if wait:
        response = problem(429, "suspicious_restricted", f"登录尝试过于频繁，请在 {wait} 秒后重试")
        response.headers['Retry-After'] = str(wait)
        return response
    try:
        principal, binding, _token = services.identity.login(
            body.username, body.password
        )
    except IdentityError as error:
        # 认证失败按来源计数，与已认证用户的验证失败分开。
        return problem(401, "unauthorized", error.message)
    reserve(source, body.username, success=True)
    runtime.create_session(
        principal=principal, protocol="v2", session_id=binding.session_id
    )
    return LoginResponse(
        session_id=binding.session_id,
        principal=PrincipalView(
            principal_id=principal.principal_id,
            user_id=principal.user_id,
            display_name=principal.display_name,
            kind=principal.kind,
            session_protocol="v2",
        ),
        credential_expires_at=binding.credential_expires_at,
        credential_hint=binding.session_id[:8],
    )


@router.post("/auth/logout")
def logout(session_id: str, principal: Principal = Depends(require_principal)):
    services = get_security_services()
    with runtime.session_lock(session_id):
        services.identity.revoke_session(session_id)
        runtime.discard_session(session_id)
    return {"contract_version": CONTRACT_VERSION_V2, "revoked": True}


@router.post("/sessions", response_model=LoginResponse)
def create_session(
    body: CreateSessionRequest,
    session_id: str = Header(alias="X-Session-Source", default=""),
    authorization: str | None = Header(default=None),
):
    """V2 不接受凭据换取新会话：必须先用 /auth/login 建立绑定会话。"""
    raise HTTPException(status_code=409, detail="session_principal_mismatch")


# --------------------------------------------------------------------- chat
@router.post("/sessions/{session_id}/chat", response_model=V2Envelope)
def chat(
    session_id: str,
    body: V2ChatRequest,
    principal: Principal = Depends(require_principal),
    client_message_id: str | None = Header(default=None,alias='X-Client-Message-Id',pattern=r'^[A-Za-z0-9_-]{8,100}$'),
):
    from dialogue.quota import legacy_call
    return legacy_call(client_message_id,'v2\n'+body.message,lambda:_accepted_chat(session_id,body,principal))


def _accepted_chat(session_id,body,principal):
    session = runtime.get_session(session_id)
    if session is None:
        return problem(404, "session_not_found", "会话不存在或后端已重启")
    services = get_security_services()
    services.identity.assert_session_owner(session_id, principal)
    chain = AuditChain()
    with runtime.session_lock(session_id):
        try:
            reply, session = runtime.handle(session_id, body.message)
        except KeyError:
            return problem(404, "session_not_found", "会话不存在或后端已重启")
        except LLMConfigurationError:
            return problem(503, "service_unavailable", "LLM 未配置或暂不可用")
        session.audit_chain = chain
    return present_v2(reply, session_id, session)


@router.post("/sessions/{session_id}/confirm", response_model=V2Envelope)
def confirm(
    session_id: str,
    body: V2ConfirmRequest,
    principal: Principal = Depends(require_principal),
):
    session = runtime.get_session(session_id)
    if session is None:
        return problem(404, "session_not_found", "会话不存在或后端已重启")
    services = get_security_services()
    services.identity.assert_session_owner(session_id, principal)
    if session.pending_action is None:
        return problem(409, "no_pending_interaction", "当前没有等待确认的操作")
    with runtime.session_lock(session_id):
        try:
            reply, session = runtime.handle(session_id, body.text)
        except LLMConfigurationError:
            return problem(503, "service_unavailable", "LLM 未配置或暂不可用")
    return present_v2(reply, session_id, session)


@router.post("/sessions/{session_id}/cancel", response_model=V2Envelope)
def cancel(session_id: str, principal: Principal = Depends(require_principal)):
    session = runtime.get_session(session_id)
    if session is None:
        return problem(404, "session_not_found", "会话不存在或后端已重启")
    services = get_security_services()
    services.identity.assert_session_owner(session_id, principal)
    with runtime.session_lock(session_id):
        # 同步取消未消费的强验证挑战，避免确认与取消竞争产生两个结果。
        pending_challenge = getattr(session, "pending_challenge", None)
        if pending_challenge is not None:
            try:
                services.strong_auth.cancel(
                    principal, session_id, pending_challenge.challenge_id
                )
            except StrongAuthError:
                pass
            session.pending_challenge = None
        if session.pending_action is None and session.pending_clarification is None:
            return problem(409, "no_pending_interaction", "当前没有等待确认或取消的操作")
        try:
            reply, session = runtime.handle(session_id, "取消")
        except LLMConfigurationError:
            return problem(503, "service_unavailable", "LLM 未配置或暂不可用")
        envelope = present_v2(reply, session_id, session)
        if envelope.status not in {"failed", "expired"}:
            envelope = envelope.model_copy(update={"status": "cancelled"})
        return envelope


@router.get("/sessions/{session_id}/state", response_model=V2Envelope)
def state(session_id: str, principal: Principal = Depends(require_principal)):
    session = runtime.get_session(session_id)
    if session is None:
        return problem(404, "session_not_found", "会话不存在或后端已重启")
    services = get_security_services()
    services.identity.assert_session_owner(session_id, principal)
    challenge = getattr(session, "pending_challenge", None)
    if challenge is not None:
        challenge = services.strong_auth.load_view(challenge.challenge_id) or challenge
        session.pending_challenge = challenge
    return present_v2(
        runtime.get_last_reply(session_id),
        session_id,
        session,
        challenge=challenge,
        message="当前会话状态",
    )


# --------------------------------------------------------------------- read
@router.post("/sessions/{session_id}/read", response_model=V2Envelope)
def read(
    session_id: str,
    body: V2ReadRequest,
    principal: Principal = Depends(require_principal),
):
    session = runtime.get_session(session_id)
    if session is None:
        return problem(404, "session_not_found", "会话不存在或后端已重启")
    services = get_security_services()
    services.identity.assert_session_owner(session_id, principal)
    reader = get_read_only_service()
    name = body.tool.strip()

    if name in READ_SPECS:
        try:
            decision = services.policy.evaluate(name, principal, {})
        except PolicyError as error:
            return _v2_error(
                "failed", "validation_failed", error.message, session_id=session_id
            )
        if decision.requires_confirmation and not body.authorize_query:
            return present_v2(
                None,
                session_id,
                session,
                decision=decision,
                force_status="need_confirmation",
                message=(
                    "该只读查询按赛题策略需要显式授权；"
                    "请在请求中设置 authorize_query=true 完成确认，"
                    "授权前不会返回任何业务数据。"
                ),
                error_code="query_authorization_required",
                detail_code="query_authorization_required",
            )

    envelope = reader.run(
        session_id,
        ReadRequest(
            tool=body.tool,
            arguments=body.arguments,
            authorize_query=bool(body.authorize_query),
        ),
        principal=principal,
    )
    # 只读通道的 result.kind 与 V1 完全同构；这里只补上 V2 的安全/限制字段。
    return present_v2(
        None,
        session_id,
        session,
        force_status=("success" if envelope.status == "success" else "failed"),
        message=envelope.message,
        error_code=(envelope.error.code if envelope.error is not None else None),
        detail_code=(
            envelope.error.detail_code if envelope.error is not None else None
        ),
    ).model_copy(update={"result": envelope.result})


# ------------------------------------------------------- deterministic writes
def _plan_for(principal, action: str, arguments: dict, idempotency_key: str | None = None):
    services = get_security_services()
    planner = ActionPlanner(services.config, policy=services.policy)
    return planner.prepare(
        action, arguments, principal, idempotency_key=idempotency_key
    )


def _new_request_idempotency_key(action: str) -> str | None:
    """Return a server-managed key for a new preview without mutating state."""
    from security.runners import NEEDS_IDEMPOTENCY_KEY

    if action not in NEEDS_IDEMPOTENCY_KEY:
        return None
    return f"{action}_{secrets.token_hex(16)}"


def _bind_preview(session, action: str, plan) -> None:
    """Store the exact normalized operation shown to this authenticated session."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
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


def _preview_constraints(session, action: str):
    pending = getattr(session, "pending_action", None)
    if pending is not None and pending.tool_name == action:
        return list(pending.constraints)
    draft = getattr(session, "constraint_draft", None)
    if draft is not None and draft.tool_name == action:
        return list(draft.constraints)
    return []


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
    if getattr(session, "context", None) is not None:
        session.context.pending = None
    _clear_preview_binding(session)


def _changed_operation_response(session_id, session, decision=None):
    return present_v2(
        None, session_id, session, decision=decision,
        force_status="need_confirmation",
        message="操作参数已变化，旧预览、确认和验证均已失效；请重新预览并确认。",
        error_code="authorization_parameter_changed",
        detail_code="authorization_parameter_changed",
    )


def _write_response(
    session_id: str,
    session,
    plan,
    principal,
    *,
    action: str,
    confirmed: bool,
    confirm_text: str | None = None,
    requested_arguments: dict | None = None,
):
    """Shared preview/execute response for every deterministic write endpoint."""
    services = get_security_services()
    if not plan.permitted:
        return present_v2(
            None, session_id, session, decision=plan.decision,
            force_status="failed", message=plan.message,
            error_code=_map_action_error(plan.error_code),
            detail_code=plan.error_code,
        )
    decision = plan.decision
    if not confirmed:
        inherited_constraints = _preview_constraints(session, action)
        conflict = _amount_constraint_conflict(plan, inherited_constraints)
        if conflict is not None:
            return present_v2(
                None, session_id, session, decision=decision,
                force_status="failed", message=conflict[1],
                error_code="limit_exceeded", detail_code=conflict[0],
            )
        # 新的预览取代旧的待办/挑战：取消旧挑战，避免"新旧两个待办"竞争
        # 或旧挑战泄漏到新操作上。旧挑战即使仍存在也无法被消费（摘要已变）。
        stale_challenge = getattr(session, "pending_challenge", None)
        if stale_challenge is not None:
            try:
                services.strong_auth.cancel(
                    principal, session_id, stale_challenge.challenge_id
                )
            except StrongAuthError:
                pass
            session.pending_challenge = None
        session.pending_action = None
        preview_summary = _preview_summary(action, plan)
        if decision.requires_strong_auth:
            if inherited_constraints:
                from agent.schemas import ConstraintDraft

                session.constraint_draft = ConstraintDraft(
                    tool_name=action,
                    arguments=dict(requested_arguments or {}),
                    constraints=list(inherited_constraints),
                    reason="strong_auth_required",
                )
            chain = AuditChain()
            session.audit_chain = chain
            view, _code = services.strong_auth.create_challenge(
                principal,
                session_id,
                action,
                decision.action_digest,
                decision.decision_id,
                decision.policy_version,
                decision.permission_level,
            )
            session.pending_challenge = view
            if session.pending_revision > 0:
                pending = _pending_action_for(
                    session_id, action, plan, None,
                    requested_arguments=requested_arguments,
                    constraints=inherited_constraints, require_phrase=True,
                )
                pending.requires_strong_auth = True
                session.pending_action = pending
            _bind_preview(session, action, plan)
            services.gateway._audit(
                chain, "permission_decision", "ok", principal, session_id,
                action=action, decision=decision,
            )
            return present_v2(
                None, session_id, session, decision=decision, challenge=view,
                force_status="need_verification",
                message=(
                    "该操作需要模拟强验证。请通过独立模拟通道"
                    f"（python -m security.otp_inbox latest）获取验证码后提交。{preview_summary}"
                ),
            )
        authorization = services.gateway.authorize_write(
            action, principal, session_id, plan.arguments, chain=AuditChain(),
            idempotency_key=plan.idempotency_key, decision=decision,
        ).authorization
        pending = _pending_action_for(
            session_id, action, plan, authorization,
            requested_arguments=requested_arguments,
            constraints=inherited_constraints,
            require_phrase=(session.pending_revision > 0),
        )
        session.pending_action = pending
        session.constraint_draft = None
        _bind_preview(session, action, plan)
        hint = (
            f" 请回传确认短语：{pending.confirmation_phrase}。"
            if pending.confirmation_phrase
            else " 请在确认请求中提供确认文本。"
        )
        return present_v2(
            None, session_id, session, decision=decision,
            force_status="need_confirmation",
            message="该操作需要明确确认。" + hint + preview_summary,
        )

    # ---- confirmation branch ----
    return _execute_confirmed(
        session_id, session, plan, principal, action=action,
        confirm_text=confirm_text,
    )


def _map_action_error(error_code: str | None) -> str:
    mapping = {
        "recipient_not_found": "not_found",
        "recipient_ambiguous": "validation_failed",
        "recipient_mismatch": "validation_failed",
        "insufficient_balance": "insufficient_funds",
        "daily_limit_exceeded": "limit_exceeded",
        "card_not_found": "not_found",
        "card_unavailable": "risk_rejected",
        "recipient_disabled": "risk_rejected",
        "recipient_untrusted": "risk_rejected",
        "risk_mismatch": "risk_rejected",
        "below_minimum": "validation_failed",
        "product_inactive": "risk_rejected",
        "product_not_found": "not_found",
        "holding_not_found": "not_found",
        "holding_redeemed": "risk_rejected",
        "amount_exceeds_principal": "validation_failed",
        "invalid_amount": "validation_failed",
        "invalid_identifier": "validation_failed",
        "unknown_action": "validation_failed",
    }
    return mapping.get(error_code or "", "operation_failed")


def _preview_summary(action: str, plan) -> str:
    preview = plan.preview or {}
    if action == "transfer_money":
        return (
            f" 预览：付款卡 {preview.get('from_masked_card_number', '')}，"
            f"收款人 {preview.get('recipient_name', '')}，"
            f"金额 {preview.get('amount', 0)} 元。"
        )
    if action == "set_daily_limit":
        return (
            f" 预览：银行卡 {preview.get('masked_card_number', '')}，"
            f"限额 {preview.get('old_limit', 0)} -> {preview.get('new_limit', 0)} 元。"
        )
    if action == "report_card_lost":
        return f" 预览：银行卡 {preview.get('masked_card_number', '')}。"
    if action == "cancel_subscription":
        return f" 预览：订阅 {preview.get('name', '')}。"
    if action in {"buy_wealth_product", "redeem_wealth_product"}:
        return f" 预览：产品 {preview.get('product_name', '')}。"
    return ""


def _pending_action_for(
    session_id: str,
    action: str,
    plan,
    authorization,
    *,
    requested_arguments: dict | None = None,
    constraints=None,
    require_phrase: bool = False,
):
    from agent.schemas import PendingAction

    now = datetime.now(timezone.utc).replace(microsecond=0)
    # 转账保留 V1 的六位动态确认短语（它是"确认"证据，不是 OTP）。
    confirmation_phrase = (
        f"确认转账 {secrets.randbelow(1_000_000):06d}"
        if action == "transfer_money"
        else f"确认操作 {secrets.randbelow(1_000_000):06d}" if require_phrase else None
    )
    if confirmation_phrase is not None:
        confirmation_phrase = runtime.get_session(session_id).reserve_confirmation_phrase(
            confirmation_phrase
        )
    return PendingAction(
        tool_name=action,
        requested_arguments=dict(requested_arguments or {}),
        execution_arguments=dict(plan.arguments),
        confirmation_message="该操作需要明确确认。",
        confirmation_phrase=confirmation_phrase,
        created_at=now,
        expires_at=now + timedelta(seconds=_CONFIRM_TTL_SECONDS),
        action=action,
        permission_level=plan.decision.permission_level,
        decision_id=plan.decision.decision_id,
        policy_version=plan.decision.policy_version,
        action_digest=plan.decision.action_digest,
        requires_strong_auth=False,
        requires_confirmation=True,
        execution_permitted=True,
        authorization_id=(
            authorization.authorization_id if authorization is not None else None
        ),
        constraints=list(constraints or []),
    )


_CONFIRM_TTL_SECONDS = 120


def _execute_confirmed(session_id, session, plan, principal, *, action, confirm_text=None):
    from execution.confirmed import _execute_confirmed as execute
    result=execute(session_id,session,plan,principal,action=action,confirm_text=confirm_text)
    reply=None
    if result.status == 'success':
        from agent.schemas import AgentReply, ToolResult
        data=dict(result.data or {})
        data.pop('_idempotency_key',None)
        data.pop('write_verification',None)
        reply=AgentReply(action='tool_result',tool_name=action,message=result.message,
            tool_result=ToolResult(status='success',message=result.message,data=data))
        runtime.set_last_reply(session_id,reply)
    if getattr(session,'context',None) is not None and session.pending_action is None:
        session.context.pending=None
    return present_v2(reply,session_id,session,decision=result.decision,challenge=result.challenge,
        force_status=result.status,message=result.message,
        error_code=normalize_v2_error_code(_map_action_error(result.error_code)) if result.error_code else None,
        detail_code=result.detail_code or result.error_code)


def _outcome_to_response(session_id, session, outcome, principal, *, action: str):
    if outcome.success:
        services = get_security_services()
        tool_result = _tool_result_from_outcome(action, outcome)
        reply = __import__("agent.schemas", fromlist=["AgentReply"]).AgentReply(
            message=outcome.message,
            action="tool_result",
            tool_name=action,
            tool_result=tool_result,
        )
        runtime.set_last_reply(session_id, reply)
        return present_v2(
            reply, session_id, session, decision=outcome.decision,
            force_status="success",
        )
    if outcome.stage == "submitted_unverified":
        services = get_security_services()
        return present_v2(
            None, session_id, session, decision=outcome.decision,
            force_status="failed", message=outcome.message,
            error_code="verification_failed",
            detail_code="verification_failed",
        )
    return present_v2(
        None, session_id, session, decision=outcome.decision,
        force_status="failed", message=outcome.message,
        error_code=normalize_v2_error_code(_map_action_error(outcome.error_code)),
        detail_code=outcome.error_code,
    )


def _tool_result_from_outcome(action: str, outcome):
    from agent.schemas import ToolResult

    data = dict(outcome.data or {})
    # 内部字段不下发给客户端：幂等键、内部写后核验标记。
    data.pop("_idempotency_key", None)
    data.pop("write_verification", None)
    return ToolResult(status="success", data=data, message=outcome.message)


@router.post("/sessions/{session_id}/transfer", response_model=V2Envelope)
def transfer(
    session_id: str,
    body: dict,
    principal: Principal = Depends(require_principal),
):
    return _deterministic_write(session_id, principal, "transfer_money", body)


@router.post("/sessions/{session_id}/cancel-subscription", response_model=V2Envelope)
def cancel_subscription_endpoint(
    session_id: str,
    body: dict,
    principal: Principal = Depends(require_principal),
):
    return _deterministic_write(session_id, principal, "cancel_subscription", body)


@router.post("/sessions/{session_id}/report-card-lost", response_model=V2Envelope)
def report_card_lost_endpoint(
    session_id: str,
    body: dict,
    principal: Principal = Depends(require_principal),
):
    return _deterministic_write(session_id, principal, "report_card_lost", body)


@router.post("/sessions/{session_id}/daily-limit", response_model=V2Envelope)
def daily_limit_endpoint(
    session_id: str,
    body: dict,
    principal: Principal = Depends(require_principal),
):
    return _deterministic_write(session_id, principal, "set_daily_limit", body)


@router.post("/sessions/{session_id}/wealth/purchase", response_model=V2Envelope)
def wealth_purchase_endpoint(
    session_id: str,
    body: dict,
    principal: Principal = Depends(require_principal),
):
    return _deterministic_write(session_id, principal, "buy_wealth_product", body)


@router.post("/sessions/{session_id}/wealth/redemption", response_model=V2Envelope)
def wealth_redemption_endpoint(
    session_id: str,
    body: dict,
    principal: Principal = Depends(require_principal),
):
    return _deterministic_write(session_id, principal, "redeem_wealth_product", body)


def _reject_injected_fields(payload: dict) -> str | None:
    from security.schemas import find_protected_field

    return find_protected_field(payload, "body")


def _normalized_confirmation(text: str | None) -> str | None:
    return (text or "").strip(" \t\r\n。．.!！?？,，;；:：").casefold() or None


def _retry_arguments(arguments: dict) -> dict:
    """Canonical validated public input; never an authorization identity."""
    from decimal import Decimal

    result = dict(arguments)
    for field in ("amount", "daily_limit"):
        if result.get(field) is not None:
            result[field] = Decimal(str(result[field]))
    return result


def _deterministic_write(session_id: str, principal: Principal, action: str, body: dict):
    from lifecycle.service import session_operation
    from lifecycle.store import TaskError
    session = runtime.get_session(session_id)
    if session is None:
        return problem(404, "session_not_found", "会话不存在或后端已重启")
    with runtime.session_lock(session_id):
        try:
            payload = {k:v for k,v in body.items() if k not in {'confirmed','confirm_text','authorize'}}
            # Invalid requests must not detach a live task or persist its old preview as a new task.
            if _reject_injected_fields(payload):
                return _deterministic_write_impl(session_id, principal, action, body)
            try:
                _validate_action_payload(action, payload)
            except Exception:
                return _deterministic_write_impl(session_id, principal, action, body)
            session.lifecycle_requested = {k: v for k, v in body.items()
                if k not in {"confirmed", "confirm_text"}}
            with session_operation(session, fresh=not body.get("confirmed", False)):
                return _deterministic_write_impl(session_id, principal, action, body)
        except TaskError as exc:
            return present_v2(None, session_id, session, force_status="failed",
                error_code="authorization_required", message=exc.message)


def _deterministic_write_impl(session_id: str, principal: Principal, action: str, body: dict):
    from pydantic import ValidationError
    from security.runners import get_runner

    session = runtime.get_session(session_id)
    if session is None:
        return problem(404, "session_not_found", "会话不存在或后端已重启")
    services = get_security_services()
    services.identity.assert_session_owner(session_id, principal)

    payload = dict(body or {})
    if not isinstance(payload, dict):
        return problem(422, "invalid_request", "请求体必须是 JSON 对象")
    confirmed = bool(payload.pop("confirmed", False))
    confirm_text = payload.pop("confirm_text", None)
    authorize_query = payload.pop("authorize", False)
    injected = _reject_injected_fields(payload)
    if injected:
        # 确定的违规：计入可疑请求限制（仅结构化字段名，不给用户贴标签）
        services.restrictions.record_suspicious(
            principal, "protected_field_injection", session_id=session_id
        )
        return present_v2(
            None, session_id, session, force_status="failed",
            message=f"请求包含由后端安全层管理的内部字段：{injected}",
            error_code="validation_failed",
            detail_code="protected_field_injection",
        )
    runner = get_runner(action)
    if runner is None:
        return problem(422, "validation_failed", "未知写操作")
    try:
        validated = runner.argument_model.model_validate(payload)
    except (ValidationError, AttributeError):
        try:
            validated = _validate_action_payload(action, payload)
        except Exception as error:
            return present_v2(
                None, session_id, session, force_status="failed",
                message="写操作参数校验失败",
                error_code="validation_failed",
                detail_code="invalid_arguments",
            )
    arguments = (
        validated.model_dump(mode="json") if hasattr(validated, "model_dump") else dict(validated)
    )

    with runtime.session_lock(session_id):
        if confirmed:
            pending = getattr(session, "pending_action", None)
            challenge = getattr(session, "pending_challenge", None)
            # A live interaction always wins over the last result cache.
            # Its confirmation proof must be checked before planning a body
            # that may belong to an older operation (and invalidating it).
            if pending is not None and pending.confirmation_phrase and (
                _normalized_confirmation(confirm_text)
                != _normalized_confirmation(pending.confirmation_phrase)
            ):
                return present_v2(
                    None, session_id, session, force_status="need_confirmation",
                    message="确认凭据不属于当前预览，未执行；当前预览仍有效。",
                    error_code="authorization_parameter_changed",
                    detail_code="confirmation_mismatch",
                )
            completed_key = session.completed_idempotency_key
            completed_response = session.completed_response
            if (
                pending is None and challenge is None
                and not session.request_action_digest
                and completed_response is not None
                and session.completed_action == action
                and session.completed_pending_revision == session.pending_revision
                and _normalized_confirmation(confirm_text)
                    == session.completed_confirm_text
            ):
                # Exact validated input replay does not re-check today's
                # balance/product status: the committed operation already ran.
                same_request = (
                    _retry_arguments(arguments)
                    == _retry_arguments(session.completed_arguments or {})
                )
                if not same_request:
                    replay_plan = _plan_for(
                        principal, action, arguments, idempotency_key=completed_key
                    )
                    same_request = (
                        replay_plan.permitted
                        and replay_plan.decision is not None
                        and replay_plan.decision.action_digest
                            == session.completed_action_digest
                    )
                if same_request:
                    cached_payload = completed_response.model_dump(mode="json")
                    data = ((cached_payload.get("result") or {}).get("data") or {})
                    if isinstance(data, dict) and "idempotent_replay" in data:
                        data["idempotent_replay"] = True
                    return V2Envelope.model_validate(cached_payload)

            if pending is not None:
                bound_action = pending.action or pending.tool_name
                bound_digest = pending.action_digest
                bound_arguments = dict(pending.execution_arguments)
                key = bound_arguments.get("idempotency_key")
            elif challenge is not None:
                bound_action = getattr(session, "request_action", None)
                bound_digest = getattr(session, "request_action_digest", None)
                bound_arguments = dict(
                    getattr(session, "request_arguments", None) or {}
                )
                key = getattr(session, "request_idempotency_key", None)
                expires_at = getattr(session, "request_expires_at", None)
                if expires_at is not None and datetime.now(timezone.utc) >= expires_at:
                    _invalidate_preview(session_id, session, principal)
                    return present_v2(
                        None, session_id, session, force_status="expired",
                        message="该操作预览已过期，请重新预览并确认。",
                        error_code="authorization_required",
                        detail_code="pending_action_expired",
                    )
            else:
                return present_v2(
                    None, session_id, session,
                    force_status="need_confirmation",
                    message="当前没有与该请求绑定的有效预览或待确认操作。",
                    error_code="authorization_required",
                    detail_code="no_pending_interaction",
                )
            if bound_action != action or not bound_digest or not bound_arguments:
                _invalidate_preview(session_id, session, principal)
                return _changed_operation_response(session_id, session)

            plan = _plan_for(principal, action, arguments, idempotency_key=key)
            if not plan.permitted or plan.decision is None:
                # A confirmation request that can no longer normalize to the
                # preview is not allowed to leave the old proof reusable.
                _invalidate_preview(session_id, session, principal)
                return _write_response(
                    session_id, session, plan, principal, action=action,
                    confirmed=True, confirm_text=confirm_text,
                    requested_arguments=arguments,
                )
            if (
                plan.decision.action_digest != bound_digest
                or dict(plan.arguments) != bound_arguments
            ):
                _invalidate_preview(session_id, session, principal)
                return _changed_operation_response(
                    session_id, session, plan.decision
                )
        else:
            key = _new_request_idempotency_key(action)
            plan = _plan_for(principal, action, arguments, idempotency_key=key)

        response = _write_response(
            session_id, session, plan, principal, action=action, confirmed=confirmed,
            confirm_text=confirm_text, requested_arguments=arguments,
        )
        if isinstance(response, V2Envelope) and response.status == "success":
            session.completed_request_signature = plan.decision.action_digest
            session.completed_idempotency_key = key
            session.completed_action = action
            session.completed_action_digest = plan.decision.action_digest
            session.completed_response = response.model_copy(deep=True)
            session.completed_confirm_text = _normalized_confirmation(confirm_text)
            session.completed_arguments = dict(arguments)
            session.completed_pending_revision = session.pending_revision
        return response


def _validate_action_payload(action: str, payload: dict):
    from pydantic import BaseModel, ConfigDict, Field, model_validator

    from agent.schemas import (
        BuyWealthProductArguments,
        RedeemWealthProductArguments,
        ReportCardLostArguments,
        SetDailyLimitArguments,
        TransferMoneyArguments,
    )

    class _CancelSubscriptionV2(BaseModel):
        """V2 取消订阅接受订阅编号或订阅名称（名称在归属范围内解析）。"""

        model_config = ConfigDict(extra="forbid")

        subscription_id: int | None = Field(default=None, gt=0)
        subscription: str | None = Field(default=None, min_length=1, max_length=100)

        @model_validator(mode="after")
        def _one_selector(self):
            if self.subscription_id is None and self.subscription is None:
                raise ValueError("subscription_id 和 subscription 至少提供一个")
            return self

    models: dict[str, type[BaseModel]] = {
        "transfer_money": TransferMoneyArguments,
        "cancel_subscription": _CancelSubscriptionV2,
        "report_card_lost": ReportCardLostArguments,
        "set_daily_limit": SetDailyLimitArguments,
        "buy_wealth_product": BuyWealthProductArguments,
        "redeem_wealth_product": RedeemWealthProductArguments,
    }
    model = models.get(action)
    if model is None:
        raise ValueError("unknown action")
    validated = model.model_validate(payload)
    # 只把显式提供的选择器传给 runner，避免把 None 覆盖进去。
    return {
        key: value
        for key, value in validated.model_dump(mode="json").items()
        if value is not None
    }


# ----------------------------------------------------------------- challenge
@router.post(
    "/sessions/{session_id}/challenges",
    response_model=ChallengeStatusResponse,
)
def create_challenge(
    session_id: str,
    body: SendChallengeRequest,
    principal: Principal = Depends(require_principal),
):
    session = runtime.get_session(session_id)
    if session is None:
        return problem(404, "session_not_found", "会话不存在或后端已重启")
    services = get_security_services()
    services.identity.assert_session_owner(session_id, principal)
    action = body.action
    if action not in ACTION_POLICIES:
        return problem(422, "validation_failed", "未知操作")
    payload = dict(body.arguments or {})
    if _reject_injected_fields(payload):
        services.restrictions.record_suspicious(
            principal, "protected_field_injection", session_id=session_id
        )
        return problem(422, "validation_failed", "挑战参数包含内部字段")
    try:
        validated = _validate_action_payload(action, payload)
        arguments = (
            validated.model_dump(mode="json")
            if hasattr(validated, "model_dump")
            else dict(validated)
        )
    except Exception:
        return problem(422, "validation_failed", "挑战参数校验失败")
    with runtime.session_lock(session_id):
        key = _new_request_idempotency_key(action)
        plan = _plan_for(principal, action, arguments, idempotency_key=key)
        if not plan.permitted or plan.decision is None:
            return problem(422, "validation_failed", plan.message or "参数校验失败")
        if not plan.decision.requires_strong_auth:
            # 黄色操作只需要明确确认，不创建 OTP 挑战（避免把 MFA 塞进普通确认）。
            return problem(
                409,
                "validation_failed",
                "该操作需要明确确认而不是模拟 OTP；请在对应写端点完成确认。",
            )
        constraints = _preview_constraints(session, action)
        conflict = _amount_constraint_conflict(plan, constraints)
        if conflict is not None:
            return problem(409, "limit_exceeded", conflict[1])
        _invalidate_preview(session_id, session, principal)
        view, _code = services.strong_auth.create_challenge(
            principal,
            session_id,
            action,
            plan.decision.action_digest,
            plan.decision.decision_id,
            plan.decision.policy_version,
            plan.decision.permission_level,
        )
        session.pending_challenge = view
        _bind_preview(session, action, plan)
        return ChallengeStatusResponse(
            verification=verification_view(view),
            security=security_view(plan.decision),
        )


@router.post(
    "/sessions/{session_id}/challenges/{challenge_id}/verify",
    response_model=V2Envelope,
)
def verify_challenge(
    session_id: str,
    challenge_id: str,
    body: VerifyChallengeRequest,
    principal: Principal = Depends(require_principal),
):
    session = runtime.get_session(session_id)
    if session is None:
        return problem(404, "session_not_found", "会话不存在或后端已重启")
    services = get_security_services()
    services.identity.assert_session_owner(session_id, principal)
    try:
        view = services.strong_auth.verify(
            principal, session_id, challenge_id, body.code
        )
    except StrongAuthError as error:
        services.restrictions.record_suspicious(
            principal, error.code, session_id=session_id
        ) if error.code in {
            "challenge_session_mismatch",
            "challenge_parameter_changed",
        } else None
        restriction = services.restrictions.strong_auth_lock(
            principal, session_id=session_id
        )
        status = "locked" if restriction is not None and restriction.active else "failed"
        return present_v2(
            None, session_id, session, challenge=error.view,
            restriction=restriction, force_status=status,
            message=error.message, error_code=error.code, detail_code=error.code,
        )
    session.pending_challenge = view
    return present_v2(
        None, session_id, session, challenge=view,
        force_status="need_verification",
        message=(
            "验证码校验通过。请重新调用对应的写端点在确认位置完成执行；"
            "该验证只授权本次操作，不会给整个任务链放行。"
        ),
    )


@router.post(
    "/sessions/{session_id}/challenges/{challenge_id}/resend",
    response_model=ChallengeStatusResponse,
)
def resend_challenge(
    session_id: str,
    challenge_id: str,
    principal: Principal = Depends(require_principal),
):
    session = runtime.get_session(session_id)
    if session is None:
        return problem(404, "session_not_found", "会话不存在或后端已重启")
    services = get_security_services()
    services.identity.assert_session_owner(session_id, principal)
    try:
        view, _code = services.strong_auth.resend(principal, session_id, challenge_id)
    except StrongAuthError as error:
        return problem(409, error.code, error.message)
    session.pending_challenge = view
    return ChallengeStatusResponse(verification=verification_view(view))


@router.get(
    "/sessions/{session_id}/challenges/{challenge_id}",
    response_model=ChallengeStatusResponse,
)
def challenge_status(
    session_id: str,
    challenge_id: str,
    principal: Principal = Depends(require_principal),
):
    session = runtime.get_session(session_id)
    if session is None:
        return problem(404, "session_not_found", "会话不存在或后端已重启")
    services = get_security_services()
    services.identity.assert_session_owner(session_id, principal)
    view = services.strong_auth.load_view(challenge_id)
    if view is None or view.session_id != session_id:
        return problem(404, "challenge_not_found", "验证挑战不存在")
    restriction = services.restrictions.strong_auth_lock(
        principal, session_id=session_id
    )
    return ChallengeStatusResponse(
        verification=verification_view(view),
        restriction=restriction_view(restriction),
    )


@router.delete(
    "/sessions/{session_id}/challenges/{challenge_id}",
    response_model=ChallengeStatusResponse,
)
def cancel_challenge(
    session_id: str,
    challenge_id: str,
    principal: Principal = Depends(require_principal),
):
    session = runtime.get_session(session_id)
    if session is None:
        return problem(404, "session_not_found", "会话不存在或后端已重启")
    services = get_security_services()
    services.identity.assert_session_owner(session_id, principal)
    try:
        view = services.strong_auth.cancel(principal, session_id, challenge_id)
    except StrongAuthError as error:
        return problem(404, error.code, error.message)
    session.pending_challenge = None
    return ChallengeStatusResponse(verification=verification_view(view))


# ------------------------------------------------------------------- status
@router.get("/sessions/{session_id}/security", response_model=SecurityStatusResponse)
def security_status(session_id: str, principal: Principal = Depends(require_principal)):
    session = runtime.get_session(session_id)
    if session is None:
        return problem(404, "session_not_found", "会话不存在或后端已重启")
    services = get_security_services()
    services.identity.assert_session_owner(session_id, principal)
    restrictions = []
    lock = services.restrictions.strong_auth_lock(principal, session_id=session_id)
    if lock is not None:
        restrictions.append(restriction_view(lock))
    suspicious = services.restrictions.suspicious_restriction(
        principal, session_id=session_id
    )
    if suspicious is not None:
        restrictions.append(restriction_view(suspicious))
    breakers = []
    for tool in _BREAKER_TOOLS:
        status = services.restrictions.breaker_status(tool)
        if status.state != "closed" or status.failure_count:
            breakers.append(
                BreakerView(
                    tool=status.tool,
                    state=status.state,
                    failure_count=status.failure_count,
                    open_until=status.open_until,
                    last_error_code=status.last_error_code,
                )
            )
    challenge = getattr(session, "pending_challenge", None)
    view = services.strong_auth.load_view(challenge.challenge_id) if challenge else None
    return SecurityStatusResponse(
        session_id=session_id,
        principal=PrincipalView(
            principal_id=principal.principal_id,
            user_id=principal.user_id,
            display_name=principal.display_name,
            kind=principal.kind,
            session_protocol="v2",
        ),
        policy_version=services.config.policy_version,
        permission_requirements={
            name: PermissionRequirement(
                action=name,
                permission_level=policy.base_level,
                requires_confirmation=policy.requires_confirmation,
                required_authentication=(
                    "mock_otp"
                    if policy.requires_strong_auth
                    else ("confirmation" if policy.requires_confirmation else "none")
                ),
                requires_strong_auth=policy.requires_strong_auth,
            )
            for name, policy in ACTION_POLICIES.items()
        },
        restrictions=[item for item in restrictions if item is not None],
        pending_challenge=verification_view(view),
        circuit_breakers=breakers,
    )


@router.get("/restrictions", response_model=SecurityStatusResponse)
def restrictions_overview(session_id: str, principal: Principal = Depends(require_principal)):
    return security_status(session_id, principal)


# -------------------------------------------------- local mock OTP inbox
@router.get("/demo/mock-otp-inbox")
def mock_otp_inbox(
    authorization: str | None = Header(default=None),
    session_credential: str | None = Header(
        default=None, alias="X-Session-Credential"
    ),
    session_id: str | None = None,
    challenge_id: str | None = None,
):
    """本地演示用的模拟验证码通道。

    仅当显式配置 ``BA07_MOCK_OTP_INBOX_TOKEN`` 并携带匹配的 Bearer 令牌时可用；
    默认进程内随机令牌意味着该端点默认不可读。生产模式请改用
    ``python -m security.otp_inbox``。
    """
    import os

    services = get_security_services()
    configured = os.environ.get("BA07_MOCK_OTP_INBOX_TOKEN", "").strip()
    if not services.config.mock_otp_inbox_enabled or not configured:
        return problem(
            404,
            "not_found",
            "模拟验证码通道未启用；请使用 python -m security.otp_inbox",
        )
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token, configured):
        return problem(401, "unauthorized", "模拟验证码通道凭据无效")
    if not session_id:
        return problem(422, "validation_failed", "模拟收件箱必须绑定 V2 会话")
    binding = services.identity.get_session(session_id)
    credential = (session_credential or "").removeprefix("Bearer ").strip()
    if (
        binding is None
        or binding.revoked
        or not credential
        or not secrets.compare_digest(credential, binding.session_id)
    ):
        return problem(401, "unauthorized", "模拟收件箱会话凭据无效")
    principal = services.identity.resolve_principal(session_id)
    if principal is None:
        return problem(401, "credential_expired", "模拟收件箱会话已失效")
    entry = inbox.latest(
        challenge_id,
        principal_id=principal.principal_id,
        session_id=session_id,
    )
    if entry is None:
        return problem(404, "not_found", "没有可用的模拟验证码")
    return {
        "contract_version": CONTRACT_VERSION_V2,
        "challenge_id": entry.get("challenge_id"),
        "action": entry.get("action"),
        "code": entry.get("code"),
        "expires_at": entry.get("expires_at"),
        "notice": "本通道仅用于本机模拟演示，不是真实短信或生产 MFA。",
    }
