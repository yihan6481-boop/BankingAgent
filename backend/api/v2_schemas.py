"""Contract 2.0 request/response models.

Every state exposed by ``/api/v2`` has an explicit Pydantic model here and a
matching TypeScript interface in ``contracts/v2/UI_TYPES_V2.ts``.  No response
field is typed as ``Any`` and no state is hidden inside a free-form ``dict``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from security.schemas import (
    AuthenticationMethod,
    PermissionLevel,
)

CONTRACT_VERSION_V2 = "2.0"

V2Status = Literal[
    "ready",
    "need_info",
    "need_confirmation",
    "need_verification",
    "executing",
    "success",
    "cancelled",
    "expired",
    "failed",
    "locked",
    "restricted",
]

V2ErrorCode = Literal[
    "invalid_request",
    "unauthorized",
    "session_not_found",
    "session_principal_mismatch",
    "session_revoked",
    "no_pending_interaction",
    "credential_expired",
    "not_found",
    "challenge_not_found",
    "challenge_not_verified",
    "challenge_not_pending",
    "challenge_expired",
    "challenge_cancelled",
    "challenge_failed",
    "challenge_attempts_exhausted",
    "challenge_already_used",
    "challenge_session_mismatch",
    "challenge_action_mismatch",
    "challenge_parameter_changed",
    "invalid_code",
    "resend_too_soon",
    "strong_auth_locked",
    "strong_auth_required",
    "suspicious_restricted",
    "tool_circuit_open",
    "permission_denied",
    "permission_escalated",
    "policy_error",
    "security_fact_unavailable",
    "query_authorization_required",
    "strong_auth_required_via_v2",
    "authorization_required",
    "authorization_not_found",
    "authorization_parameter_changed",
    "authorization_already_consumed",
    "authorization_principal_mismatch",
    "authorization_session_mismatch",
    "authorization_action_mismatch",
    "risk_rejected",
    "insufficient_funds",
    "limit_exceeded",
    "validation_failed",
    "verification_failed",
    "duplicate_operation",
    "tool_execution_failed",
    "service_unavailable",
    "operation_failed",
    "agent_error",
    "contract_payload_mismatch",
]

TaskStatusV2 = Literal[
    "planned",
    "running",
    "waiting_clarification",
    "waiting_confirmation",
    "waiting_verification",
    "completed",
    "failed",
    "cancelled",
    "expired",
]

StepStatusV2 = Literal[
    "pending",
    "running",
    "waiting_clarification",
    "waiting_confirmation",
    "waiting_verification",
    "completed",
    "failed",
    "skipped",
    "cancelled",
]


class StrictV2Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ requests
class LoginRequest(StrictV2Model):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class CreateSessionRequest(StrictV2Model):
    """V2 sessions are created from a verified credential, never a user_id."""


class V2ChatRequest(StrictV2Model):
    message: str = Field(min_length=1, max_length=2000)


class V2ConfirmRequest(StrictV2Model):
    text: str = Field(min_length=1, max_length=200)


class V2ReadRequest(StrictV2Model):
    tool: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)
    # 订阅查询等 yellow 只读操作需要显式确认；确认后仍走独立只读目录。
    authorize_query: bool = False


class SendChallengeRequest(StrictV2Model):
    action: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)


class VerifyChallengeRequest(StrictV2Model):
    code: str = Field(min_length=4, max_length=12)


# ---------------------------------------------------------------- responses
class LoginResponse(StrictV2Model):
    contract_version: Literal["2.0"] = CONTRACT_VERSION_V2
    session_id: str
    principal: "PrincipalView"
    credential_expires_at: datetime
    # 会话 ID 即不透明随机访问凭据；这里只返回前缀用于前端提示，不重复完整凭据。
    credential_hint: str = ""


class PrincipalView(StrictV2Model):
    principal_id: str
    user_id: int
    display_name: str
    kind: Literal["simulated_user", "legacy_demo", "system"]
    session_protocol: Literal["v1", "v2"]


class SecurityView(StrictV2Model):
    """Structured security data; supersedes guessing from Chinese messages."""

    decision_id: str | None = None
    policy_version: str
    permission_level: PermissionLevel
    allowed: bool
    reason_codes: list[str] = Field(default_factory=list)
    requires_confirmation: bool = False
    required_authentication: AuthenticationMethod = "none"
    requires_strong_auth: bool = False
    action: str | None = None
    summary: str | None = None
    threshold_cents: int | None = None
    projected_total_cents: int | None = None


class VerificationView(StrictV2Model):
    challenge_id: str
    action: str
    method: Literal["mock_otp", "confirmation"] = "mock_otp"
    status: Literal[
        "pending", "verified", "consumed", "expired", "cancelled", "failed"
    ] = "pending"
    attempts_remaining: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    created_at: datetime
    expires_at: datetime
    resend_available_at: datetime | None = None
    masked_delivery_hint: str | None = None
    delivery_channel: Literal["simulated_local_inbox"] = "simulated_local_inbox"


class RestrictionView(StrictV2Model):
    kind: Literal[
        "strong_auth_lockout", "suspicious_restriction", "tool_circuit_open"
    ]
    scope: str
    reason_code: str
    active: bool
    locked_at: datetime | None = None
    unlock_at: datetime | None = None
    retry_after_seconds: int | None = None
    detail: str | None = None


class UiInteractionV2(StrictV2Model):
    type: Literal[
        "none", "clarification", "confirmation", "verification"
    ] = "none"
    missing_fields: list[str] = Field(default_factory=list)
    confirmation_mode: Literal["none", "explicit", "dynamic_phrase"] = "none"
    confirmation_phrase: str | None = None
    expires_at: str | None = None
    challenge_id: str | None = None


class UiTaskStepV2(StrictV2Model):
    step_id: str
    title: str
    status: StepStatusV2
    permission_level: PermissionLevel | None = None
    requires_strong_auth: bool = False
    error_code: str | None = None


class UiTaskV2(StrictV2Model):
    task_id: str
    status: TaskStatusV2
    current_step_id: str | None = None
    steps: list[UiTaskStepV2]


class V2Error(StrictV2Model):
    code: V2ErrorCode
    message: str
    detail_code: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    retry_after_seconds: int | None = None
    unlock_at: datetime | None = None


# ---- V2 result payloads reuse the frozen V1 business payloads -------------
from api.schemas import UiResult  # noqa: E402  (reuse, do not duplicate)

V2Result = UiResult


class V2Envelope(StrictV2Model):
    contract_version: Literal["2.0"] = CONTRACT_VERSION_V2
    session_id: str
    status: V2Status
    message: str
    interaction: UiInteractionV2
    security: SecurityView | None = None
    verification: VerificationView | None = None
    restriction: RestrictionView | None = None
    task: UiTaskV2 | None = None
    result: V2Result | None = None
    error: V2Error | None = None


class ChallengeStatusResponse(StrictV2Model):
    contract_version: Literal["2.0"] = CONTRACT_VERSION_V2
    verification: VerificationView
    security: SecurityView | None = None
    restriction: RestrictionView | None = None


class SecurityStatusResponse(StrictV2Model):
    contract_version: Literal["2.0"] = CONTRACT_VERSION_V2
    session_id: str
    principal: PrincipalView
    policy_version: str
    permission_requirements: dict[str, "PermissionRequirement"]
    restrictions: list[RestrictionView] = Field(default_factory=list)
    pending_challenge: VerificationView | None = None
    circuit_breakers: list["BreakerView"] = Field(default_factory=list)


class PermissionRequirement(StrictV2Model):
    action: str
    permission_level: PermissionLevel
    requires_confirmation: bool
    required_authentication: AuthenticationMethod
    requires_strong_auth: bool


class BreakerView(StrictV2Model):
    tool: str
    state: Literal["closed", "open", "half_open"]
    failure_count: int = Field(ge=0)
    open_until: datetime | None = None
    last_error_code: str | None = None


class ApiProblemV2(StrictV2Model):
    contract_version: Literal["2.0"] = CONTRACT_VERSION_V2
    code: V2ErrorCode
    message: str


class HealthResponseV2(StrictV2Model):
    status: Literal["ok"] = "ok"
    backend_version: str = "0.8.1"
    contract_version: Literal["2.0"] = CONTRACT_VERSION_V2
    policy_version: str
    llm_configured: bool
    demo_login_enabled: bool = True
    mock_otp_inbox_enabled: bool = False


class CapabilitiesResponseV2(StrictV2Model):
    contract_version: Literal["2.0"] = CONTRACT_VERSION_V2
    capabilities: list["CapabilityV2"]


class CapabilityV2(StrictV2Model):
    id: Literal[
        "auth",
        "session",
        "bill",
        "card",
        "subscription",
        "transfer",
        "wealth",
        "workflow",
        "security",
        "strong_auth",
    ]
    name: str
    enabled: bool


LoginResponse.model_rebuild()
SecurityStatusResponse.model_rebuild()
CapabilitiesResponseV2.model_rebuild()
