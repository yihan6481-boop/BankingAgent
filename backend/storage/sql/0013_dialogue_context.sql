-- Conversation provenance only: business values remain in goals/tasks.
CREATE TABLE pg02.dialogue_links (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 dialogue_id TEXT NOT NULL REFERENCES pg02.dialogues(id), reference TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('transfer','query','result','task')),
 source_turn TEXT NOT NULL REFERENCES pg02.dialogue_turns(id), source_call TEXT NOT NULL,
 associated_at TIMESTAMPTZ NOT NULL DEFAULT now(), focused BOOLEAN NOT NULL DEFAULT false,
 PRIMARY KEY(dialogue_id,reference),
 FOREIGN KEY(source_turn,source_call) REFERENCES pg02.dialogue_calls(turn_id,call_id)
);
CREATE UNIQUE INDEX dialogue_one_focus ON pg02.dialogue_links(dialogue_id) WHERE focused;
ALTER TABLE pg02.dialogue_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE pg02.dialogue_links FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.dialogue_links TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
REVOKE ALL ON pg02.dialogue_links FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE ON pg02.dialogue_links TO banking_runtime;
ALTER TABLE pg02.dialogue_calls ADD COLUMN outcome TEXT NOT NULL DEFAULT 'pending'
 CHECK(outcome IN ('pending','succeeded','failed','unknown'));
ALTER TABLE pg02.dialogue_calls ADD COLUMN protocol_status TEXT NOT NULL DEFAULT 'pending'
 CHECK(protocol_status IN ('pending','paired','unpaired'));
ALTER TABLE pg02.dialogue_calls ADD COLUMN resolution JSONB;
-- The non-superuser table owner must migrate existing rows despite FORCE RLS.
-- Both changes are transactional; FORCE is restored before migration commits.
ALTER TABLE pg02.dialogue_calls NO FORCE ROW LEVEL SECURITY;
ALTER TABLE pg02.dialogue_turns NO FORCE ROW LEVEL SECURITY;
UPDATE pg02.dialogue_calls c SET
 outcome=CASE WHEN status IN ('started','unknown') OR result->>'status'='commit_unknown' THEN 'unknown'
              WHEN result->>'status' IN ('completed','saved_incomplete','ambiguous') OR jsonb_array_length(COALESCE(result->'effects','[]'::jsonb))>0 THEN 'succeeded'
              ELSE 'failed' END,
 protocol_status=CASE WHEN EXISTS (SELECT 1 FROM pg02.dialogue_turns t,
     jsonb_array_elements(t.exchanges) m WHERE t.id=c.turn_id AND m->>'role'='tool' AND m->>'tool_call_id'=c.call_id)
     THEN 'paired' ELSE 'unpaired' END;
ALTER TABLE pg02.dialogue_calls FORCE ROW LEVEL SECURITY;
ALTER TABLE pg02.dialogue_turns FORCE ROW LEVEL SECURITY;
-- Existing associations are recovered lazily from verified exact call references,
-- never from session_links, recency, names, amounts or natural language.
