"""V0.7.0 security configuration.

Every threshold, window and TTL required by the V0.7.0 permission policy,
strong-authentication闭环 and 熔断器 lives here so that behaviour is
configuration driven instead of being hard-coded across modules.

Values can be overridden with environment variables (``BA07_*``).  The module
exposes a frozen :class:`SecurityConfig` so tests can build a deterministic
configuration with ``dataclasses.replace`` without mutating process globals.

This is a *simulated* banking environment: the defaults below are demo
thresholds, not production risk parameters.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

POLICY_VERSION = "0.7.0"

# Business day boundary for the cumulative transfer threshold.  A banking day
# must never be derived by slicing a UTC timestamp: 00:00-08:00 Shanghai time
# still belongs to the previous UTC day.
BUSINESS_TIMEZONE_NAME = "Asia/Shanghai"
BUSINESS_TIMEZONE = ZoneInfo(BUSINESS_TIMEZONE_NAME)

DEMO_USER_ID = 1
LEGACY_DEMO_USER_ID = 0
LEGACY_DEMO_PRINCIPAL_ID = "legacy-demo"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().casefold() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SecurityConfig:
    """Immutable V0.7.0 security policy configuration."""

    policy_version: str = POLICY_VERSION
    authentication_mode: str = "demo"

    def __post_init__(self):
        if self.authentication_mode not in {"demo", "secure"}:
            raise ValueError("BA091_AUTH_MODE must be demo or secure")

    # --- cumulative daily transfer threshold (integer cents) -------------
    # 本日用户累计转账金额（所有归属卡片合计）+ 本次金额 <= 该值 => yellow
    daily_transfer_confirm_cents: int = 100_000  # 1000.00 CNY

    # --- card daily limit escalation ------------------------------------
    # 每日限额调整默认 yellow；新版限额高于该值 => red
    daily_limit_escalation_cents: int = 5_000_000  # 50000.00 CNY

    # --- 赛题例外：订阅查询/费用统计默认 yellow -------------------------
    # 只读查询并不自动等于 green，必须通过策略显式配置。
    subscription_query_requires_confirmation: bool = True

    # --- OTP / strong authentication ------------------------------------
    otp_length: int = 6
    otp_max_attempts: int = 3
    otp_resend_cooldown_seconds: int = 30
    challenge_ttl_seconds: int = 180
    # 强验证失败累计阈值（按可信用户/认证来源计数）
    strong_auth_failure_window_seconds: int = 600
    strong_auth_failure_threshold: int = 5
    strong_auth_lockout_seconds: int = 600

    # --- suspicious request limiting ------------------------------------
    suspicious_window_seconds: int = 600
    suspicious_threshold: int = 5
    suspicious_lockout_seconds: int = 600

    # --- tool / service circuit breaker ---------------------------------
    breaker_failure_threshold: int = 3
    breaker_window_seconds: int = 300
    breaker_open_seconds: int = 120
    breaker_half_open_max_probes: int = 1

    # --- session / credential -------------------------------------------
    credential_ttl_seconds: int = 43_200  # 12h
    pending_action_ttl_seconds: int = 120
    password_hash_iterations: int = 210_000
    # 16 random bytes -> 32 hex chars; preserves the existing s1_/s2_ shape.
    session_token_bytes: int = 16
    otp_secret_bytes: int = 32

    # --- local demo boundaries ------------------------------------------
    demo_login_enabled: bool = True
    legacy_demo_enabled: bool = True
    # HTTP 收件箱默认关闭；仅设置 BA07_MOCK_OTP_INBOX_TOKEN 时启用。
    mock_otp_inbox_enabled: bool = False
    cors_allow_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    )

    # --- configurable overrides used by tests ---------------------------
    overrides: dict = field(default_factory=dict)

    def with_overrides(self, **values) -> "SecurityConfig":
        from dataclasses import replace

        unknown = set(values) - set(self.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown security config field(s): {sorted(unknown)}")
        return replace(self, **values)


def load_security_config() -> SecurityConfig:
    """Build the default configuration, honouring ``BA07_*`` env overrides."""
    origins = os.environ.get("BA07_CORS_ALLOW_ORIGINS", "").strip()
    mock_inbox_token = os.environ.get("BA07_MOCK_OTP_INBOX_TOKEN", "").strip()
    config = SecurityConfig(
        authentication_mode=os.environ.get("BA091_AUTH_MODE", "demo").strip(),
        daily_transfer_confirm_cents=_env_int(
            "BA07_DAILY_TRANSFER_CONFIRM_CENTS", 100_000
        ),
        daily_limit_escalation_cents=_env_int(
            "BA07_DAILY_LIMIT_ESCALATION_CENTS", 5_000_000
        ),
        subscription_query_requires_confirmation=_env_bool(
            "BA07_SUBSCRIPTION_QUERY_REQUIRES_CONFIRMATION", True
        ),
        otp_max_attempts=_env_int("BA07_OTP_MAX_ATTEMPTS", 3),
        strong_auth_failure_window_seconds=_env_int(
            "BA07_STRONG_AUTH_FAILURE_WINDOW_SECONDS", 600
        ),
        strong_auth_failure_threshold=_env_int(
            "BA07_STRONG_AUTH_FAILURE_THRESHOLD", 5
        ),
        strong_auth_lockout_seconds=_env_int(
            "BA07_STRONG_AUTH_LOCKOUT_SECONDS", 600
        ),
        suspicious_window_seconds=_env_int("BA07_SUSPICIOUS_WINDOW_SECONDS", 600),
        suspicious_threshold=_env_int("BA07_SUSPICIOUS_THRESHOLD", 5),
        suspicious_lockout_seconds=_env_int(
            "BA07_SUSPICIOUS_LOCKOUT_SECONDS", 600
        ),
        breaker_failure_threshold=_env_int("BA07_BREAKER_FAILURE_THRESHOLD", 3),
        breaker_window_seconds=_env_int("BA07_BREAKER_WINDOW_SECONDS", 300),
        breaker_open_seconds=_env_int("BA07_BREAKER_OPEN_SECONDS", 120),
        credential_ttl_seconds=_env_int("BA07_CREDENTIAL_TTL_SECONDS", 43_200),
        mock_otp_inbox_enabled=bool(mock_inbox_token),
        cors_allow_origins=(
            tuple(item.strip() for item in origins.split(",") if item.strip())
            if origins
            else SecurityConfig.cors_allow_origins
        ),
    )
    return config


DEFAULT_SECURITY_CONFIG = load_security_config()

# Demo accounts.  These are credentials for a *local simulation* only and are
# never presented as production IAM.
DEMO_CREDENTIALS = (
    {
        "principal_id": "demo-user-1",
        "user_id": DEMO_USER_ID,
        "username": "demo",
        "password": "Demo#2026",
        "display_name": "演示用户（默认）",
    },
    {
        "principal_id": "demo-user-2",
        "user_id": 2,
        "username": "demo2",
        "password": "Demo2#2026",
        "display_name": "演示用户（隔离测试）",
    },
)
