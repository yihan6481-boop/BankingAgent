-- Bounded, durable vision admission: a reservation is written before any
-- provider HTTP request and is counted against the owner's business-day
-- budget even when the call later fails or its lease expires. This gate is
-- independent of the chat runtime budget ledger; neither one authorizes a
-- payment or replaces the existing task preview/TOTP kernel.
CREATE TABLE payments.vision_calls (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 call_id TEXT NOT NULL CHECK(call_id ~ '^vc_[0-9a-f]{32}$'),
 kind TEXT NOT NULL CHECK(kind IN ('bill_screenshot','payment_document')),
 reference TEXT NOT NULL CHECK(length(reference) BETWEEN 1 AND 128),
 business_day DATE NOT NULL,
 state TEXT NOT NULL DEFAULT 'reserved' CHECK(state IN ('reserved','succeeded','failed','expired')),
 error_code TEXT,
 reserved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 finished_at TIMESTAMPTZ,
 PRIMARY KEY(owner_uuid,call_id)
);
CREATE INDEX vision_calls_day ON payments.vision_calls(owner_uuid,business_day);
CREATE INDEX vision_calls_inflight ON payments.vision_calls(owner_uuid,state,reserved_at);
ALTER TABLE payments.vision_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments.vision_calls FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON payments.vision_calls TO banking_runtime
 USING (owner_uuid=core.require_owner()) WITH CHECK (owner_uuid=core.require_owner());
REVOKE ALL ON payments.vision_calls FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE ON payments.vision_calls TO banking_runtime;
