-- Multiple private receiving accounts per phone, separate from notification
-- phones and the frozen payment-1.0 managed-contact binding contract.
CREATE TABLE mobile_security.recipient_phones(
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(),recipient_id INTEGER NOT NULL,
 phone TEXT NOT NULL CHECK(phone ~ '^1[3-9][0-9]{9}$'),
 note TEXT NOT NULL DEFAULT '' CHECK(length(note)<=100),updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(owner_uuid,recipient_id,phone),
 FOREIGN KEY(owner_uuid,recipient_id) REFERENCES pg02.recipients(owner_uuid,recipient_id)
);
ALTER TABLE mobile_security.recipient_phones ENABLE ROW LEVEL SECURITY;
ALTER TABLE mobile_security.recipient_phones FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON mobile_security.recipient_phones TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT,INSERT,UPDATE,DELETE ON mobile_security.recipient_phones TO banking_runtime;
