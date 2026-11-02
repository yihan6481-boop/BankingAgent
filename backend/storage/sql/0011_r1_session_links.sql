-- Only conversation association metadata, never a second writable task snapshot.
CREATE TABLE pg02.session_links (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 owner_id INTEGER NOT NULL, principal_id TEXT NOT NULL,
 session_hash TEXT NOT NULL CHECK(session_hash ~ '^[0-9a-f]{64}$'),
 reference TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('query','transfer','result','task')),
 focused BOOLEAN NOT NULL DEFAULT false,
 associated_at TEXT NOT NULL,
 PRIMARY KEY(owner_id,principal_id,session_hash,reference)
);
CREATE UNIQUE INDEX session_one_focus ON pg02.session_links(owner_id,principal_id,session_hash) WHERE focused;
ALTER TABLE pg02.session_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE pg02.session_links FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.session_links TO banking_runtime
 USING (owner_uuid=core.require_owner()) WITH CHECK (owner_uuid=core.require_owner());
REVOKE ALL ON pg02.session_links FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE ON pg02.session_links TO banking_runtime;
