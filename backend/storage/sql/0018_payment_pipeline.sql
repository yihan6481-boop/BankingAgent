-- A-line payment pipeline: contacts, recipient resolution, payment documents,
-- obligations, preparation links and receipt reconciliation. Every table is
-- owner scoped with FORCE RLS. No bank ledger table is created or mutated here;
-- payment preparation reuses the existing transfer draft/task/authorization
-- kernel, and reconciliation only stores evidence links.
CREATE SCHEMA payments AUTHORIZATION banking_migrator;
REVOKE ALL ON SCHEMA payments FROM PUBLIC;
GRANT USAGE ON SCHEMA payments TO banking_runtime;

-- Simulated recipient directory kind only; not a real-bank ownership proof.
ALTER TABLE pg02.recipients ADD COLUMN beneficiary_kind TEXT NOT NULL DEFAULT 'person'
 CHECK(beneficiary_kind IN ('person','institution'));

CREATE TABLE payments.contacts (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 contact_id TEXT NOT NULL CHECK(contact_id ~ '^ct_[0-9a-f]{32}$'),
 recipient_id INTEGER NOT NULL,
 display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 1 AND 100),
 phone TEXT,
 phone_bound_at TIMESTAMPTZ,
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(owner_uuid,contact_id),
 UNIQUE(owner_uuid,recipient_id),
 FOREIGN KEY(owner_uuid,recipient_id) REFERENCES pg02.recipients(owner_uuid,recipient_id),
 CHECK(phone IS NULL OR phone ~ '^\+[1-9][0-9]{7,14}$'),
 CHECK((phone IS NULL AND phone_bound_at IS NULL) OR (phone IS NOT NULL AND phone_bound_at IS NOT NULL))
);
CREATE UNIQUE INDEX contacts_phone_unique ON payments.contacts(owner_uuid,phone)
 WHERE phone IS NOT NULL AND status='active';

CREATE TABLE payments.recipient_resolutions (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 resolution_id TEXT NOT NULL CHECK(resolution_id ~ '^rr_[0-9a-f]{32}$'),
 client_request_id TEXT NOT NULL CHECK(client_request_id ~ '^[A-Za-z0-9_-]{8,100}$'),
 selector_kind TEXT NOT NULL CHECK(selector_kind IN ('contact','phone','bank_account','document_payee')),
 selector JSONB NOT NULL,
 selector_fingerprint TEXT NOT NULL CHECK(selector_fingerprint ~ '^[0-9a-f]{64}$'),
 status TEXT NOT NULL CHECK(status IN ('resolved','ambiguous','not_found','unsupported','needs_information')),
 candidates JSONB NOT NULL DEFAULT '[]',
 selected_candidate_id TEXT,
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
 expires_at TIMESTAMPTZ NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(owner_uuid,resolution_id),
 UNIQUE(owner_uuid,client_request_id),
 CHECK(jsonb_typeof(selector)='object' AND jsonb_typeof(candidates)='array')
);
CREATE INDEX recipient_resolutions_expiry ON payments.recipient_resolutions(owner_uuid,expires_at);

CREATE TABLE payments.payment_documents (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 document_id TEXT NOT NULL CHECK(document_id ~ '^pd_[0-9a-f]{32}$'),
 client_request_id TEXT NOT NULL CHECK(client_request_id ~ '^[A-Za-z0-9_-]{8,100}$'),
 image_hash TEXT NOT NULL CHECK(image_hash ~ '^[0-9a-f]{64}$'),
 image BYTEA NOT NULL CHECK(octet_length(image)<=10485760),
 mime TEXT NOT NULL CHECK(mime IN ('image/png','image/jpeg')),
 width INTEGER NOT NULL CHECK(width>0),
 height INTEGER NOT NULL CHECK(height>0 AND width::BIGINT*height<=20000000),
 state TEXT NOT NULL DEFAULT 'uploaded' CHECK(state IN ('uploaded','extracting','extraction_failed','review','ready','prepared','reconciled')),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
 kind TEXT NOT NULL DEFAULT 'unknown' CHECK(kind IN ('payment_notice','invoice','receipt','bank_receipt','unknown')),
 extraction JSONB,
 fields JSONB NOT NULL DEFAULT '{}',
 selected_item_ids JSONB NOT NULL DEFAULT '[]',
 selected_payee_index INTEGER CHECK(selected_payee_index IS NULL OR selected_payee_index>=0),
 user_notes TEXT CHECK(user_notes IS NULL OR length(user_notes)<=1000),
 user_confirmed_own_bill BOOLEAN NOT NULL DEFAULT false,
 review_digest TEXT CHECK(review_digest IS NULL OR review_digest ~ '^[0-9a-f]{64}$'),
 extraction_token TEXT,
 extraction_started TIMESTAMPTZ,
 error_code TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(owner_uuid,document_id),
 UNIQUE(owner_uuid,client_request_id),
 UNIQUE(owner_uuid,image_hash),
 CHECK(jsonb_typeof(fields)='object' AND jsonb_typeof(selected_item_ids)='array'
   AND jsonb_array_length(selected_item_ids)<=20)
);
CREATE INDEX payment_documents_state ON payments.payment_documents(owner_uuid,state,created_at);

CREATE TABLE payments.document_versions (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 document_id TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>0),
 operation TEXT NOT NULL CHECK(operation IN ('upload','extract','review','prepare','reconcile','link')),
 kind TEXT NOT NULL,
 fields JSONB NOT NULL,
 digest TEXT CHECK(digest IS NULL OR digest ~ '^[0-9a-f]{64}$'),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(owner_uuid,document_id,revision),
 FOREIGN KEY(owner_uuid,document_id) REFERENCES payments.payment_documents(owner_uuid,document_id) ON DELETE CASCADE
);

CREATE TABLE payments.obligations (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 obligation_id TEXT NOT NULL CHECK(obligation_id ~ '^ob_[0-9a-f]{32}$'),
 fingerprint TEXT NOT NULL CHECK(fingerprint ~ '^[0-9a-f]{64}$'),
 kind TEXT NOT NULL CHECK(kind IN ('payment_notice','invoice')),
 institution TEXT,
 bill_number TEXT,
 payer_name TEXT,
 period_start DATE,
 period_end DATE,
 period_label TEXT,
 amount_cents BIGINT NOT NULL CHECK(amount_cents>0),
 currency TEXT NOT NULL DEFAULT 'CNY' CHECK(currency='CNY'),
 state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','prepared','linked','cancelled')),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(owner_uuid,obligation_id),
 UNIQUE(owner_uuid,fingerprint)
);

CREATE TABLE payments.document_obligations (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 document_id TEXT NOT NULL,
 obligation_id TEXT NOT NULL,
 document_revision INTEGER NOT NULL CHECK(document_revision>0),
 linked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(owner_uuid,document_id),
 FOREIGN KEY(owner_uuid,document_id) REFERENCES payments.payment_documents(owner_uuid,document_id) ON DELETE CASCADE,
 FOREIGN KEY(owner_uuid,obligation_id) REFERENCES payments.obligations(owner_uuid,obligation_id)
);

CREATE TABLE payments.preparations (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 preparation_id TEXT NOT NULL CHECK(preparation_id ~ '^pp_[0-9a-f]{32}$'),
 client_request_id TEXT NOT NULL CHECK(client_request_id ~ '^[A-Za-z0-9_-]{8,100}$'),
 document_id TEXT NOT NULL,
 document_revision INTEGER NOT NULL CHECK(document_revision>0),
 obligation_id TEXT NOT NULL,
 item_ids JSONB NOT NULL,
 amount_cents BIGINT NOT NULL CHECK(amount_cents>0),
 currency TEXT NOT NULL DEFAULT 'CNY' CHECK(currency='CNY'),
 recipient_resolution_id TEXT NOT NULL,
 candidate_id TEXT NOT NULL,
 recipient_id INTEGER NOT NULL,
 recipient_fingerprint TEXT NOT NULL CHECK(recipient_fingerprint ~ '^[0-9a-f]{64}$'),
 from_card_id INTEGER NOT NULL,
 remark TEXT CHECK(remark IS NULL OR length(remark)<=100),
 draft_reference TEXT,
 draft_revision INTEGER CHECK(draft_revision IS NULL OR draft_revision>0),
 task_id TEXT,
 task_revision INTEGER CHECK(task_revision IS NULL OR task_revision>0),
 state TEXT NOT NULL DEFAULT 'preparing' CHECK(state IN ('preparing','draft_ready','prepared','cancelled')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(owner_uuid,preparation_id),
 UNIQUE(owner_uuid,client_request_id),
 FOREIGN KEY(owner_uuid,document_id) REFERENCES payments.payment_documents(owner_uuid,document_id),
 FOREIGN KEY(owner_uuid,obligation_id) REFERENCES payments.obligations(owner_uuid,obligation_id),
 CHECK(jsonb_typeof(item_ids)='array' AND jsonb_array_length(item_ids)>0)
);
-- A payment obligation can have at most one active preparation, independent of
-- request keys, client retries or two different images of the same bill.
CREATE UNIQUE INDEX preparations_one_active ON payments.preparations(owner_uuid,obligation_id)
 WHERE state IN ('preparing','draft_ready','prepared');

CREATE TABLE payments.reconciliation_runs (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 reconciliation_id TEXT NOT NULL CHECK(reconciliation_id ~ '^rc_[0-9a-f]{32}$'),
 client_request_id TEXT NOT NULL CHECK(client_request_id ~ '^[A-Za-z0-9_-]{8,100}$'),
 document_id TEXT NOT NULL,
 document_revision INTEGER NOT NULL CHECK(document_revision>0),
 document_digest TEXT NOT NULL CHECK(document_digest ~ '^[0-9a-f]{64}$'),
 range_start TEXT NOT NULL CHECK(range_start ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'),
 range_end_exclusive TEXT NOT NULL CHECK(range_end_exclusive ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'),
 match_status TEXT NOT NULL CHECK(match_status IN ('unique_match','multiple_candidates','not_found','conflict')),
 candidates JSONB NOT NULL DEFAULT '[]',
 compared_fields JSONB NOT NULL DEFAULT '[]',
 unknown_fields JSONB NOT NULL DEFAULT '[]',
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(owner_uuid,reconciliation_id),
 UNIQUE(owner_uuid,client_request_id),
 FOREIGN KEY(owner_uuid,document_id) REFERENCES payments.payment_documents(owner_uuid,document_id)
);

CREATE TABLE payments.transaction_links (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 link_id TEXT NOT NULL CHECK(link_id ~ '^tl_[0-9a-f]{32}$'),
 client_request_id TEXT NOT NULL CHECK(client_request_id ~ '^[A-Za-z0-9_-]{8,100}$'),
 document_id TEXT NOT NULL,
 document_revision INTEGER NOT NULL CHECK(document_revision>0),
 document_digest TEXT NOT NULL CHECK(document_digest ~ '^[0-9a-f]{64}$'),
 reconciliation_id TEXT NOT NULL,
 reconciliation_revision INTEGER NOT NULL CHECK(reconciliation_revision>0),
 transaction_id BIGINT NOT NULL,
 evidence JSONB NOT NULL,
 payment_status TEXT NOT NULL CHECK(payment_status IN ('found_in_this_system','not_found_in_this_system','unknown')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(owner_uuid,link_id),
 UNIQUE(owner_uuid,client_request_id),
 UNIQUE(owner_uuid,document_id,transaction_id),
 FOREIGN KEY(owner_uuid,document_id) REFERENCES payments.payment_documents(owner_uuid,document_id),
 FOREIGN KEY(owner_uuid,reconciliation_id) REFERENCES payments.reconciliation_runs(owner_uuid,reconciliation_id)
);

CREATE TABLE payments.client_requests (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 operation TEXT NOT NULL CHECK(operation IN ('contact.create','contact.update','contact.archive',
   'resolution.create','resolution.select','document.upload','document.extract','document.review',
   'document.prepare','document.reconcile','document.link')),
 client_request_id TEXT NOT NULL CHECK(client_request_id ~ '^[A-Za-z0-9_-]{8,100}$'),
 request_hash TEXT NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
 resource_id TEXT,
 result JSONB,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(owner_uuid,operation,client_request_id)
);

CREATE TABLE payments.audit (
 audit_id UUID PRIMARY KEY,
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 document_id TEXT,
 operation TEXT NOT NULL,
 revision INTEGER,
 detail JSONB NOT NULL DEFAULT '{}',
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$ DECLARE t TEXT; BEGIN
 FOREACH t IN ARRAY ARRAY['contacts','recipient_resolutions','payment_documents','document_versions',
   'obligations','document_obligations','preparations','reconciliation_runs','transaction_links',
   'client_requests','audit'] LOOP
  EXECUTE format('ALTER TABLE payments.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE payments.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY owner_scope ON payments.%I TO banking_runtime '
    'USING (owner_uuid=core.require_owner()) WITH CHECK (owner_uuid=core.require_owner())',t);
  EXECUTE format('REVOKE ALL ON payments.%I FROM PUBLIC',t);
 END LOOP;
END $$;
GRANT SELECT,INSERT,UPDATE ON payments.contacts TO banking_runtime;
GRANT SELECT,INSERT,UPDATE ON payments.recipient_resolutions TO banking_runtime;
GRANT SELECT,INSERT,UPDATE,DELETE ON payments.payment_documents TO banking_runtime;
GRANT SELECT,INSERT ON payments.document_versions TO banking_runtime;
GRANT SELECT,INSERT,UPDATE ON payments.obligations TO banking_runtime;
GRANT SELECT,INSERT,UPDATE,DELETE ON payments.document_obligations TO banking_runtime;
GRANT SELECT,INSERT,UPDATE ON payments.preparations TO banking_runtime;
GRANT SELECT,INSERT ON payments.reconciliation_runs TO banking_runtime;
GRANT SELECT,INSERT ON payments.transaction_links TO banking_runtime;
GRANT SELECT,INSERT,UPDATE ON payments.client_requests TO banking_runtime;
GRANT SELECT,INSERT ON payments.audit TO banking_runtime;
-- Evidence tables are append-only: no UPDATE/DELETE is granted to runtime.
