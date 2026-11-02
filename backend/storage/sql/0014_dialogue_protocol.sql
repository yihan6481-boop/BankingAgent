-- Provider continuation is private protocol data, never public conversation data.
ALTER TABLE pg02.dialogue_turns ADD COLUMN protocol_data JSONB NOT NULL DEFAULT '{}';
ALTER TABLE pg02.dialogue_turns ADD COLUMN pending_exchange JSONB;
ALTER TABLE pg02.dialogue_calls ADD COLUMN preparation_reference TEXT;
-- Existing owner RLS and runtime grants apply to these columns as well.
