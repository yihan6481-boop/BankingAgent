-- Planning context only: no foreign key or trigger into executable tasks.
CREATE TABLE pg02.planning_drafts (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 draft_id TEXT PRIMARY KEY CHECK(draft_id ~ '^pd_[0-9a-f]{32}$'),
 owner_id INTEGER NOT NULL, principal_id TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>0),
 status TEXT NOT NULL CHECK(status IN ('pending','resumed','consumed','cancelled','expired','superseded')),
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 snapshot TEXT NOT NULL CHECK(octet_length(snapshot)<=16000),
 checksum TEXT NOT NULL CHECK(checksum ~ '^[0-9a-f]{64}$')
);
CREATE INDEX planning_drafts_owner ON pg02.planning_drafts(owner_uuid,principal_id,status);
ALTER TABLE pg02.planning_drafts ENABLE ROW LEVEL SECURITY;
ALTER TABLE pg02.planning_drafts FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.planning_drafts TO banking_runtime
 USING (owner_uuid=core.require_owner()) WITH CHECK (owner_uuid=core.require_owner());
REVOKE ALL ON pg02.planning_drafts FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE,DELETE ON pg02.planning_drafts TO banking_runtime;
