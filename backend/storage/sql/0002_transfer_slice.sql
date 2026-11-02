-- PG02: additive transfer slice. No seed or modification of 0001.
CREATE SCHEMA pg02 AUTHORIZATION banking_migrator;
REVOKE ALL ON SCHEMA pg02 FROM PUBLIC;
GRANT USAGE ON SCHEMA pg02 TO banking_runtime;
ALTER TABLE core.users ADD UNIQUE(public_id,id);
ALTER TABLE core.cards ADD UNIQUE(owner_id,public_id);
ALTER TABLE core.cards ADD COLUMN bank_name TEXT NOT NULL DEFAULT '模拟银行';
ALTER TABLE core.cards ADD COLUMN overseas_enabled INTEGER NOT NULL DEFAULT 0 CHECK(overseas_enabled IN (0,1));
ALTER TABLE core.cards ADD COLUMN online_payment_enabled INTEGER NOT NULL DEFAULT 1 CHECK(online_payment_enabled IN (0,1));
CREATE VIEW pg02.cards WITH (security_invoker=true) AS
 SELECT c.public_id AS card_id,u.public_id AS owner_id,c.bank_name,c.masked_number AS card_number,
 a.posted_minor-a.held_minor AS balance_cents,
 CASE WHEN a.status!='active' THEN 'frozen' WHEN c.status='active' THEN 'normal' ELSE c.status END AS status,
 c.daily_limit_minor AS daily_limit_cents,c.overseas_enabled,c.online_payment_enabled,
 c.per_tx_limit_minor AS per_tx_limit_cents,c.id AS card_uuid,c.account_id
 FROM core.cards c JOIN core.users u ON u.id=c.owner_id
 JOIN core.accounts a ON a.id=c.account_id AND a.owner_id=c.owner_id;
CREATE FUNCTION pg02.debit_card() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
 SET search_path=pg_catalog AS $$
BEGIN
 IF NEW.balance_cents>OLD.balance_cents OR NEW.balance_cents<0 THEN RAISE EXCEPTION 'invalid_debit'; END IF;
 UPDATE core.accounts SET posted_minor=posted_minor-(OLD.balance_cents-NEW.balance_cents),version=version+1
 WHERE id=OLD.account_id AND owner_id=core.require_owner()
 AND status='active' AND posted_minor-held_minor>=OLD.balance_cents-NEW.balance_cents;
 IF NOT FOUND THEN RAISE EXCEPTION 'conditional_debit_failed'; END IF;
 RETURN NEW;
END; $$;
REVOKE ALL ON FUNCTION pg02.debit_card() FROM PUBLIC;
CREATE TRIGGER debit_card INSTEAD OF UPDATE ON pg02.cards FOR EACH ROW EXECUTE FUNCTION pg02.debit_card();
GRANT SELECT,UPDATE(balance_cents) ON pg02.cards TO banking_runtime;

CREATE TABLE pg02.principals (
    principal_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN ('simulated_user', 'legacy_demo', 'system')),
    display_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'disabled')),
    created_at TEXT NOT NULL,
    owner_uuid UUID NOT NULL UNIQUE REFERENCES core.users(id),
    UNIQUE(owner_uuid,principal_id),
    FOREIGN KEY(user_id,owner_uuid) REFERENCES core.users(public_id,id)
);

CREATE TABLE pg02.credentials (
    credential_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    username TEXT NOT NULL UNIQUE,
    password_salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    hash_algorithm TEXT NOT NULL,
    hash_iterations INTEGER NOT NULL CHECK(hash_iterations > 0),
    created_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0, 1)),
    owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
    FOREIGN KEY(owner_uuid,principal_id) REFERENCES pg02.principals(owner_uuid,principal_id)
);

CREATE TABLE pg02.auth_sessions (
    session_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    credential_id TEXT NOT NULL,
    protocol TEXT NOT NULL CHECK(protocol IN ('v1', 'v2')),
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    credential_expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0, 1)),
    owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
    FOREIGN KEY(owner_uuid,principal_id) REFERENCES pg02.principals(owner_uuid,principal_id)
);

CREATE TABLE pg02.security_challenges (
    challenge_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    action_digest TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    permission_level TEXT NOT NULL,
    method TEXT NOT NULL CHECK(method IN ('totp', 'confirmation')),
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
    decision_id TEXT NOT NULL,
    owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
    FOREIGN KEY(owner_uuid,principal_id) REFERENCES pg02.principals(owner_uuid,principal_id)
);

CREATE TABLE pg02.security_authorizations (
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
    business_transfer_id TEXT,
    owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
    FOREIGN KEY(owner_uuid,principal_id) REFERENCES pg02.principals(owner_uuid,principal_id)
);

CREATE TABLE pg02.security_counters (
    scope TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    counter_type TEXT NOT NULL,
    window_started_at TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0 CHECK(count >= 0),
    locked_until TEXT,
    reason_code TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (owner_uuid, scope, scope_key, counter_type, window_started_at),
    owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id)
);

CREATE TABLE pg02.breaker_states (
    tool_name TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('closed', 'open', 'half_open')),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK(failure_count >= 0),
    window_started_at TEXT NOT NULL,
    opened_at TEXT,
    open_until TEXT,
    half_open_probes INTEGER NOT NULL DEFAULT 0 CHECK(half_open_probes >= 0),
    last_error_code TEXT,
    updated_at TEXT NOT NULL,
    owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
    PRIMARY KEY(owner_uuid,tool_name)
);

CREATE TABLE pg02.security_audit_records (
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
    created_at TEXT NOT NULL,
    owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
    rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE
);

CREATE TABLE pg02.recipients (
    recipient_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    bank_name TEXT NOT NULL,
    account_number TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'disabled')),
    trusted INTEGER NOT NULL CHECK(trusted IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE(owner_uuid, bank_name, account_number),
    owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
    owner_id INTEGER NOT NULL,
    FOREIGN KEY(owner_id,owner_uuid) REFERENCES core.users(public_id,id),
    id UUID NOT NULL UNIQUE,
    UNIQUE(owner_uuid,recipient_id)
);

CREATE TABLE pg02.transfer_orders (
    transfer_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    from_card_id INTEGER NOT NULL,
    recipient_id INTEGER NOT NULL,
    amount_cents BIGINT NOT NULL CHECK(amount_cents > 0),
    remark TEXT,
    risk_level TEXT NOT NULL CHECK(risk_level IN ('low', 'medium', 'high')),
    status TEXT NOT NULL CHECK(status = 'completed'),
    balance_before_cents BIGINT NOT NULL,
    balance_after_cents BIGINT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
    owner_id INTEGER NOT NULL,
    FOREIGN KEY(owner_id,owner_uuid) REFERENCES core.users(public_id,id),
    business_date TEXT NOT NULL, principal_id TEXT NOT NULL,session_id TEXT NOT NULL,authorization_id TEXT NOT NULL,
    UNIQUE(owner_uuid,idempotency_key), UNIQUE(owner_uuid,transfer_id),
    FOREIGN KEY(owner_uuid,from_card_id) REFERENCES core.cards(owner_id,public_id),
    FOREIGN KEY(owner_uuid,recipient_id) REFERENCES pg02.recipients(owner_uuid,recipient_id),
    CHECK(balance_before_cents-amount_cents=balance_after_cents AND balance_after_cents>=0)
);

CREATE TABLE pg02.transactions (
    transaction_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    card_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    merchant TEXT NOT NULL,
    category TEXT NOT NULL,
    amount_cents BIGINT NOT NULL CHECK(amount_cents > 0),
    direction TEXT NOT NULL CHECK(direction IN ('income', 'expense')),
    owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
    owner_id INTEGER NOT NULL,
    FOREIGN KEY(owner_id,owner_uuid) REFERENCES core.users(public_id,id),
    transaction_type TEXT NOT NULL CHECK(transaction_type='transfer'), classification_source TEXT NOT NULL,
    source_order_id TEXT NOT NULL, UNIQUE(owner_uuid,source_order_id),
    FOREIGN KEY(owner_uuid,card_id) REFERENCES core.cards(owner_id,public_id),
    FOREIGN KEY(owner_uuid,source_order_id) REFERENCES pg02.transfer_orders(owner_uuid,transfer_id)
);

CREATE TABLE pg02.operation_logs (
    log_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    operation_type TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id INTEGER,
    target_display TEXT,
    before_json TEXT,
    after_json TEXT,
    result TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL,
    owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id)
);

CREATE TABLE pg02.agent_task_events (
    event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id TEXT NOT NULL,
    step_id TEXT,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'task_created', 'step_started', 'step_completed',
        'waiting_clarification', 'clarification_resolved',
        'waiting_confirmation', 'step_failed', 'step_skipped',
        'task_completed', 'task_cancelled', 'task_expired'
    )),
    status TEXT NOT NULL,
    safe_detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
    rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE
);


CREATE TABLE pg02.authenticator_factors(
 user_id INTEGER PRIMARY KEY,principal_id TEXT NOT NULL,encrypted_secret TEXT NOT NULL,
 confirmed INTEGER NOT NULL CHECK(confirmed IN (0,1)),last_used_step BIGINT NOT NULL,
 enrollment_session TEXT NOT NULL,enrollment_expires_at TEXT NOT NULL,created_at TEXT NOT NULL,
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(),
 FOREIGN KEY(user_id,owner_uuid) REFERENCES core.users(public_id,id),
 FOREIGN KEY(owner_uuid,principal_id) REFERENCES pg02.principals(owner_uuid,principal_id));
CREATE TABLE pg02.lifecycle_schema(version INTEGER PRIMARY KEY);
INSERT INTO pg02.lifecycle_schema VALUES(1);
CREATE TABLE pg02.lifecycle_identity(task_id TEXT PRIMARY KEY,owner_id INTEGER NOT NULL,principal_id TEXT NOT NULL,
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(),UNIQUE(owner_uuid,task_id),
 FOREIGN KEY(owner_id,owner_uuid) REFERENCES core.users(public_id,id),
 FOREIGN KEY(owner_uuid,principal_id) REFERENCES pg02.principals(owner_uuid,principal_id));
CREATE TABLE pg02.lifecycle_tasks(task_id TEXT PRIMARY KEY,owner_id INTEGER NOT NULL,revision INTEGER NOT NULL CHECK(revision>0),
 status TEXT NOT NULL,due_at TEXT,snapshot TEXT NOT NULL,checksum TEXT NOT NULL,lease_token TEXT,lease_until TEXT,
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(),rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
 FOREIGN KEY(owner_uuid,task_id) REFERENCES pg02.lifecycle_identity(owner_uuid,task_id),
 FOREIGN KEY(owner_id,owner_uuid) REFERENCES core.users(public_id,id));
CREATE TABLE pg02.lifecycle_confirmation_hashes(principal_id TEXT NOT NULL,phrase_hash TEXT NOT NULL,
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(),PRIMARY KEY(principal_id,phrase_hash),
 FOREIGN KEY(owner_uuid,principal_id) REFERENCES pg02.principals(owner_uuid,principal_id));
CREATE TABLE pg02.lifecycle_receipts(operation_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,owner_id INTEGER NOT NULL,
 action TEXT NOT NULL CHECK(action='transfer_money'),arguments_hash TEXT NOT NULL,business_key TEXT NOT NULL,
 result TEXT NOT NULL,committed_at TEXT NOT NULL,execution_digest TEXT NOT NULL,
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(),UNIQUE(owner_uuid,business_key),
 FOREIGN KEY(owner_uuid,task_id) REFERENCES pg02.lifecycle_identity(owner_uuid,task_id),
 FOREIGN KEY(owner_id,owner_uuid) REFERENCES core.users(public_id,id),
 FOREIGN KEY(owner_uuid,business_key) REFERENCES pg02.transfer_orders(owner_uuid,idempotency_key));
CREATE TABLE pg02.lifecycle_rollbacks(event_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,revision INTEGER NOT NULL,
 target_step TEXT NOT NULL,reason TEXT NOT NULL,affected_json TEXT NOT NULL,history_json TEXT NOT NULL,created_at TEXT NOT NULL,
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(),
 FOREIGN KEY(owner_uuid,task_id) REFERENCES pg02.lifecycle_identity(owner_uuid,task_id));
CREATE TABLE pg02.authenticator_bindings(challenge_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,step_id TEXT NOT NULL,
 preview_id TEXT NOT NULL,binding_json TEXT NOT NULL,owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id));
ALTER TABLE pg02.auth_sessions ADD UNIQUE(owner_uuid,session_id);
ALTER TABLE pg02.auth_sessions ADD FOREIGN KEY(user_id,owner_uuid) REFERENCES core.users(public_id,id);
ALTER TABLE pg02.security_challenges ADD FOREIGN KEY(user_id,owner_uuid) REFERENCES core.users(public_id,id);
ALTER TABLE pg02.security_authorizations ADD FOREIGN KEY(user_id,owner_uuid) REFERENCES core.users(public_id,id);
ALTER TABLE pg02.credentials ADD UNIQUE(owner_uuid,credential_id);
ALTER TABLE pg02.auth_sessions ADD FOREIGN KEY(owner_uuid,credential_id) REFERENCES pg02.credentials(owner_uuid,credential_id);
ALTER TABLE pg02.security_authorizations ADD UNIQUE(owner_uuid,authorization_id);
ALTER TABLE pg02.security_challenges ADD UNIQUE(owner_uuid,challenge_id);
ALTER TABLE pg02.security_authorizations ADD FOREIGN KEY(owner_uuid,session_id) REFERENCES pg02.auth_sessions(owner_uuid,session_id);
ALTER TABLE pg02.security_challenges ADD FOREIGN KEY(owner_uuid,session_id) REFERENCES pg02.auth_sessions(owner_uuid,session_id);
ALTER TABLE pg02.security_authorizations ADD FOREIGN KEY(owner_uuid,challenge_id) REFERENCES pg02.security_challenges(owner_uuid,challenge_id);
ALTER TABLE pg02.authenticator_bindings ADD FOREIGN KEY(owner_uuid,challenge_id) REFERENCES pg02.security_challenges(owner_uuid,challenge_id);
ALTER TABLE pg02.transfer_orders ADD FOREIGN KEY(owner_uuid,authorization_id) REFERENCES pg02.security_authorizations(owner_uuid,authorization_id);
ALTER TABLE pg02.transfer_orders ADD FOREIGN KEY(owner_uuid,session_id) REFERENCES pg02.auth_sessions(owner_uuid,session_id);
CREATE INDEX transfer_daily_owner ON pg02.transfer_orders(owner_uuid,business_date,status);
CREATE INDEX transfer_daily_card ON pg02.transfer_orders(owner_uuid,from_card_id,business_date,status);
CREATE INDEX tasks_due ON pg02.lifecycle_tasks(owner_uuid,status,due_at);
CREATE INDEX challenge_session ON pg02.security_challenges(owner_uuid,session_id,status);
CREATE INDEX audit_task ON pg02.security_audit_records(owner_uuid,task_id,rowid);
CREATE FUNCTION pg02.immutable() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN RAISE EXCEPTION 'immutable evidence' USING ERRCODE='42501'; END; $$;
REVOKE ALL ON FUNCTION pg02.immutable() FROM PUBLIC;
CREATE TRIGGER receipt_immutable BEFORE UPDATE OR DELETE ON pg02.lifecycle_receipts FOR EACH ROW EXECUTE FUNCTION pg02.immutable();
CREATE TRIGGER order_immutable BEFORE UPDATE OR DELETE ON pg02.transfer_orders FOR EACH ROW EXECUTE FUNCTION pg02.immutable();
CREATE TRIGGER ledger_immutable BEFORE UPDATE OR DELETE ON pg02.transactions FOR EACH ROW EXECUTE FUNCTION pg02.immutable();
CREATE TRIGGER identity_immutable BEFORE UPDATE OR DELETE ON pg02.lifecycle_identity FOR EACH ROW EXECUTE FUNCTION pg02.immutable();

ALTER TABLE pg02.principals ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.principals TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.principals TO banking_runtime;

ALTER TABLE pg02.credentials ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.credentials TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.credentials TO banking_runtime;

ALTER TABLE pg02.auth_sessions ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.auth_sessions TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.auth_sessions TO banking_runtime;

GRANT INSERT ON pg02.auth_sessions TO banking_runtime;

GRANT UPDATE ON pg02.auth_sessions TO banking_runtime;

ALTER TABLE pg02.security_challenges ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.security_challenges TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.security_challenges TO banking_runtime;

GRANT INSERT ON pg02.security_challenges TO banking_runtime;

GRANT UPDATE ON pg02.security_challenges TO banking_runtime;

ALTER TABLE pg02.security_authorizations ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.security_authorizations TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.security_authorizations TO banking_runtime;

GRANT INSERT ON pg02.security_authorizations TO banking_runtime;

GRANT UPDATE ON pg02.security_authorizations TO banking_runtime;

ALTER TABLE pg02.security_counters ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.security_counters TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.security_counters TO banking_runtime;

GRANT INSERT ON pg02.security_counters TO banking_runtime;

GRANT UPDATE ON pg02.security_counters TO banking_runtime;

ALTER TABLE pg02.breaker_states ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.breaker_states TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.breaker_states TO banking_runtime;

GRANT INSERT ON pg02.breaker_states TO banking_runtime;

GRANT UPDATE ON pg02.breaker_states TO banking_runtime;

ALTER TABLE pg02.security_audit_records ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.security_audit_records TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.security_audit_records TO banking_runtime;

GRANT INSERT ON pg02.security_audit_records TO banking_runtime;

ALTER TABLE pg02.recipients ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.recipients TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.recipients TO banking_runtime;

GRANT INSERT ON pg02.recipients TO banking_runtime;

ALTER TABLE pg02.transfer_orders ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.transfer_orders TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.transfer_orders TO banking_runtime;

GRANT INSERT ON pg02.transfer_orders TO banking_runtime;

ALTER TABLE pg02.transactions ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.transactions TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.transactions TO banking_runtime;

GRANT INSERT ON pg02.transactions TO banking_runtime;

ALTER TABLE pg02.operation_logs ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.operation_logs TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.operation_logs TO banking_runtime;

GRANT INSERT ON pg02.operation_logs TO banking_runtime;

ALTER TABLE pg02.agent_task_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.agent_task_events TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.agent_task_events TO banking_runtime;

GRANT INSERT ON pg02.agent_task_events TO banking_runtime;

ALTER TABLE pg02.authenticator_factors ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.authenticator_factors TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.authenticator_factors TO banking_runtime;

GRANT INSERT ON pg02.authenticator_factors TO banking_runtime;

GRANT UPDATE ON pg02.authenticator_factors TO banking_runtime;

ALTER TABLE pg02.lifecycle_identity ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.lifecycle_identity TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.lifecycle_identity TO banking_runtime;

GRANT INSERT ON pg02.lifecycle_identity TO banking_runtime;

ALTER TABLE pg02.lifecycle_tasks ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.lifecycle_tasks TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.lifecycle_tasks TO banking_runtime;

GRANT INSERT ON pg02.lifecycle_tasks TO banking_runtime;

GRANT UPDATE ON pg02.lifecycle_tasks TO banking_runtime;

ALTER TABLE pg02.lifecycle_confirmation_hashes ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.lifecycle_confirmation_hashes TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.lifecycle_confirmation_hashes TO banking_runtime;

GRANT INSERT ON pg02.lifecycle_confirmation_hashes TO banking_runtime;

ALTER TABLE pg02.lifecycle_receipts ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.lifecycle_receipts TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.lifecycle_receipts TO banking_runtime;

GRANT INSERT ON pg02.lifecycle_receipts TO banking_runtime;

ALTER TABLE pg02.lifecycle_rollbacks ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.lifecycle_rollbacks TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.lifecycle_rollbacks TO banking_runtime;

GRANT INSERT ON pg02.lifecycle_rollbacks TO banking_runtime;

ALTER TABLE pg02.authenticator_bindings ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.authenticator_bindings TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.authenticator_bindings TO banking_runtime;

GRANT INSERT ON pg02.authenticator_bindings TO banking_runtime;

GRANT SELECT ON pg02.lifecycle_schema TO banking_runtime;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA pg02 TO banking_runtime;

GRANT DELETE ON pg02.security_counters TO banking_runtime;

-- Only the unauthenticated login counter has no user context. Runtime cannot
-- inspect or write this table; the fixed function returns one scoped count.
CREATE TABLE pg02.login_failures(window_start BIGINT PRIMARY KEY,attempts BIGINT NOT NULL CHECK(attempts>0));
CREATE FUNCTION pg02.record_login_failure(window_value BIGINT) RETURNS BIGINT LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog AS $$
 INSERT INTO pg02.login_failures VALUES(window_value,1)
 ON CONFLICT(window_start) DO UPDATE SET attempts=pg02.login_failures.attempts+1 RETURNING attempts; $$;
REVOKE ALL ON FUNCTION pg02.record_login_failure(BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION pg02.record_login_failure(BIGINT) TO banking_runtime;


CREATE FUNCTION pg02.lookup_login(login_name TEXT) RETURNS JSONB LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog AS $$
 SELECT to_jsonb(c)||jsonb_build_object('principal_user_id',p.user_id,'principal_kind',p.kind,
 'principal_display_name',p.display_name,'principal_status',p.status)
 FROM pg02.credentials c JOIN pg02.principals p ON p.principal_id=c.principal_id AND p.owner_uuid=c.owner_uuid
 JOIN core.users u ON u.id=p.owner_uuid AND u.public_id=p.user_id
 WHERE c.username=login_name AND c.revoked=0 AND p.status='active' AND u.status='active'; $$;
CREATE FUNCTION pg02.lookup_session(sid TEXT) RETURNS JSONB LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog AS $$
 SELECT to_jsonb(s)||jsonb_build_object('display_name',p.display_name,'kind',p.kind,
 'principal_status',p.status,'credential_revoked',c.revoked,'user_status',u.status)
 FROM pg02.auth_sessions s JOIN pg02.principals p ON p.principal_id=s.principal_id AND p.owner_uuid=s.owner_uuid
 JOIN pg02.credentials c ON c.credential_id=s.credential_id AND c.owner_uuid=s.owner_uuid
 JOIN core.users u ON u.id=s.owner_uuid AND u.public_id=s.user_id
 WHERE s.session_id=sid; $$;
REVOKE ALL ON FUNCTION pg02.lookup_login(TEXT),pg02.lookup_session(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION pg02.lookup_login(TEXT),pg02.lookup_session(TEXT) TO banking_runtime;

-- Row locking needs UPDATE privilege in PG. Keep identity tables read-only to
-- runtime and expose only a fixed, owner-scoped locking operation instead.
CREATE FUNCTION pg02.lock_identity(pid TEXT,cid TEXT,sid TEXT) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE owner_value UUID;
BEGIN
 owner_value:=core.require_owner();
 PERFORM 1 FROM core.users WHERE id=owner_value FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'credential_expired' USING ERRCODE='42501'; END IF;
 PERFORM 1 FROM pg02.principals WHERE principal_id=pid AND owner_uuid=owner_value FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'credential_expired' USING ERRCODE='42501'; END IF;
 PERFORM 1 FROM pg02.credentials WHERE credential_id=cid AND principal_id=pid AND owner_uuid=owner_value FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'credential_expired' USING ERRCODE='42501'; END IF;
 IF sid IS NOT NULL THEN
  PERFORM 1 FROM pg02.auth_sessions WHERE session_id=sid AND principal_id=pid AND credential_id=cid AND owner_uuid=owner_value FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'credential_expired' USING ERRCODE='42501'; END IF;
 END IF;
END; $$;
REVOKE ALL ON FUNCTION pg02.lock_identity(TEXT,TEXT,TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION pg02.lock_identity(TEXT,TEXT,TEXT) TO banking_runtime;

CREATE FUNCTION pg02.lock_recipient(rid INTEGER) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog AS $$
BEGIN
 PERFORM 1 FROM pg02.recipients WHERE recipient_id=rid AND owner_uuid=core.require_owner() FOR UPDATE;
END; $$;
REVOKE ALL ON FUNCTION pg02.lock_recipient(INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION pg02.lock_recipient(INTEGER) TO banking_runtime;
