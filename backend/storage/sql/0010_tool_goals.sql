-- Non-authorizing structured goals and immutable query snapshots.
CREATE TABLE pg02.tool_goals (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 reference TEXT PRIMARY KEY CHECK(reference ~ '^tg_[0-9a-f]{32}$'),
 owner_id INTEGER NOT NULL, principal_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('query','transfer','result')),
 status TEXT NOT NULL CHECK(status IN ('open','closed','preparing','linked','snapshot')),
 revision INTEGER NOT NULL CHECK(revision>0), payload TEXT NOT NULL,
 checksum TEXT NOT NULL CHECK(checksum ~ '^[0-9a-f]{64}$')
);
ALTER TABLE pg02.tool_goals ENABLE ROW LEVEL SECURITY;
ALTER TABLE pg02.tool_goals FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.tool_goals TO banking_runtime
 USING (owner_uuid=core.require_owner()) WITH CHECK (owner_uuid=core.require_owner());
REVOKE ALL ON pg02.tool_goals FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE ON pg02.tool_goals TO banking_runtime;
