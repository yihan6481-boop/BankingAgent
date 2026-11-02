"""Strict Pydantic models shared by the V0.7.0 security layer.

Everything that crosses a module boundary (policy decisions, principals,
challenges, authorization contexts, cost snapshots, restrictions and audit
records) is modelled here.  Nothing is passed around as a bare ``Any`` or a
free-form ``dict`` so the V2 protocol and the audit chain stay checkable.
"""

from __future__ import annotations

import json
import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PermissionLevel = Literal["green", "yellow", "red"]
AuthenticationMethod = Literal["none", "confirmation", "mock_otp", "payment"]
# 对象种类：决定归属检查读哪张表
ObjectKind = Literal[
    "card",
    "recipient",
    "subscription",
    "wealth_holding",
    "wealth_product",
    "transfer_order",
    "wealth_order",
    "transaction",
    "principal",
    "session",
    "other",
]
# 审计阶段：与需求里的决策链一一对应
AuditStage = Literal[
    "plan_validation",
    "permission_decision",
    "preview",
    "confirmation",
    "strong_auth",
    "revalidation",
    "execution",
    "verification",
    "final_outcome",
    "restriction",
    "circuit_breaker",
]
AuditOutcome = Literal["ok", "denied", "failed", "pending", "skipped"]

RestrictionKind = Literal[
    "strong_auth_lockout",
    "suspicious_restriction",
    "tool_circuit_open",
]
BreakerState = Literal["closed", "open", "half_open"]

PROTECTED_MESSAGE = "该字段由后端安全层管理，请求不得提供。"


class StrictSecurityModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResourceRef(StrictSecurityModel):
    """Ownership reference passed to the object-ownership checker."""

    kind: ObjectKind
    object_id: int | str | None = None
    # 归属字段名（owner column）与后端实际表名由检查器映射，避免调用方编造
    label: str | None = None

    @field_validator("object_id")
    @classmethod
    def validate_object_id(cls, value):
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("对象编号不能是布尔值")
        if isinstance(value, int):
            if value <= 0:
                raise ValueError("对象编号必须是正整数")
            return value
        if isinstance(value, str):
            text = value.strip()
            if not text or len(text) > 128:
                raise ValueError("对象编号必须是长度不超过128的非空文本")
            return text
        raise ValueError("对象编号只能是整数或文本")


class Principal(StrictSecurityModel):
    """Server-side identity.  Never reconstructed from a request body."""

    principal_id: str = Field(min_length=1, max_length=64)
    user_id: int = Field(ge=0)
    display_name: str = Field(min_length=1, max_length=64)
    kind: Literal["simulated_user", "legacy_demo", "system"] = "simulated_user"
    authenticated_at: datetime
    credential_id: str | None = Field(default=None, max_length=64)

    @property
    def is_legacy_demo(self) -> bool:
        return self.kind == "legacy_demo"

    @field_validator("authenticated_at")
    @classmethod
    def require_timezone(cls, value: datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("authenticated_at 必须包含时区")
        return value


class Credential(StrictSecurityModel):
    credential_id: str
    principal_id: str
    user_id: int
    username: str
    password_hash: str
    display_name: str
    created_at: datetime
    revoked: bool = False


class SessionBinding(StrictSecurityModel):
    """Session record; the credential is bound to the session, not the body."""

    session_id: str
    principal_id: str
    user_id: int
    credential_id: str
    protocol: Literal["v1", "v2"]
    created_at: datetime
    last_seen_at: datetime
    credential_expires_at: datetime
    revoked: bool = False


class BusinessStateSnapshot(StrictSecurityModel):
    """Checked business facts used by the policy decision."""

    card_daily_limit_cents: int | None = Field(default=None, ge=0)
    transfer_daily_total_cents: int | None = Field(default=None, ge=0)
    amount_cents: int | None = Field(default=None, ge=0)
    card_status: str | None = None
    recipient_status: str | None = None
    recipient_trusted: bool | None = None
    product_status: str | None = None
    product_risk_level: str | None = None
    user_risk_level: str | None = None
    holding_status: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if value is not None
        }


class PolicyDecision(StrictSecurityModel):
    """Complete, auditable output of the Risk Engine / Permission Policy."""

    decision_id: str = Field(min_length=8, max_length=64)
    policy_version: str = Field(min_length=1, max_length=32)
    permission_level: PermissionLevel
    allowed: bool
    reason_codes: list[str] = Field(default_factory=list)
    requires_confirmation: bool
    required_authentication: AuthenticationMethod
    requires_strong_auth: bool
    action: str = Field(min_length=1, max_length=64)
    subject: str = Field(min_length=1, max_length=128)
    action_digest: str = Field(min_length=16, max_length=128)
    summary: str = Field(min_length=1, max_length=400)
    resources: list[ResourceRef] = Field(default_factory=list)
    business_state: BusinessStateSnapshot = Field(
        default_factory=BusinessStateSnapshot
    )
    threshold_cents: int | None = Field(default=None, ge=0)
    projected_total_cents: int | None = Field(default=None, ge=0)
    decided_at: datetime
    idempotency_managed: bool = False

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, values):
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("reason_codes 必须是非空稳定代码")
            if len(value) > 64:
                raise ValueError("reason_codes 代码过长")
        if len(values) != len(set(values)):
            raise ValueError("reason_codes 不得重复")
        return values

    @field_validator("decided_at")
    @classmethod
    def require_timezone(cls, value: datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decided_at 必须包含时区")
        return value

    @property
    def safe_for_llm(self) -> dict[str, Any]:
        """The subset that may be shown to the model / returned to clients."""
        return {
            "decision_id": self.decision_id,
            "policy_version": self.policy_version,
            "permission_level": self.permission_level,
            "allowed": self.allowed,
            "reason_codes": list(self.reason_codes),
            "requires_confirmation": self.requires_confirmation,
            "required_authentication": self.required_authentication,
            "summary": self.summary,
        }


class AuthorizationContext(StrictSecurityModel):
    """Proof that a specific write action was authorized and consumed.

    Instances are produced **only** by the security layer.  They are never
    accepted from an LLM response, an API body or a service default value.
    """

    authorization_id: str = Field(min_length=8, max_length=64)
    principal_id: str = Field(min_length=1, max_length=64)
    user_id: int = Field(ge=0)
    session_id: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=64)
    action_digest: str = Field(min_length=16, max_length=128)
    permission_level: PermissionLevel
    policy_version: str = Field(min_length=1, max_length=32)
    decision_id: str = Field(min_length=8, max_length=64)
    challenge_id: str | None = Field(default=None, max_length=64)
    strong_auth_method: AuthenticationMethod = "confirmation"
    issued_at: datetime
    consumed: bool = False
    consumed_at: datetime | None = None

    @field_validator("issued_at", "consumed_at")
    @classmethod
    def require_timezone(cls, value):
        if value is None:
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("授权时间必须包含时区")
        return value


class ChallengePublicView(StrictSecurityModel):
    """Client-visible challenge state.  Never contains the OTP value."""

    challenge_id: str
    action: str
    session_id: str
    permission_level: PermissionLevel
    method: Literal["none", "confirmation", "mock_otp", "totp", "payment"] = "mock_otp"
    status: Literal["pending", "verified", "consumed", "expired", "cancelled",
                    "failed"] = "pending"
    attempts_remaining: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    created_at: datetime
    expires_at: datetime
    resend_available_at: datetime | None = None
    masked_delivery_hint: str | None = None
    unlock_at: datetime | None = None
    reason_codes: list[str] = Field(default_factory=list)

    @field_validator("created_at", "expires_at", "resend_available_at", "unlock_at")
    @classmethod
    def require_timezone(cls, value):
        if value is None:
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("挑战时间必须包含时区")
        return value


class RestrictionStatus(StrictSecurityModel):
    """Structured restriction state; never parsed from a Chinese message."""

    kind: RestrictionKind
    scope: str = Field(min_length=1, max_length=64)
    reason_code: str = Field(min_length=1, max_length=64)
    active: bool
    locked_at: datetime | None = None
    unlock_at: datetime | None = None
    retry_after_seconds: int | None = Field(default=None, ge=0)
    detail: str | None = Field(default=None, max_length=200)

    @field_validator("locked_at", "unlock_at")
    @classmethod
    def require_timezone(cls, value):
        if value is None:
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("限制时间必须包含时区")
        return value


class BreakerStatus(StrictSecurityModel):
    tool: str
    state: BreakerState
    failure_count: int = Field(ge=0)
    opened_at: datetime | None = None
    open_until: datetime | None = None
    half_open_probes: int = Field(default=0, ge=0)
    last_error_code: str | None = Field(default=None, max_length=64)

    @field_validator("opened_at", "open_until")
    @classmethod
    def require_timezone(cls, value):
        if value is None:
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("熔断时间必须包含时区")
        return value


class AuditRecord(StrictSecurityModel):
    """One persisted, desensitized decision-chain fact."""

    record_id: str = Field(min_length=8, max_length=64)
    request_id: str = Field(min_length=8, max_length=64)
    sequence: int = Field(ge=0)
    stage: AuditStage
    outcome: AuditOutcome
    principal_id: str | None = Field(default=None, max_length=64)
    user_id: int | None = None
    session_id: str | None = Field(default=None, max_length=64)
    task_id: str | None = Field(default=None, max_length=64)
    step_id: str | None = Field(default=None, max_length=64)
    action: str | None = Field(default=None, max_length=64)
    policy_version: str | None = Field(default=None, max_length=32)
    permission_level: PermissionLevel | None = None
    decision_id: str | None = Field(default=None, max_length=64)
    challenge_id: str | None = Field(default=None, max_length=64)
    authorization_id: str | None = Field(default=None, max_length=64)
    authentication_method: Literal["none", "confirmation", "mock_otp", "totp", "payment"] | None = None
    reason_codes: list[str] = Field(default_factory=list)
    error_code: str | None = Field(default=None, max_length=64)
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at 必须包含时区")
        return value


PROTECTED_INTERNAL_FIELDS = (
    "confirmed",
    "confirmation",
    "confirmation_phrase",
    "auth_verified",
    "authenticated",
    "authorization",
    "authorization_id",
    "authorization_context",
    "permission_level",
    "permission_decision",
    "risk_level",
    "policy_version",
    "decision_id",
    "challenge_id",
    "otp",
    "otp_code",
    "verification_code",
    "code_hash",
    "principal",
    "principal_id",
    "owner_user_id",
    "user_id",
    "session_id",
    "credential",
    "access_token",
    "token",
    "api_key",
    "idempotency_key",
    "amount_cents",
    "new_limit_cents",
    "execution_arguments",
    "task_status",
    "step_status",
    "verification_status",
    "result",
    "sql",
)


def find_protected_field(value: Any, path: str = "body") -> str | None:
    """Return the dotted path of the first forbidden internal field, if any.

    This is the shared injection guard used by the V2 API layer, the
    deterministic read channel and the workflow validator.  It never inspects
    natural-language free text for "attacker" keywords: only structured field
    names and clearly internal artifacts are rejected.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().casefold()
            if normalized in PROTECTED_INTERNAL_FIELDS:
                return f"{path}.{key}"
            if normalized.startswith("__") or normalized.endswith("__"):
                return f"{path}.{key}"
            if normalized.endswith("_sql") or normalized.startswith("sql_"):
                return f"{path}.{key}"
            nested = find_protected_field(item, f"{path}.{key}")
            if nested:
                return nested
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            nested = find_protected_field(item, f"{path}[{index}]")
            if nested:
                return nested
    return None


def compute_action_digest(action: str, arguments: dict[str, Any]) -> str:
    """Stable digest binding an authorization to one concrete action.

    Any change to amount, account, recipient, product, target step or policy
    version produces a different digest, which invalidates old confirmations
    and challenges.
    """
    payload = json.dumps(
        {"action": action, "arguments": _canonical(arguments)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if value is None:
        return None
    return str(value)
