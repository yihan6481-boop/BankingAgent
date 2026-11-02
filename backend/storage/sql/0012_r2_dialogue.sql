CREATE TABLE pg02.dialogues (
 id TEXT PRIMARY KEY, owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 owner_id INTEGER NOT NULL, principal_id TEXT NOT NULL, engine TEXT NOT NULL CHECK(engine='r2'),
 session_hash TEXT NOT NULL, next_turn INTEGER NOT NULL DEFAULT 1,
 active_turn TEXT, lease_until TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE pg02.dialogue_turns (
 id TEXT PRIMARY KEY, owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 dialogue_id TEXT NOT NULL REFERENCES pg02.dialogues(id), client_id TEXT NOT NULL,
 number INTEGER NOT NULL, input_hash TEXT NOT NULL, user_text TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('running','completed','failed','cancelled','interrupted')),
 cancel_requested BOOLEAN NOT NULL DEFAULT false, progress TEXT NOT NULL DEFAULT '正在理解',
 exchanges JSONB NOT NULL DEFAULT '[]', answer TEXT NOT NULL DEFAULT '', error_code TEXT,
 usage JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(dialogue_id,client_id), UNIQUE(dialogue_id,number)
);
CREATE TABLE pg02.dialogue_calls (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 turn_id TEXT NOT NULL REFERENCES pg02.dialogue_turns(id), call_id TEXT NOT NULL,
 name TEXT NOT NULL, arguments JSONB NOT NULL, status TEXT NOT NULL CHECK(status IN ('started','completed','unknown')),
 result JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(turn_id,call_id)
);
ALTER TABLE pg02.dialogues ENABLE ROW LEVEL SECURITY;
ALTER TABLE pg02.dialogues FORCE ROW LEVEL SECURITY;
ALTER TABLE pg02.dialogue_turns ENABLE ROW LEVEL SECURITY;
ALTER TABLE pg02.dialogue_turns FORCE ROW LEVEL SECURITY;
ALTER TABLE pg02.dialogue_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE pg02.dialogue_calls FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.dialogues TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
CREATE POLICY owner_scope ON pg02.dialogue_turns TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
CREATE POLICY owner_scope ON pg02.dialogue_calls TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
REVOKE ALL ON pg02.dialogues,pg02.dialogue_turns,pg02.dialogue_calls FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE ON pg02.dialogues,pg02.dialogue_turns,pg02.dialogue_calls TO banking_runtime;
