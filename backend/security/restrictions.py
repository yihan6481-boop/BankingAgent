"""Security restrictions: verification failures, suspicious requests, breakers.

Three limitation classes are implemented separately, each with a clear scope
and reason code:

1. **verification failures** – a challenge dies after its configured attempt
   limit; reaching the per-user failure threshold within the configured window
   locks strong authentication for the configured cooldown;
2. **suspicious requests** – confirmed permission bypass attempts, injected
   internal fields and repeated cross-user resource access are counted per
   trusted user / authenticated source.  Ordinary business refusals (insufficient
   balance, unmet conditions, user cancellation, parameter clarification) are
   never counted, and unauthenticated sources are rate limited separately so a
   forged ``user_id`` cannot lock another user;
3. **tool faults** – consecutive real infrastructure/tool failures trip a
   closed/open/half_open breaker per tool.  Recovery probes use no-side-effect
   reads or an explicit manual reset; a write tool is never probed with real
   money movement.

All counters and lockouts are persisted, so a restart does not clear them, and
the clock is injectable for deterministic tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .config import DEFAULT_SECURITY_CONFIG, SecurityConfig
from .db import read_connection, write_transaction
from .schemas import (
    BreakerStatus,
    Principal,
    RestrictionStatus,
)

# 普通业务拒绝：绝不计入可疑请求或工具故障
BENIGN_ERROR_CODES = {
    "insufficient_balance",
    "insufficient_funds",
    "card_unavailable",
    "card_not_found",
    "card_lost",
    "recipient_disabled",
    "recipient_untrusted",
    "recipient_not_found",
    "recipient_ambiguous",
    "recipient_mismatch",
    "below_minimum",
    "risk_mismatch",
    "product_inactive",
    "product_not_found",
    "holding_redeemed",
    "holding_not_found",
    "amount_exceeds_principal",
    "daily_limit_exceeded",
    "invalid_arguments",
    "invalid_amount",
    "invalid_card_id",
    "invalid_recipient_id",
    "invalid_remark",
    "invalid_time",
    "missing_public_arguments",
    "condition_not_met",
    "user_cancelled",
    "cancelled",
    "confirmation_required",
    "write_tool_not_allowed",
}

# 确定的违规：权限绕过、内部字段注入、反复跨用户资源访问
SUSPICIOUS_ERROR_CODES = {
    "permission_bypass_attempt",
    "protected_field_injection",
    "cross_user_resource_access",
    "authorization_forgery_attempt",
    "challenge_binding_violation",
    "policy_override_attempt",
}

# 真实基础设施/工具故障
INFRASTRUCTURE_ERROR_CODES = {
    "tool_execution_error",
    "tool_execution_failed",
    "service_unavailable",
    "security_fact_unavailable",
    "database_error",
    "storage_error",
    "circuit_probe_failed",
}

STRONG_AUTH_SCOPE = "strong_auth"
SUSPICIOUS_SCOPE = "suspicious"
UNAUTHENTICATED_SCOPE = "unauthenticated"


def _window_start(now: datetime, window_seconds: int) -> str:
    epoch = int(now.timestamp())
    bucket = epoch - (epoch % max(int(window_seconds), 1))
    return datetime.fromtimestamp(bucket, tz=now.tzinfo).isoformat()


class SecurityRestrictions:
    """Persistent counters, lockouts and the tool circuit breaker."""

    def __init__(self, config: SecurityConfig | None = None, now_provider=None):
        from .identity import _utcnow

        self.config = config or DEFAULT_SECURITY_CONFIG
        self.now_provider = now_provider or _utcnow

    def _now(self) -> datetime:
        value = self.now_provider()
        if not isinstance(value, datetime):
            raise TypeError("now_provider 必须返回 datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now_provider 必须返回带时区的 datetime")
        return value.replace(microsecond=0)

    @staticmethod
    def _subject(principal: Principal | None, session_id: str | None = None) -> str:
        if principal is not None and getattr(principal, "kind", None) != "legacy_demo":
            return f"user:{int(principal.user_id)}"
        if principal is not None and principal.kind == "legacy_demo":
            return "legacy_demo"
        return f"anon:{(session_id or 'unknown')[:16]}"

    # ------------------------------------------------------------ counters
    def _record(
        self, scope: str, subject: str, counter_type: str, *,
        connection=None, window_seconds: int | None = None,
    ) -> int:
        now = self._now()
        window = window_seconds or self.config.strong_auth_failure_window_seconds
        bucket = _window_start(now, window)
        owns = connection is None
        if owns:
            from .db import get_connection  # local import to avoid cycles

            connection = get_connection()
            connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT count FROM security_counters WHERE scope = ? AND "
                "scope_key = ? AND counter_type = ? AND window_started_at = ?",
                (scope, subject, counter_type, bucket),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO security_counters (
                        scope, scope_key, counter_type, window_started_at,
                        count, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?)
                    """,
                    (scope, subject, counter_type, bucket, now.isoformat()),
                )
                count = 1
            else:
                count = int(row["count"]) + 1
                connection.execute(
                    "UPDATE security_counters SET count = ?, updated_at = ? "
                    "WHERE scope = ? AND scope_key = ? AND counter_type = ? "
                    "AND window_started_at = ?",
                    (
                        count, now.isoformat(), scope, subject, counter_type,
                        bucket,
                    ),
                )
            if owns:
                connection.commit()
            return count
        except Exception:
            if owns:
                connection.rollback()
            raise
        finally:
            if owns:
                connection.close()

    def _current(self, scope: str, subject: str, counter_type: str, window_seconds: int):
        now = self._now()
        bucket = _window_start(now, window_seconds)
        with read_connection() as connection:
            row = connection.execute(
                "SELECT count FROM security_counters WHERE scope = ? AND "
                "scope_key = ? AND counter_type = ? AND window_started_at = ?",
                (scope, subject, counter_type, bucket),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def _lock_until(self, scope: str, subject: str):
        now = self._now()
        with read_connection() as connection:
            rows = connection.execute(
                "SELECT locked_until FROM security_counters WHERE scope = ? AND "
                "scope_key = ? AND locked_until IS NOT NULL "
                "ORDER BY locked_until DESC LIMIT 1",
                (scope, subject),
            ).fetchall()
        for row in rows:
            try:
                until = datetime.fromisoformat(row["locked_until"])
            except (TypeError, ValueError):
                continue
            if until > now:
                return until
        return None

    def _set_lock(self, scope: str, subject: str, until: datetime, reason_code: str,
                  connection=None):
        now = self._now()
        bucket = _window_start(now, self.config.strong_auth_failure_window_seconds)
        owns = connection is None
        if owns:
            from .db import get_connection

            connection = get_connection()
            connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                INSERT INTO security_counters (
                    scope, scope_key, counter_type, window_started_at, count,
                    locked_until, reason_code, updated_at
                ) VALUES (?, ?, 'lock', ?, 0, ?, ?, ?)
                ON CONFLICT(scope, scope_key, counter_type, window_started_at)
                DO UPDATE SET locked_until = excluded.locked_until,
                              reason_code = excluded.reason_code,
                              updated_at = excluded.updated_at
                """,
                (
                    scope, subject, bucket, until.isoformat(), reason_code,
                    now.isoformat(),
                ),
            )
            if owns:
                connection.commit()
        except Exception:
            if owns:
                connection.rollback()
            raise
        finally:
            if owns:
                connection.close()

    # ---------------------------------------------- strong auth failures
    def record_strong_auth_failure(self, principal: Principal | None, *,
                                   session_id: str | None = None,
                                   connection=None) -> RestrictionStatus | None:
        subject = self._subject(principal, session_id)
        count = self._record(
            STRONG_AUTH_SCOPE, subject, "verification_failure",
            connection=connection,
            window_seconds=self.config.strong_auth_failure_window_seconds,
        )
        if count >= self.config.strong_auth_failure_threshold:
            until = self._now() + timedelta(
                seconds=self.config.strong_auth_lockout_seconds
            )
            self._set_lock(
                STRONG_AUTH_SCOPE, subject, until, "verification_failure_threshold",
                connection=connection,
            )
            return RestrictionStatus(
                kind="strong_auth_lockout",
                scope=subject,
                reason_code="verification_failure_threshold",
                active=True,
                locked_at=self._now(),
                unlock_at=until,
                retry_after_seconds=self.config.strong_auth_lockout_seconds,
                detail="强验证失败次数达到阈值",
            )
        return None

    def clear_strong_auth_failures(self, principal: Principal | None, *,
                                   session_id: str | None = None,
                                   connection=None):
        """A *successful* verification resets the failure window."""
        subject = self._subject(principal, session_id)
        owns = connection is None
        if owns:
            from .db import get_connection

            connection = get_connection()
            connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "DELETE FROM security_counters WHERE scope = ? AND scope_key = ? "
                "AND counter_type = 'verification_failure'",
                (STRONG_AUTH_SCOPE, subject),
            )
            if owns:
                connection.commit()
        finally:
            if owns:
                connection.close()

    def strong_auth_lock(self, principal: Principal | None, *,
                         session_id: str | None = None) -> RestrictionStatus | None:
        subject = self._subject(principal, session_id)
        until = self._lock_until(STRONG_AUTH_SCOPE, subject)
        if until is None:
            return None
        return self._restriction("strong_auth_lockout", subject, until,
                                 "verification_failure_threshold")

    def strong_auth_lock_for_user(self, user_id: int) -> RestrictionStatus | None:
        subject = f"user:{int(user_id)}"
        until = self._lock_until(STRONG_AUTH_SCOPE, subject)
        if until is None:
            return None
        return self._restriction("strong_auth_lockout", subject, until,
                                 "verification_failure_threshold")

    def _restriction(self, kind, subject, until, reason_code) -> RestrictionStatus:
        now = self._now()
        remaining = int((until - now).total_seconds())
        return RestrictionStatus(
            kind=kind,
            scope=subject,
            reason_code=reason_code,
            active=remaining > 0,
            locked_at=now,
            unlock_at=until,
            retry_after_seconds=max(remaining, 0),
        )

    # ------------------------------------------------- suspicious requests
    def is_suspicious_error(self, error_code: str | None) -> bool:
        return (error_code or "") in SUSPICIOUS_ERROR_CODES

    def is_benign_error(self, error_code: str | None) -> bool:
        code = error_code or ""
        if code in SUSPICIOUS_ERROR_CODES:
            return False
        if code in BENIGN_ERROR_CODES:
            return True
        return code.startswith("invalid_") or "mismatch" in code

    def record_suspicious(
        self,
        principal: Principal | None,
        error_code: str,
        *,
        session_id: str | None = None,
        source: str | None = None,
    ) -> RestrictionStatus | None:
        """Count a *confirmed* violation.  Benign refusals return ``None``."""
        if not self.is_suspicious_error(error_code):
            return None
        subject = self._subject(principal, session_id) if principal else (
            f"source:{source}" if source else self._subject(None, session_id)
        )
        count = self._record(
            SUSPICIOUS_SCOPE, subject, "violation",
            window_seconds=self.config.suspicious_window_seconds,
        )
        if count >= self.config.suspicious_threshold:
            until = self._now() + timedelta(
                seconds=self.config.suspicious_lockout_seconds
            )
            self._set_lock(SUSPICIOUS_SCOPE, subject, until, error_code)
            return RestrictionStatus(
                kind="suspicious_restriction",
                scope=subject,
                reason_code=error_code,
                active=True,
                locked_at=self._now(),
                unlock_at=until,
                retry_after_seconds=self.config.suspicious_lockout_seconds,
                detail="可疑请求次数达到阈值",
            )
        return None

    def suspicious_restriction(
        self, principal: Principal | None, *, session_id: str | None = None
    ) -> RestrictionStatus | None:
        subject = self._subject(principal, session_id)
        until = self._lock_until(SUSPICIOUS_SCOPE, subject)
        if until is None:
            return None
        return self._restriction("suspicious_restriction", subject, until,
                                 "suspicious_threshold_reached")

    # ------------------------------------------ unauthenticated compatibility
    def record_unauthenticated_attempt(self, source: str):
        from .login_limit import reserve
        wait = reserve(source, '')
        if not wait:
            return None
        return self._restriction('suspicious_restriction', 'source:login',
                                 self._now() + timedelta(seconds=wait),
                                 'unauthenticated_rate_limit')

    def unauthenticated_restriction(self, source: str):
        from .login_limit import reserve
        wait = reserve(source, '', status_only=True)
        if not wait:
            return None
        return self._restriction('suspicious_restriction', 'source:login',
                                 self._now() + timedelta(seconds=wait),
                                 'unauthenticated_rate_limit')

    # ----------------------------------------------------- circuit breaker
    def breaker_status(self, tool_name: str) -> BreakerStatus:
        from .tool_guard import breaker_operation, dependency_for
        value = breaker_operation(dependency_for(tool_name), 'status')
        return BreakerStatus(tool=tool_name, state=value['state'],
                             failure_count=value['failure_count'])

    def breaker_allows(self, tool_name: str) -> tuple[bool, BreakerStatus]:
        # Observational compatibility API. guarded_tool_call reserves atomically.
        status = self.breaker_status(tool_name)
        return status.state == 'closed', status

    def record_tool_success(self, tool_name: str):
        from .tool_guard import breaker_operation, dependency_for
        breaker_operation(dependency_for(tool_name), 'success')

    def record_tool_failure(self, tool_name: str, error_code: str | None) -> BreakerStatus:
        from .tool_guard import breaker_operation, dependency_for, _INFRA_CODES
        if error_code in _INFRA_CODES:
            breaker_operation(dependency_for(tool_name), 'failure', error_code)
        return self.breaker_status(tool_name)

    def claim_half_open_probe(self, tool_name: str) -> bool:
        from .tool_guard import breaker_operation, dependency_for
        return breaker_operation(dependency_for(tool_name), 'read')['allowed']

    def manual_reset(self, tool_name: str) -> BreakerStatus:
        # Explicit server operator API, never registered as an LLM tool.
        self.record_tool_success(tool_name)
        return self.breaker_status(tool_name)


__all__ = [
    "BENIGN_ERROR_CODES",
    "INFRASTRUCTURE_ERROR_CODES",
    "SUSPICIOUS_ERROR_CODES",
    "SecurityRestrictions",
]
