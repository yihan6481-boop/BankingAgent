-- Round 4: durable payment-target context for an unfinished dialogue goal.
--
-- A payment preparation is associated with its controlled lifecycle task via the
-- existing pg02.dialogue_links row. The document/preparation references that the
-- goal depends on are stored in `context` so a later context assembly can re-read
-- the CURRENT server-side payment state (never a stale snapshot and never as
-- authorization). The model still cannot execute/confirm funds from this data.
ALTER TABLE pg02.dialogue_links
    ADD COLUMN context JSONB NOT NULL DEFAULT '{}'::jsonb;
