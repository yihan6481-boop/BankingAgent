-- Admission is durable, account scoped, and committed with the new R2 turn.
-- A slot is a database invariant, not a process-local counter.
CREATE TABLE pg02.daily_message_admissions (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 client_message_id TEXT NOT NULL CHECK(client_message_id ~ '^[A-Za-z0-9_-]{8,100}$'),
 accepted_at TIMESTAMPTZ NOT NULL,
 quota_day DATE NOT NULL,
 slot SMALLINT NOT NULL CHECK(slot BETWEEN 1 AND 200),
 input_hash TEXT NOT NULL,
 dialogue_id TEXT REFERENCES pg02.dialogues(id),
 turn_id TEXT UNIQUE REFERENCES pg02.dialogue_turns(id),
 legacy_reference TEXT UNIQUE,
 legacy_response JSONB,
 CHECK(quota_day=(accepted_at AT TIME ZONE 'Asia/Shanghai')::date),
 CHECK((turn_id IS NOT NULL AND dialogue_id IS NOT NULL AND legacy_reference IS NULL)
    OR (turn_id IS NULL AND dialogue_id IS NULL AND legacy_reference IS NOT NULL)),
 PRIMARY KEY(owner_uuid,client_message_id),
 UNIQUE(owner_uuid,quota_day,slot)
);
ALTER TABLE pg02.daily_message_admissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE pg02.daily_message_admissions FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.daily_message_admissions TO banking_runtime
 USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
REVOKE ALL ON pg02.daily_message_admissions FROM PUBLIC;
GRANT SELECT,INSERT ON pg02.daily_message_admissions TO banking_runtime;
GRANT UPDATE(legacy_response) ON pg02.daily_message_admissions TO banking_runtime;
