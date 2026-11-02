"""Simulated strong authentication: challenge store and MockOTPProvider.

Scope of this version (stated honestly): this implements a **simulated strong
authentication闭环** for a virtual bank.  It does not deploy real SMS, face or
voice verification, and it is not production MFA.

Design decisions that the tests rely on:

* the OTP is a secure-random 6-digit code (configurable length/TTL/attempts);
* the code value is **never stored**.  Only a per-challenge random selector is
  persisted together with a per-challenge server secret; the verification value
  is ``HMAC(server_secret, selector)``.  A leaked database therefore does not
  let an attacker brute-force six digits;
* the challenge is bound to principal, session, action, action digest (which
  covers amount, account, recipient, product and target step), policy version
  and expiry, so changing any of them invalidates the old confirmation;
* a consumed, cancelled, expired or failed challenge can never be used again,
  and re-sending never resets the failure counter or revives an old code;
* the code never appears in API responses, ``/state``, LLM context, business
  logs, fixtures or error payloads.  Delivery is a separate simulated channel
  (``security/mock_inbox.py`` and ``python -m security.otp_inbox``).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from .config import DEFAULT_SECURITY_CONFIG, SecurityConfig
from .db import read_connection, write_transaction
from .schemas import ChallengePublicView, Principal

MOCK_OTP_METHOD = "mock_otp"
CHALLENGE_STATUSES = {
    "pending",
    "verified",
    "consumed",
    "expired",
    "cancelled",
    "failed",
}


class StrongAuthError(Exception):
    def __init__(self, code: str, message: str, view: ChallengePublicView | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.view = view


def _server_auth_secret(config: SecurityConfig) -> bytes:
    """Per-installation secret used to derive verification values.

    Resolution order:

    1. ``BA07_AUTH_SECRET`` environment value (operator controlled);
    2. the persisted ``security_settings.auth_secret`` row, generated once.

    The secret is never written to any response, log, fixture or audit record,
    and the code itself is never persisted — only the random per-challenge
    selector is stored, so a leaked database cannot brute-force six digits.
    """
    import os

    raw = os.environ.get("BA07_AUTH_SECRET", "").strip()
    if raw:
        return raw.encode("utf-8")
    return _persisted_secret(config)


def _persisted_secret(config: SecurityConfig) -> bytes:
    global _PROCESS_SECRET
    if _PROCESS_SECRET is not None:
        return _PROCESS_SECRET
    from .db import read_connection, write_transaction

    try:
        with read_connection() as connection:
            row = connection.execute(
                "SELECT setting_value FROM security_settings "
                "WHERE setting_key = 'auth_secret'"
            ).fetchone()
        if row is not None and row["setting_value"]:
            _PROCESS_SECRET = bytes.fromhex(row["setting_value"])
            return _PROCESS_SECRET
        value = secrets.token_bytes(config.otp_secret_bytes).hex()
        with write_transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO security_settings "
                "(setting_key, setting_value, updated_at) VALUES "
                "('auth_secret', ?, ?)",
                (value, _now_iso()),
            )
        _PROCESS_SECRET = bytes.fromhex(value)
        return _PROCESS_SECRET
    except Exception:
        _PROCESS_SECRET = secrets.token_bytes(config.otp_secret_bytes)
        return _PROCESS_SECRET


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_PROCESS_SECRET: bytes | None = None


class StrongAuthProvider:
    """Replaceable provider interface (SMS / biometrics can implement this)."""

    method: str = MOCK_OTP_METHOD

    def generate_code(self, length: int) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def delivery_hint(self, principal: Principal) -> str:  # pragma: no cover
        raise NotImplementedError


class MockOTPProvider(StrongAuthProvider):
    """Local simulated OTP provider.

    The code is generated with ``secrets`` and handed to the isolated mock
    inbox, never to the API response.
    """

    method = MOCK_OTP_METHOD

    def __init__(self, inbox=None, code_provider=None):
        self._inbox = inbox
        self._code_provider = code_provider

    def generate_code(self, length: int) -> str:
        if self._code_provider is not None:
            value = str(self._code_provider(length)).strip()
            if len(value) != length or not value.isdigit():
                raise ValueError("注入的 OTP provider 必须返回固定长度数字")
            return value
        return "".join(str(secrets.randbelow(10)) for _ in range(length))

    def delivery_hint(self, principal: Principal) -> str:
        return f"模拟验证码通道（{principal.display_name}）"

    def deliver(self, principal, session_id, challenge_id, code, expires_at, action):
        if self._inbox is not None:
            self._inbox.record(
                principal=principal,
                session_id=session_id,
                challenge_id=challenge_id,
                code=code,
                expires_at=expires_at,
                action=action,
            )


class StrongAuthService:
    """Challenge lifecycle: create, deliver, verify, resend, cancel."""

    def __init__(self, config: SecurityConfig | None = None, provider=None,
                 now_provider=None, inbox=None):
        from .identity import _utcnow

        self.config = config or DEFAULT_SECURITY_CONFIG
        self.provider = provider or MockOTPProvider(inbox=inbox)
        self.now_provider = now_provider or _utcnow
        self.inbox = inbox

    # ------------------------------------------------------------------ time
    def _now(self) -> datetime:
        value = self.now_provider()
        if not isinstance(value, datetime):
            raise TypeError("now_provider 必须返回 datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now_provider 必须返回带时区的 datetime")
        return value.replace(microsecond=0)

    # ------------------------------------------------------------ derivation
    def _verification_value(self, selector_hex: str) -> str:
        secret = _server_auth_secret(self.config)
        digest = hmac.new(
            secret, bytes.fromhex(selector_hex), hashlib.sha256
        ).hexdigest()
        length = self.config.otp_length
        modulus = 10 ** length
        value = int(digest, 16) % modulus
        return str(value).zfill(length)

    # -------------------------------------------------------------- create
    def create_challenge(
        self,
        principal: Principal,
        session_id: str,
        action: str,
        action_digest: str,
        decision_id: str,
        policy_version: str,
        permission_level: str,
    ) -> tuple[ChallengePublicView, str]:
        """Create a pending challenge.

        Returns ``(public_view, raw_code)``.  The raw code is returned only so
        the isolated simulated delivery channel can hand it to the operator.
        It must never be placed in an API response or model context.
        """
        if principal is None:
            raise StrongAuthError("principal_required", "缺少服务器端身份")
        now = self._now()
        challenge_id = f"ch_{secrets.token_hex(12)}"
        selector_hex = secrets.token_bytes(8).hex()
        code = self._verification_value(selector_hex)
        expires_at = now + timedelta(seconds=self.config.challenge_ttl_seconds)
        resend_at = now + timedelta(
            seconds=self.config.otp_resend_cooldown_seconds
        )
        with write_transaction() as connection:
            connection.execute(
                """
                INSERT INTO security_challenges (
                    challenge_id, session_id, principal_id, user_id, action,
                    action_digest, policy_version, permission_level, method,
                    status, attempt_count, max_attempts, code_derivation,
                    code_hint, created_at, expires_at, resend_available_at,
                    decision_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?,
                          ?, ?, ?, ?)
                """,
                (
                    challenge_id,
                    session_id,
                    principal.principal_id,
                    int(principal.user_id),
                    action,
                    action_digest,
                    policy_version,
                    permission_level,
                    MOCK_OTP_METHOD,
                    self.config.otp_max_attempts,
                    selector_hex,
                    f"selector:{selector_hex[:4]}****",
                    now.isoformat(),
                    expires_at.isoformat(),
                    resend_at.isoformat(),
                    decision_id,
                ),
            )
        view = ChallengePublicView(
            challenge_id=challenge_id,
            action=action,
            session_id=session_id,
            permission_level=permission_level,
            method=MOCK_OTP_METHOD,
            status="pending",
            attempts_remaining=self.config.otp_max_attempts,
            max_attempts=self.config.otp_max_attempts,
            created_at=now,
            expires_at=expires_at,
            resend_available_at=resend_at,
            masked_delivery_hint=self.provider.delivery_hint(principal),
            reason_codes=["simulated_strong_auth"],
        )
        self.provider.deliver(
            principal, session_id, challenge_id, code, expires_at, action
        )
        return view, code

    # ----------------------------------------------------------------- read
    def _row(self, challenge_id):
        with read_connection() as connection:
            return connection.execute(
                "SELECT * FROM security_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()

    def load_view(self, challenge_id) -> ChallengePublicView | None:
        row = self._row(challenge_id)
        if row is None:
            return None
        return self._view_from_row(row)

    def _view_from_row(self, row) -> ChallengePublicView:
        now = self._now()
        status = row["status"]
        expires_at = datetime.fromisoformat(row["expires_at"])
        unlock_at = self._strong_auth_lock_until(row["user_id"])
        if status == "pending" and expires_at <= now:
            status = "expired"
        attempts_remaining = max(
            int(row["max_attempts"]) - int(row["attempt_count"]), 0
        )
        return ChallengePublicView(
            challenge_id=row["challenge_id"],
            action=row["action"],
            session_id=row["session_id"],
            permission_level=row["permission_level"],
            method=row["method"],
            status=status,
            attempts_remaining=attempts_remaining,
            max_attempts=int(row["max_attempts"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=expires_at,
            resend_available_at=(
                datetime.fromisoformat(row["resend_available_at"])
                if row["resend_available_at"]
                else None
            ),
            masked_delivery_hint=None,
            unlock_at=unlock_at,
            reason_codes=["simulated_strong_auth"],
        )

    # --------------------------------------------------------------- verify
    def verify(
        self,
        principal: Principal,
        session_id: str,
        challenge_id: str,
        code: str,
        *,
        action_digest: str | None = None,
    ) -> ChallengePublicView:
        """Verify one OTP submission and atomically mark the challenge verified.

        Failures increment the persistent per-user failure counter; reaching the
        threshold locks strong authentication for the configured window.  The
        per-challenge attempt limit invalidates the challenge.
        """
        from .restrictions import SecurityRestrictions

        if principal is None:
            raise StrongAuthError("principal_required", "缺少服务器端身份")
        if isinstance(code, bool) or not isinstance(code, str):
            raise StrongAuthError("invalid_code", "验证码格式不正确")
        normalized = code.strip()
        if not normalized.isdigit() or len(normalized) != self.config.otp_length:
            raise StrongAuthError("invalid_code", "验证码格式不正确")

        restrictions = SecurityRestrictions(self.config, now_provider=self.now_provider)
        lock = restrictions.strong_auth_lock(principal)
        if lock is not None and lock.active:
            raise StrongAuthError(
                "strong_auth_locked", "强验证已暂时锁定，请稍后重试"
            )

        now = self._now()
        # 失败计数、挑战状态失效与尝试次数必须在**提交之后**再抛错，
        # 否则 raise 会让写事务回滚，限制机制将形同失效。
        pending_error: StrongAuthError | None = None
        result_view: ChallengePublicView | None = None
        with write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM security_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
            if row is None:
                pending_error = StrongAuthError("challenge_not_found", "验证挑战不存在")
            elif row["principal_id"] != principal.principal_id:
                # 跨用户使用挑战：不区分"不存在"，避免探测其他用户挑战
                pending_error = StrongAuthError("challenge_not_found", "验证挑战不存在")
            elif row["session_id"] != session_id:
                pending_error = StrongAuthError(
                    "challenge_session_mismatch", "该验证挑战不属于当前会话"
                )
            elif action_digest is not None and row["action_digest"] != action_digest:
                self._invalidate(connection, row, "parameter_changed", now)
                pending_error = StrongAuthError(
                    "challenge_parameter_changed",
                    "操作参数已变化，原验证挑战失效，请重新预览并确认",
                )
            elif row["status"] != "pending":
                if row["status"] == "expired":
                    pending_error = StrongAuthError(
                        "challenge_expired", "验证挑战已过期"
                    )
                elif row["status"] in {"consumed", "verified"}:
                    pending_error = StrongAuthError(
                        "challenge_already_used", "该验证挑战已被使用"
                    )
                elif row["status"] == "cancelled":
                    pending_error = StrongAuthError(
                        "challenge_cancelled", "验证挑战已取消"
                    )
                else:
                    pending_error = StrongAuthError(
                        "challenge_failed", "验证挑战已失效"
                    )
            else:
                expires_at = datetime.fromisoformat(row["expires_at"])
                if expires_at <= now:
                    connection.execute(
                        "UPDATE security_challenges SET status = 'expired' "
                        "WHERE challenge_id = ?",
                        (challenge_id,),
                    )
                    pending_error = StrongAuthError(
                        "challenge_expired", "验证挑战已过期"
                    )
                else:
                    expected = self._verification_value(row["code_derivation"])
                    if not hmac.compare_digest(expected, normalized):
                        attempts = int(row["attempt_count"]) + 1
                        max_attempts = int(row["max_attempts"])
                        new_status = (
                            "failed" if attempts >= max_attempts else "pending"
                        )
                        connection.execute(
                            "UPDATE security_challenges SET attempt_count = ?, "
                            "status = ? WHERE challenge_id = ?",
                            (attempts, new_status, challenge_id),
                        )
                        restrictions.record_strong_auth_failure(
                            principal, connection=connection
                        )
                        result_view = ChallengePublicView(
                            challenge_id=challenge_id,
                            action=row["action"],
                            session_id=session_id,
                            permission_level=row["permission_level"],
                            status=new_status,
                            attempts_remaining=max(max_attempts - attempts, 0),
                            max_attempts=max_attempts,
                            created_at=datetime.fromisoformat(row["created_at"]),
                            expires_at=expires_at,
                            reason_codes=["otp_mismatch"],
                        )
                        if new_status == "failed":
                            pending_error = StrongAuthError(
                                "challenge_attempts_exhausted",
                                "验证码错误次数过多，该验证挑战已失效，请重新发起",
                                result_view,
                            )
                        else:
                            pending_error = StrongAuthError(
                                "invalid_code",
                                f"验证码不正确，剩余尝试次数 "
                                f"{max(max_attempts - attempts, 0)}",
                                result_view,
                            )
                    else:
                        connection.execute(
                            "UPDATE security_challenges SET status = 'verified', "
                            "verified_at = ? WHERE challenge_id = ?",
                            (now.isoformat(), challenge_id),
                        )
                        restrictions.clear_strong_auth_failures(
                            principal, connection=connection
                        )
                        fresh = connection.execute(
                            "SELECT * FROM security_challenges WHERE challenge_id = ?",
                            (challenge_id,),
                        ).fetchone()
                        result_view = self._view_from_row(fresh)

        if pending_error is not None:
            raise pending_error
        assert result_view is not None
        return result_view

    def _invalidate(self, connection, row, reason, now):
        connection.execute(
            "UPDATE security_challenges SET status = 'cancelled', "
            "cancelled_at = ? WHERE challenge_id = ?",
            (now.isoformat(), row["challenge_id"]),
        )

    # --------------------------------------------------------------- resend
    def resend(self, principal: Principal, session_id: str, challenge_id: str
               ) -> tuple[ChallengePublicView, str]:
        """Issue a new code for the same action without resetting failures."""
        now = self._now()
        with write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM security_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
            if row is None or row["principal_id"] != principal.principal_id:
                raise StrongAuthError("challenge_not_found", "验证挑战不存在")
            if row["session_id"] != session_id:
                raise StrongAuthError(
                    "challenge_session_mismatch", "该验证挑战不属于当前会话"
                )
            if row["status"] != "pending":
                raise StrongAuthError(
                    "challenge_not_pending", "该验证挑战已结束，不能重新发送验证码"
                )
            if datetime.fromisoformat(row["expires_at"]) <= now:
                connection.execute(
                    "UPDATE security_challenges SET status = 'expired' "
                    "WHERE challenge_id = ?",
                    (challenge_id,),
                )
                raise StrongAuthError("challenge_expired", "验证挑战已过期")
            resend_at = (
                datetime.fromisoformat(row["resend_available_at"])
                if row["resend_available_at"]
                else now
            )
            if resend_at > now:
                raise StrongAuthError(
                    "resend_too_soon", "重新发送验证码过于频繁，请稍后重试"
                )
            # 老码失效：换 selector 即换码，但失败计数保持不变。
            new_selector = secrets.token_bytes(8).hex()
            code = self._verification_value(new_selector)
            next_resend = now + timedelta(
                seconds=self.config.otp_resend_cooldown_seconds
            )
            connection.execute(
                "UPDATE security_challenges SET code_derivation = ?, "
                "resend_available_at = ? WHERE challenge_id = ?",
                (new_selector, next_resend.isoformat(), challenge_id),
            )
            fresh = connection.execute(
                "SELECT * FROM security_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
        view = self._view_from_row(fresh)
        self.provider.deliver(
            principal,
            session_id,
            challenge_id,
            code,
            view.expires_at,
            view.action,
        )
        return view, code

    # --------------------------------------------------------------- cancel
    def cancel(self, principal: Principal, session_id: str, challenge_id: str):
        now = self._now()
        with write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM security_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
            if row is None or row["principal_id"] != principal.principal_id:
                raise StrongAuthError("challenge_not_found", "验证挑战不存在")
            if row["session_id"] != session_id:
                raise StrongAuthError(
                    "challenge_session_mismatch", "该验证挑战不属于当前会话"
                )
            if row["status"] in {"consumed", "cancelled", "expired", "failed"}:
                return self._view_from_row(row)
            connection.execute(
                "UPDATE security_challenges SET status = 'cancelled', "
                "cancelled_at = ? WHERE challenge_id = ?",
                (now.isoformat(), challenge_id),
            )
            fresh = connection.execute(
                "SELECT * FROM security_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
        return self._view_from_row(fresh)

    # ------------------------------------------------------------ consume
    def consume_verified_challenge(
        self, connection, principal: Principal, session_id: str, challenge_id: str,
        action: str, action_digest: str, now: datetime,
    ):
        """Atomically consume a verified challenge inside the caller's write tx.

        Returns the ``verified_at`` timestamp.  Raises when the challenge is not
        in the ``verified`` state, does not belong to the principal/session, is
        bound to a different action digest, or has already been consumed.
        """
        row = connection.execute(
            "SELECT * FROM security_challenges WHERE challenge_id = ?",
            (challenge_id,),
        ).fetchone()
        if row is None or row["principal_id"] != principal.principal_id:
            raise StrongAuthError("challenge_not_found", "验证挑战不存在")
        if row["session_id"] != session_id:
            raise StrongAuthError(
                "challenge_session_mismatch", "该验证挑战不属于当前会话"
            )
        if row["action"] != action:
            raise StrongAuthError("challenge_action_mismatch", "验证挑战绑定的操作不一致")
        if row["action_digest"] != action_digest:
            raise StrongAuthError(
                "challenge_parameter_changed", "操作参数已变化，原验证挑战失效"
            )
        if row["status"] != "verified":
            if row["status"] == "consumed":
                raise StrongAuthError("challenge_already_used", "该验证挑战已被使用")
            raise StrongAuthError("challenge_not_verified", "验证挑战尚未通过校验")
        if datetime.fromisoformat(row["expires_at"]) <= now:
            connection.execute(
                "UPDATE security_challenges SET status = 'expired' "
                "WHERE challenge_id = ?",
                (challenge_id,),
            )
            raise StrongAuthError("challenge_expired", "验证挑战已过期")
        connection.execute(
            "UPDATE security_challenges SET status = 'consumed', consumed_at = ? "
            "WHERE challenge_id = ? AND status = 'verified'",
            (now.isoformat(), challenge_id),
        )
        return datetime.fromisoformat(row["verified_at"]) if row["verified_at"] else now

    # ------------------------------------------------------------ lock view
    def _strong_auth_lock_until(self, user_id):
        from .restrictions import SecurityRestrictions

        try:
            lock = SecurityRestrictions(
                self.config, now_provider=self.now_provider
            ).strong_auth_lock_for_user(int(user_id))
        except Exception:
            return None
        return lock.unlock_at if lock is not None and lock.active else None


__all__ = [
    "CHALLENGE_STATUSES",
    "MOCK_OTP_METHOD",
    "MockOTPProvider",
    "StrongAuthError",
    "StrongAuthProvider",
    "StrongAuthService",
]
