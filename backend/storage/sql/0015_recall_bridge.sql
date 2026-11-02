-- R4 metadata only. Hindsight remains authoritative for documents/facts/indexes.
CREATE SCHEMA recall AUTHORIZATION banking_migrator;
REVOKE ALL ON SCHEMA recall FROM PUBLIC;
GRANT USAGE ON SCHEMA recall TO banking_runtime;
CREATE TABLE recall.spaces (
 owner_uuid UUID PRIMARY KEY DEFAULT core.require_owner() REFERENCES core.users(id),
 owner_id INTEGER NOT NULL, principal_id TEXT NOT NULL, bank_ref TEXT NOT NULL UNIQUE,
 epoch BIGINT NOT NULL DEFAULT 1, auto_enabled BOOLEAN NOT NULL DEFAULT false,
 profile_enabled BOOLEAN NOT NULL DEFAULT false, config_version BIGINT NOT NULL DEFAULT 1,
 last_auto_submitted TIMESTAMPTZ,
 availability_failures INTEGER NOT NULL DEFAULT 0, circuit_until TIMESTAMPTZ,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE recall.sources (
 id TEXT PRIMARY KEY, owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES recall.spaces(owner_uuid),
 origin_ref TEXT NOT NULL, origin_version INTEGER NOT NULL DEFAULT 1,
 origin_kind TEXT NOT NULL CHECK(origin_kind IN ('user','verified_tool','inference')),
 related_reference TEXT,
 generation INTEGER NOT NULL DEFAULT 1, content_hash TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','active','invalidated','deleted')),
 automatic BOOLEAN NOT NULL DEFAULT false, config_version BIGINT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(owner_uuid,id)
);
CREATE INDEX recall_sources_origin ON recall.sources(owner_uuid,origin_ref,origin_version);
CREATE TABLE recall.operations (
 id TEXT PRIMARY KEY, owner_uuid UUID NOT NULL DEFAULT core.require_owner(),
 source_ref TEXT NOT NULL, generation INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('save','replace','delete')),
 dedup TEXT NOT NULL, external_ref UUID NOT NULL UNIQUE, payload TEXT,
 state TEXT NOT NULL CHECK(state IN ('queued','running','reconciling','succeeded','failed','cancelled')),
 effect TEXT NOT NULL CHECK(effect IN ('none','partial','committed','unknown')),
 availability TEXT NOT NULL CHECK(availability IN ('pending','available','blocked','unknown')),
 error_code TEXT, lease_until TIMESTAMPTZ, lease_generation BIGINT NOT NULL DEFAULT 0,
 submitted BOOLEAN NOT NULL DEFAULT false, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 cleanup_generation INTEGER NOT NULL DEFAULT 0,
 failures INTEGER NOT NULL DEFAULT 0, retry_at TIMESTAMPTZ,
 FOREIGN KEY(owner_uuid,source_ref) REFERENCES recall.sources(owner_uuid,id), UNIQUE(owner_uuid,dedup)
);
CREATE INDEX recall_operations_pending ON recall.operations(owner_uuid,state,created_at);
CREATE TABLE recall.exclusions (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES recall.spaces(owner_uuid),
 origin_ref TEXT NOT NULL, origin_version INTEGER NOT NULL,
 reason TEXT NOT NULL CHECK(reason IN ('temporary','source_invalidated','consent_revoked')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(owner_uuid,origin_ref,origin_version)
);
CREATE TABLE recall.events (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES recall.spaces(owner_uuid),
 reference TEXT NOT NULL, action TEXT NOT NULL, version BIGINT NOT NULL,
 trusted_origin TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE recall.spaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE recall.spaces FORCE ROW LEVEL SECURITY;
ALTER TABLE recall.sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE recall.sources FORCE ROW LEVEL SECURITY;
ALTER TABLE recall.operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE recall.operations FORCE ROW LEVEL SECURITY;
ALTER TABLE recall.exclusions ENABLE ROW LEVEL SECURITY;
ALTER TABLE recall.exclusions FORCE ROW LEVEL SECURITY;
ALTER TABLE recall.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE recall.events FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON recall.spaces TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
CREATE POLICY owner_scope ON recall.sources TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
CREATE POLICY owner_scope ON recall.operations TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
CREATE POLICY owner_scope ON recall.exclusions TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
CREATE POLICY owner_scope ON recall.events TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT,INSERT,UPDATE ON recall.spaces,recall.sources,recall.operations,recall.exclusions TO banking_runtime;
GRANT SELECT,INSERT ON recall.events TO banking_runtime;
GRANT USAGE ON SEQUENCE recall.events_id_seq TO banking_runtime;
-- Narrow background identity discovery follows pg02.due_owners. Runtime gets
-- no unrestricted scan, arbitrary owner argument, credential or source text.
CREATE POLICY worker_discovery ON recall.spaces TO banking_migrator USING(true);
CREATE POLICY worker_discovery ON recall.operations TO banking_migrator USING(true);
CREATE FUNCTION recall.due_owners() RETURNS TABLE(owner_uuid UUID,principal_id TEXT,user_id INTEGER)
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT DISTINCT s.owner_uuid,s.principal_id,s.owner_id FROM recall.spaces s
 JOIN recall.operations o ON o.owner_uuid=s.owner_uuid
 JOIN pg02.principals p ON p.owner_uuid=s.owner_uuid AND p.principal_id=s.principal_id
 JOIN core.users u ON u.id=s.owner_uuid AND u.public_id=s.owner_id
 WHERE o.state IN ('queued','running','reconciling') AND (o.retry_at IS NULL OR o.retry_at<=now())
 AND p.status='active' AND u.status='active' LIMIT 20;
$$;
REVOKE ALL ON FUNCTION recall.due_owners() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION recall.due_owners() TO banking_runtime;
