"""Durable security tables and the demo-data ownership migration.

V0.7.0 needs persistent identity, session bindings, OTP challenges, security
counters, breaker state and the desensitized audit chain.  The tables below are
created idempotently on top of the existing V0.6.4 schema; no existing table or
row is dropped, and the ownership migration moves already-seeded demo data to
the default demo user instead of resetting the database.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from database import get_connection

from .config import (
    DEMO_CREDENTIALS,
    DEMO_USER_ID,
    LEGACY_DEMO_PRINCIPAL_ID,
    LEGACY_DEMO_USER_ID,
)

SECURITY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS principals (
    principal_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN ('simulated_user', 'legacy_demo', 'system')),
    display_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'disabled')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    credential_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    username TEXT NOT NULL UNIQUE,
    password_salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    hash_algorithm TEXT NOT NULL,
    hash_iterations INTEGER NOT NULL CHECK(hash_iterations > 0),
    created_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0, 1)),
    FOREIGN KEY(principal_id) REFERENCES principals(principal_id)
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    session_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    credential_id TEXT NOT NULL,
    protocol TEXT NOT NULL CHECK(protocol IN ('v1', 'v2')),
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    credential_expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0, 1)),
    FOREIGN KEY(principal_id) REFERENCES principals(principal_id)
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_principal
    ON auth_sessions(principal_id, created_at);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
    ON auth_sessions(user_id, created_at);

CREATE TABLE IF NOT EXISTS security_challenges (
    challenge_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    action_digest TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    permission_level TEXT NOT NULL,
    method TEXT NOT NULL CHECK(method IN ('mock_otp', 'confirmation')),
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'verified', 'consumed', 'expired', 'cancelled', 'failed'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
    code_derivation TEXT,
    code_hint TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    resend_available_at TEXT,
    verified_at TEXT,
    consumed_at TEXT,
    cancelled_at TEXT,
    decision_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_security_challenges_session
    ON security_challenges(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_security_challenges_user
    ON security_challenges(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_security_challenges_status
    ON security_challenges(status, expires_at);

CREATE TABLE IF NOT EXISTS security_authorizations (
    authorization_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    action TEXT NOT NULL,
    action_digest TEXT NOT NULL,
    permission_level TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    challenge_id TEXT,
    strong_auth_method TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0 CHECK(consumed IN (0, 1)),
    consumed_at TEXT,
    business_transfer_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_security_authorizations_session
    ON security_authorizations(session_id, issued_at);
CREATE INDEX IF NOT EXISTS idx_security_authorizations_user
    ON security_authorizations(user_id, issued_at);

CREATE TABLE IF NOT EXISTS security_counters (
    scope TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    counter_type TEXT NOT NULL,
    window_started_at TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0 CHECK(count >= 0),
    locked_until TEXT,
    reason_code TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, scope_key, counter_type, window_started_at)
);
CREATE INDEX IF NOT EXISTS idx_security_counters_lookup
    ON security_counters(scope, scope_key, counter_type, updated_at);

CREATE TABLE IF NOT EXISTS breaker_states (
    tool_name TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK(state IN ('closed', 'open', 'half_open')),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK(failure_count >= 0),
    window_started_at TEXT NOT NULL,
    opened_at TEXT,
    open_until TEXT,
    half_open_probes INTEGER NOT NULL DEFAULT 0 CHECK(half_open_probes >= 0),
    last_error_code TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS security_audit_records (
    record_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence >= 0),
    stage TEXT NOT NULL,
    outcome TEXT NOT NULL,
    principal_id TEXT,
    user_id INTEGER,
    session_id TEXT,
    task_id TEXT,
    step_id TEXT,
    action TEXT,
    policy_version TEXT,
    permission_level TEXT,
    decision_id TEXT,
    challenge_id TEXT,
    authorization_id TEXT,
    authentication_method TEXT,
    reason_codes_json TEXT NOT NULL,
    error_code TEXT,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_security_audit_request
    ON security_audit_records(request_id, sequence);
CREATE INDEX IF NOT EXISTS idx_security_audit_session
    ON security_audit_records(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_security_audit_decision
    ON security_audit_records(decision_id);

CREATE TABLE IF NOT EXISTS security_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _table_columns(connection: sqlite3.Connection, table: str) -> set:
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {row[1] for row in rows}


def _add_column_if_missing(connection, table, column, definition):
    if column in _table_columns(connection, table):
        return False
    try:
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )
        return True
    except sqlite3.Error:
        return False


def migrate_ownership_columns(connection):
    """Attach the demo owner to pre-existing business tables without data loss.

    ``user_id`` defaults to the default demo user so already seeded data stays
    visible to the default demo principal; the legacy V1 demo principal uses a
    separate ``owner_id`` marker (0) and can therefore never read it.
    """
    created = []
    for table in (
        "cards",
        "transactions",
        "subscriptions",
        "recipients",
        "transfer_orders",
        "wealth_orders",
        "wealth_holdings",
        "user_risk_profiles",
    ):
        if _add_column_if_missing(
            connection, table, "owner_id", f"INTEGER NOT NULL DEFAULT {DEMO_USER_ID}"
        ):
            created.append(table)
    # 上海银行业务日必须显式保存，不能在查询时截取 UTC 时间戳。
    _add_column_if_missing(connection, "transfer_orders", "business_date", "TEXT")
    _add_column_if_missing(connection, "wealth_orders", "business_date", "TEXT")
    _add_column_if_missing(connection, "transfer_orders", "principal_id", "TEXT")
    _add_column_if_missing(connection, "transfer_orders", "session_id", "TEXT")
    _add_column_if_missing(connection, "transfer_orders", "authorization_id", "TEXT")
    _add_column_if_missing(connection, "wealth_orders", "principal_id", "TEXT")
    _add_column_if_missing(connection, "wealth_orders", "session_id", "TEXT")
    _add_column_if_missing(connection, "wealth_orders", "authorization_id", "TEXT")
    connection.execute(
        """
        UPDATE transfer_orders
        SET business_date = substr(completed_at, 1, 10)
        WHERE business_date IS NULL
        """
    )
    connection.execute(
        """
        UPDATE wealth_orders
        SET business_date = substr(created_at, 1, 10)
        WHERE business_date IS NULL
        """
    )
    for table in (
        "cards",
        "transactions",
        "subscriptions",
        "recipients",
        "user_risk_profiles",
    ):
        connection.execute(
            f"UPDATE {table} SET owner_id = ? WHERE owner_id IS NULL",
            (DEMO_USER_ID,),
        )
    # 默认演示用户（owner 1）的订阅数据：旧 seed 未包含订阅表，这里补齐，
    # 使赛题例外（订阅查询/费用统计为 yellow）在默认演示库中可达。
    connection.execute(
        """
        INSERT OR IGNORE INTO subscriptions (
            subscription_id, name, amount_cents, cycle, next_charge_date,
            card_id, status, owner_id
        ) VALUES (9101, '腾讯体育', 2500, 'monthly', '2026-12-01', 1,
                  'active', 1)
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO subscriptions (
            subscription_id, name, amount_cents, cycle, next_charge_date,
            card_id, status, owner_id
        ) VALUES (9102, '云盘会员', 1200, 'monthly', '2026-12-05', 2,
                  'active', 1)
        """
    )
    # 隔离的第二演示用户数据（V0.7.0 归属隔离验收需要真实存在的第二用户资源）
    connection.execute(
        """
        INSERT OR IGNORE INTO cards (
            card_id, bank_name, card_number, balance_cents, status,
            daily_limit_cents, overseas_enabled, online_payment_enabled, owner_id
        ) VALUES (9001, '隔离测试银行', '6217000000009001', 500000, 'normal',
                  2000000, 0, 1, 2)
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO recipients (
            recipient_id, name, bank_name, account_number, status, trusted,
            created_at, owner_id
        ) VALUES (9001, '隔离收款人', '隔离测试银行', '6217000000009002',
                  'active', 1, '2026-01-01T00:00:00+00:00', 2)
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO user_risk_profiles (
            profile_id, user_id, risk_level, updated_at, owner_id
        ) VALUES (9001, 2, 'R2', '2026-01-01T00:00:00+00:00', 2)
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO subscriptions (
            subscription_id, name, amount_cents, cycle, next_charge_date,
            card_id, status, owner_id
        ) VALUES (9001, '隔离订阅', 1980, 'monthly', '2026-12-01', 9001,
                  'active', 2)
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO wealth_holdings (
            holding_id, user_id, product_id, card_id, principal_cents, status,
            purchased_at, updated_at
        ) VALUES (9001, 2, 1, 9001, 100000, 'active', '2026-01-01T00:00:00+00:00',
                  '2026-01-01T00:00:00+00:00')
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO transactions (
            transaction_id, card_id, date, merchant, category, amount_cents,
            direction, owner_id
        ) VALUES (9001, 9001, '2026-09-10', '隔离测试商户', '其他', 12300,
                  'expense', 2)
        """
    )
    # 旧版 V1 无登录 Demo：只能看到隔离的 legacy demo owner scope（owner_id = 0）。
    connection.execute(
        """
        INSERT OR IGNORE INTO cards (
            card_id, bank_name, card_number, balance_cents, status,
            daily_limit_cents, overseas_enabled, online_payment_enabled, owner_id
        ) VALUES (9000, '本机演示银行', '6217000000009000', 300000, 'normal',
                  1000000, 0, 1, 0)
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO recipients (
            recipient_id, name, bank_name, account_number, status, trusted,
            created_at, owner_id
        ) VALUES (9000, '本机演示收款人', '本机演示银行', '6217000000009010',
                  'active', 1, '2026-01-01T00:00:00+00:00', 0)
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO subscriptions (
            subscription_id, name, amount_cents, cycle, next_charge_date,
            card_id, status, owner_id
        ) VALUES (9000, '本机演示订阅', 2500, 'monthly', '2026-12-01', 9000,
                  'active', 0)
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO user_risk_profiles (
            profile_id, user_id, risk_level, updated_at, owner_id
        ) VALUES (9000, 0, 'R2', '2026-01-01T00:00:00+00:00', 0)
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO wealth_holdings (
            holding_id, user_id, product_id, card_id, principal_cents, status,
            purchased_at, updated_at
        ) VALUES (9000, 0, 1, 9000, 50000, 'active', '2026-01-01T00:00:00+00:00',
                  '2026-01-01T00:00:00+00:00')
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_transfer_orders_business_day "
        "ON transfer_orders(owner_id, business_date, status)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_cards_owner ON cards(owner_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_subscriptions_owner "
        "ON subscriptions(owner_id, status)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_recipients_owner "
        "ON recipients(owner_id, status)"
    )
    return created


def seed_principals(connection, now_iso: str, password_hasher):
    """Create the simulated principals and credentials if they are absent."""
    rows = [
        ("legacy-demo", LEGACY_DEMO_USER_ID, "legacy_demo", "本机 V1 单用户 Demo"),
        ("system", 9001, "system", "系统安全组件"),
    ]
    for principal_id, user_id, kind, display_name in rows:
        connection.execute(
            """
            INSERT OR IGNORE INTO principals (
                principal_id, user_id, kind, display_name, status, created_at
            ) VALUES (?, ?, ?, ?, 'active', ?)
            """,
            (principal_id, user_id, kind, display_name, now_iso),
        )
    for demo in DEMO_CREDENTIALS:
        connection.execute(
            """
            INSERT OR IGNORE INTO principals (
                principal_id, user_id, kind, display_name, status, created_at
            ) VALUES (?, ?, 'simulated_user', ?, 'active', ?)
            """,
            (
                demo["principal_id"],
                demo["user_id"],
                demo["display_name"],
                now_iso,
            ),
        )
        existing = connection.execute(
            "SELECT 1 FROM credentials WHERE username = ?", (demo["username"],)
        ).fetchone()
        if existing is not None:
            continue
        salt, digest, algorithm, iterations = password_hasher(demo["password"])
        connection.execute(
            """
            INSERT INTO credentials (
                credential_id, principal_id, username, password_salt,
                password_hash, hash_algorithm, hash_iterations, created_at, revoked
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                f"cred-{demo['principal_id']}",
                demo["principal_id"],
                demo["username"],
                salt,
                digest,
                algorithm,
                iterations,
                now_iso,
            ),
        )
    # 第二测试用户的隔离业务数据由 migrate_ownership_columns() 统一补齐，
    # 避免两处重复插入产生时间戳不一致。


def ensure_security_schema(password_hasher, now_iso: str):
    """Create/migrate all V0.7.0 security tables; safe to call repeatedly."""
    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.executescript(SECURITY_SCHEMA_SQL)
        from .totp import migrate as migrate_authenticator
        migrate_authenticator(connection)
        created = migrate_ownership_columns(connection)
        seed_principals(connection, now_iso, password_hasher)
        connection.commit()
        return created
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@contextmanager
def write_transaction(connection=None, busy_timeout_ms: int = 15_000):
    """Run a block inside ``BEGIN IMMEDIATE`` with busy/timeout handling.

    ``BEGIN IMMEDIATE`` takes the SQLite write lock up front so concurrent
    sessions cannot both read a stale cumulative total and then both write.
    """
    own_connection = connection is None
    active = connection or get_connection()
    try:
        if own_connection:
            active.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        active.execute("BEGIN IMMEDIATE")
        yield active
        active.commit()
    except Exception:
        try:
            active.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        if own_connection:
            active.close()


@contextmanager
def read_connection():
    connection = get_connection()
    try:
        yield connection
        connection.rollback()
    finally:
        connection.close()
