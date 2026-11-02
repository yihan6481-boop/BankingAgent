-- Additive customer service and advisory risk evidence; no financial authority.
CREATE SCHEMA assistance AUTHORIZATION banking_migrator;
REVOKE ALL ON SCHEMA assistance FROM PUBLIC;
GRANT USAGE ON SCHEMA assistance TO banking_runtime;
CREATE TABLE assistance.seat_sessions (
 token_hash TEXT PRIMARY KEY, seat TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
 heartbeat TIMESTAMPTZ NOT NULL DEFAULT now(), online BOOLEAN NOT NULL DEFAULT false
);
CREATE TABLE assistance.login_attempts (key TEXT PRIMARY KEY, failures INTEGER NOT NULL, started_at TIMESTAMPTZ NOT NULL);
CREATE TABLE assistance.cases (
 id UUID PRIMARY KEY, owner_uuid UUID NOT NULL REFERENCES core.users(id),
 session_id TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('waiting','connected','ended')),
 seat TEXT, task_id TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), ended_at TIMESTAMPTZ,
 CHECK((state='connected' AND seat IS NOT NULL) OR state!='connected')
);
CREATE UNIQUE INDEX one_active_case ON assistance.cases(session_id) WHERE state!='ended';
CREATE TABLE assistance.messages (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, case_id UUID NOT NULL REFERENCES assistance.cases(id),
 owner_uuid UUID NOT NULL REFERENCES core.users(id), sender TEXT NOT NULL CHECK(sender IN ('customer','seat','system')),
 client_id TEXT NOT NULL, text TEXT NOT NULL CHECK(length(text)<=2000), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(case_id,sender,client_id)
);
CREATE TABLE assistance.audit (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, case_id UUID NOT NULL REFERENCES assistance.cases(id),
 owner_uuid UUID NOT NULL REFERENCES core.users(id), seat TEXT NOT NULL, tool TEXT NOT NULL,
 task_id TEXT, outcome TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE assistance.risk_reviews (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 task_id TEXT NOT NULL, revision INTEGER NOT NULL, version TEXT NOT NULL, evidence JSONB NOT NULL,
 choice TEXT CHECK(choice IN ('continue','modify','dismiss','support')), chosen_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(owner_uuid,task_id,revision,version)
);
CREATE TABLE assistance.semantic_clues (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 session_id TEXT NOT NULL, turn_id TEXT NOT NULL, target_reference TEXT, clues JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(owner_uuid,session_id,turn_id)
);
GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA assistance TO banking_runtime;
GRANT DELETE ON assistance.login_attempts TO banking_runtime;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA assistance TO banking_runtime;
-- Customer paths always bind the authenticated owner. Seat paths additionally
-- carry an authenticated, unexpired opaque seat token, never a request seat ID.
CREATE FUNCTION assistance.is_seat() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT EXISTS(SELECT 1 FROM assistance.seat_sessions WHERE token_hash=current_setting('assistance.token',true) AND expires_at>now());
$$;
REVOKE ALL ON FUNCTION assistance.is_seat() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION assistance.is_seat() TO banking_runtime;
ALTER TABLE assistance.cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE assistance.messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE assistance.audit ENABLE ROW LEVEL SECURITY;
CREATE POLICY service_scope ON assistance.cases TO banking_runtime USING(owner_uuid::text=current_setting('banking.owner_id',true) OR assistance.is_seat()) WITH CHECK(owner_uuid::text=current_setting('banking.owner_id',true) OR assistance.is_seat());
CREATE POLICY service_scope ON assistance.messages TO banking_runtime USING(owner_uuid::text=current_setting('banking.owner_id',true) OR assistance.is_seat()) WITH CHECK(owner_uuid::text=current_setting('banking.owner_id',true) OR assistance.is_seat());
CREATE POLICY service_scope ON assistance.audit TO banking_runtime USING(owner_uuid::text=current_setting('banking.owner_id',true) OR assistance.is_seat()) WITH CHECK(owner_uuid::text=current_setting('banking.owner_id',true) OR assistance.is_seat());
ALTER TABLE assistance.risk_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE assistance.semantic_clues ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON assistance.risk_reviews TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
CREATE POLICY owner_scope ON assistance.semantic_clues TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
