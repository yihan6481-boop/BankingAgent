"""Simulated server-side identity: principals, credentials and session bindings.

This is a **local simulation** of an identity provider for a virtual bank.  It
implements salted password hashing and an expiring random access credential,
and it deliberately does not claim to be production IAM.

Key guarantees relied upon by the rest of V0.7.0:

* the identity always comes from a stored credential, never from the request
  body (a ``user_id`` in a payload is rejected as an injected internal field);
* a credential is bound to a session, and a session belongs to exactly one
  principal, so sessions cannot be shared across users;
* the legacy V1 demo principal (``owner_id = 0``) is isolated from every
  simulated user's business data.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from .config import (
    DEMO_USER_ID,
    LEGACY_DEMO_PRINCIPAL_ID,
    LEGACY_DEMO_USER_ID,
    SecurityConfig,
)
from .db import ensure_security_schema, read_connection, write_transaction
from .schemas import Credential, Principal, SessionBinding

PASSWORD_ALGORITHM = "pbkdf2_hmac_sha256"


class IdentityError(Exception):
    """Authentication/authorization failure with a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str, iterations: int, salt: bytes | None = None):
    """Salted PBKDF2-HMAC-SHA256.  Returns (salt_hex, hash_hex, algorithm, iters)."""
    if not isinstance(password, str) or not password:
        raise ValueError("password 必须是非空文本")
    salt_bytes = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt_bytes, iterations
    )
    return salt_bytes.hex(), digest.hex(), PASSWORD_ALGORITHM, iterations


def verify_password(password: str, salt_hex: str, expected_hash: str, iterations: int) -> bool:
    try:
        if isinstance(password, bool) or not isinstance(password, str):
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), iterations
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected_hash)


class IdentityService:
    """Credential storage, login and session binding."""

    def __init__(self, config: SecurityConfig | None = None, now_provider=None):
        from .config import DEFAULT_SECURITY_CONFIG

        self.config = config or DEFAULT_SECURITY_CONFIG
        self.now_provider = now_provider or _utcnow
        ensure_security_schema(self._password_hasher, self._now().isoformat())
        self._legacy_session_id: str | None = None

    # ------------------------------------------------------------------ time
    def _now(self) -> datetime:
        value = self.now_provider()
        if not isinstance(value, datetime):
            raise TypeError("now_provider 必须返回 datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now_provider 必须返回带时区的 datetime")
        return value.replace(microsecond=0)

    def _password_hasher(self, password):
        return hash_password(password, self.config.password_hash_iterations)

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _principal_from_row(row) -> Principal:
        return Principal(
            principal_id=row["principal_id"],
            user_id=row["user_id"],
            display_name=row["display_name"],
            kind=row["kind"],
            authenticated_at=datetime.fromisoformat(row["created_at"]),
            credential_id=None,
        )

    @staticmethod
    def _session_from_row(row) -> SessionBinding:
        return SessionBinding(
            session_id=row["session_id"],
            principal_id=row["principal_id"],
            user_id=row["user_id"],
            credential_id=row["credential_id"],
            protocol=row["protocol"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
            credential_expires_at=datetime.fromisoformat(
                row["credential_expires_at"]
            ),
            revoked=bool(row["revoked"]),
        )

    # ---------------------------------------------------------------- login
    def login(self, username, password) -> tuple[Principal, SessionBinding, str]:
        """Verify credentials and open a new bound session.

        Returns ``(principal, session_binding, access_token)``.  The raw access
        credential is returned only to the caller of this method; it is never
        written to the database, logs, audit records or API responses after
        login and never reaches the LLM context.
        """
        if not isinstance(username, str) or not username.strip():
            raise IdentityError("invalid_credentials", "用户名或密码不正确")
        if not isinstance(password, str) or not password:
            raise IdentityError("invalid_credentials", "用户名或密码不正确")
        normalized_username = username.strip()

        with read_connection() as connection:
            row = connection.execute(
                """
                SELECT c.*, p.user_id AS principal_user_id, p.kind AS principal_kind,
                       p.display_name AS principal_display_name, p.status AS principal_status
                FROM credentials c
                JOIN principals p ON p.principal_id = c.principal_id
                WHERE c.username = ?
                """,
                (normalized_username,),
            ).fetchone()
        if row is None or row["revoked"] or row["principal_status"] != "active":
            raise IdentityError("invalid_credentials", "用户名或密码不正确")
        if not verify_password(
            password, row["password_salt"], row["password_hash"],
            row["hash_iterations"],
        ):
            raise IdentityError("invalid_credentials", "用户名或密码不正确")

        now = self._now()
        expires_at = now + timedelta(seconds=self.config.credential_ttl_seconds)
        session_id = f"s2_{secrets.token_hex(self.config.session_token_bytes)}"
        credential_id = row["credential_id"]
        principal = Principal(
            principal_id=row["principal_id"],
            user_id=row["principal_user_id"],
            display_name=row["principal_display_name"],
            kind=row["principal_kind"],
            authenticated_at=now,
            credential_id=credential_id,
        )
        binding = SessionBinding(
            session_id=session_id,
            principal_id=principal.principal_id,
            user_id=principal.user_id,
            credential_id=credential_id,
            protocol="v2",
            created_at=now,
            last_seen_at=now,
            credential_expires_at=expires_at,
        )
        with write_transaction() as connection:
            connection.execute(
                """
                INSERT INTO auth_sessions (
                    session_id, principal_id, user_id, credential_id, protocol,
                    created_at, last_seen_at, credential_expires_at, revoked
                ) VALUES (?, ?, ?, ?, 'v2', ?, ?, ?, 0)
                """,
                (
                    session_id,
                    principal.principal_id,
                    principal.user_id,
                    credential_id,
                    now.isoformat(),
                    now.isoformat(),
                    expires_at.isoformat(),
                ),
            )
        return principal, binding, "unused"

    # -------------------------------------------------------------- session
    def get_session(self, session_id) -> SessionBinding | None:
        if not isinstance(session_id, str) or not session_id:
            return None
        with read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM auth_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return self._session_from_row(row) if row is not None else None

    def get_principal(self, principal_id) -> Principal | None:
        if not isinstance(principal_id, str) or not principal_id:
            return None
        with read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM principals WHERE principal_id = ?", (principal_id,)
            ).fetchone()
        return self._principal_from_row(row) if row is not None else None

    def resolve_principal(self, session_id) -> Principal | None:
        """Server-side principal for a session; ``None`` if unusable/expired."""
        binding = self.get_session(session_id)
        if binding is None or binding.revoked:
            return None
        if binding.credential_expires_at <= self._now():
            return None
        if binding.protocol == 'v2':
            with read_connection() as connection:
                active = connection.execute(
                    "SELECT 1 FROM credentials c JOIN principals p ON p.principal_id=c.principal_id "
                    "WHERE c.credential_id=? AND c.principal_id=? AND c.revoked=0 AND p.status='active'",
                    (binding.credential_id, binding.principal_id),
                ).fetchone()
            if active is None:
                return None
        principal = self.get_principal(binding.principal_id)
        if principal is None:
            return None
        return principal.model_copy(update={"credential_id": binding.credential_id})

    def touch_session(self, session_id):
        with write_transaction() as connection:
            connection.execute(
                "UPDATE auth_sessions SET last_seen_at = ? WHERE session_id = ?",
                (self._now().isoformat(), session_id),
            )

    def revoke_session(self, session_id):
        with write_transaction() as connection:
            connection.execute(
                "UPDATE auth_sessions SET revoked = 1 WHERE session_id = ?",
                (session_id,),
            )
            # Durable revocation also retires unconsumed proofs. Receipts and
            # lifecycle snapshots are retained for authenticated recovery.
            connection.execute(
                "UPDATE security_challenges SET status='cancelled', cancelled_at=? "
                "WHERE session_id=? AND status IN ('pending','verified')",
                (self._now().isoformat(), session_id),
            )
            connection.execute(
                "UPDATE security_authorizations SET consumed=1, consumed_at=? "
                "WHERE session_id=? AND consumed=0",
                (self._now().isoformat(), session_id),
            )

    def assert_session_owner(self, session_id, principal: Principal):
        """A session must belong to the principal that presents it."""
        binding = self.get_session(session_id)
        if binding is None:
            raise IdentityError("session_not_found", "会话不存在或后端已重启")
        if binding.revoked:
            raise IdentityError("session_revoked", "会话已失效，请重新登录")
        if binding.principal_id != principal.principal_id:
            raise IdentityError(
                "session_principal_mismatch", "该会话不属于当前登录身份"
            )
        if binding.credential_expires_at <= self._now():
            raise IdentityError("credential_expired", "访问凭据已过期，请重新登录")
        return binding

    # --------------------------------------------------- legacy V1 boundary
    def legacy_demo_principal(self) -> Principal:
        """Isolated single-user V1 demo principal (``owner_id = 0``).

        V1 has no login entry point.  Its requests may only see the legacy demo
        owner scope, never a simulated user's cards, sessions or challenges.
        """
        if not self.config.legacy_demo_enabled:
            raise IdentityError("legacy_demo_disabled", "旧版演示身份已关闭")
        principal = self.get_principal(LEGACY_DEMO_PRINCIPAL_ID)
        if principal is None:
            raise IdentityError("legacy_principal_missing", "旧版演示身份不可用")
        return principal

    def create_legacy_session(self) -> SessionBinding:
        if not self.config.legacy_demo_enabled:
            raise IdentityError("legacy_demo_disabled", "旧版演示身份已关闭")
        now = self._now()
        session_id = f"s1_{secrets.token_hex(self.config.session_token_bytes)}"
        binding = SessionBinding(
            session_id=session_id,
            principal_id=LEGACY_DEMO_PRINCIPAL_ID,
            user_id=LEGACY_DEMO_USER_ID,
            credential_id="legacy-demo-credential",
            protocol="v1",
            created_at=now,
            last_seen_at=now,
            credential_expires_at=now + timedelta(
                seconds=self.config.credential_ttl_seconds
            ),
        )
        with write_transaction() as connection:
            connection.execute(
                """
                INSERT INTO auth_sessions (
                    session_id, principal_id, user_id, credential_id, protocol,
                    created_at, last_seen_at, credential_expires_at, revoked
                ) VALUES (?, ?, ?, ?, 'v1', ?, ?, ?, 0)
                """,
                (
                    session_id,
                    binding.principal_id,
                    binding.user_id,
                    binding.credential_id,
                    now.isoformat(),
                    now.isoformat(),
                    binding.credential_expires_at.isoformat(),
                ),
            )
        return binding


def assert_object_owner(row, expected_user_id: int, object_name: str):
    """Ownership check helper for a fetched business row.

    Public product catalogues are the only documented exception; everything
    else must carry ``owner_id``/``user_id`` equal to the acting principal.
    """
    if row is None:
        raise IdentityError(f"{object_name}_not_found", f"{object_name} 不存在")
    actual = None
    try:
        keys = row.keys()
    except AttributeError:
        keys = []
    for candidate in ("owner_id", "user_id"):
        if candidate in keys:
            actual = row[candidate]
            break
    if actual is None:
        raise IdentityError(
            f"{object_name}_ownership_unknown", "资源缺少归属信息，拒绝访问"
        )
    if int(actual) != int(expected_user_id):
        raise IdentityError(f"{object_name}_not_found", f"{object_name} 不存在")
    return row


__all__ = [
    "IdentityError",
    "IdentityService",
    "assert_object_owner",
    "hash_password",
    "verify_password",
    "PASSWORD_ALGORITHM",
]
