-- External uploads only. No changes to the bank ledger or old migration bytes.
CREATE SCHEMA billvision AUTHORIZATION banking_migrator;
REVOKE ALL ON SCHEMA billvision FROM PUBLIC;
GRANT USAGE ON SCHEMA billvision TO banking_runtime;
CREATE TABLE billvision.documents (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 document_id TEXT NOT NULL CHECK(document_id ~ '^bv_[0-9a-f]{32}$'),
 image_hash TEXT NOT NULL CHECK(image_hash ~ '^[0-9a-f]{64}$'),
 image BYTEA NOT NULL CHECK(octet_length(image)<=10485760),
 mime TEXT NOT NULL CHECK(mime IN ('image/png','image/jpeg')),
 width INTEGER NOT NULL CHECK(width>0), height INTEGER NOT NULL CHECK(height>0 AND width::BIGINT*height<=20000000),
 state TEXT NOT NULL CHECK(state IN ('uploaded','recognizing','recognition_failed','review','confirmed','unsupported')),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
 platform TEXT NOT NULL DEFAULT 'unknown' CHECK(platform IN ('wechat','alipay','unknown')),
 original JSONB, rows JSONB NOT NULL DEFAULT '[]',
 recognition_token TEXT, recognition_started TIMESTAMPTZ,
 error_code TEXT, confirmation JSONB,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(owner_uuid,document_id), UNIQUE(owner_uuid,image_hash),
 CHECK(jsonb_typeof(rows)='array' AND jsonb_array_length(rows)<=100)
);
CREATE TABLE billvision.versions (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(), document_id TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>0), operation TEXT NOT NULL,
 rows JSONB NOT NULL, platform TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(owner_uuid,document_id,revision),
 FOREIGN KEY(owner_uuid,document_id) REFERENCES billvision.documents(owner_uuid,document_id) ON DELETE CASCADE
);
CREATE TABLE billvision.records (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(), document_id TEXT NOT NULL,
 row_id TEXT NOT NULL, revision INTEGER NOT NULL, platform TEXT NOT NULL,
 event_key TEXT, value JSONB NOT NULL,
 PRIMARY KEY(owner_uuid,document_id,row_id), UNIQUE(owner_uuid,platform,event_key),
 FOREIGN KEY(owner_uuid,document_id) REFERENCES billvision.documents(owner_uuid,document_id) ON DELETE CASCADE,
 FOREIGN KEY(owner_uuid,document_id,revision) REFERENCES billvision.versions(owner_uuid,document_id,revision)
);
CREATE TABLE billvision.audit (
 audit_id UUID PRIMARY KEY, owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 document_id TEXT NOT NULL, revision INTEGER NOT NULL, operation TEXT NOT NULL,
 row_count INTEGER NOT NULL CHECK(row_count>=0), created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
DO $$ DECLARE t TEXT; BEGIN
 FOREACH t IN ARRAY ARRAY['documents','versions','records','audit'] LOOP
  EXECUTE format('ALTER TABLE billvision.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE billvision.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY owner_scope ON billvision.%I TO banking_runtime USING (owner_uuid=core.require_owner()) WITH CHECK (owner_uuid=core.require_owner())',t);
  EXECUTE format('REVOKE ALL ON billvision.%I FROM PUBLIC',t);
 END LOOP;
END $$;
GRANT SELECT,INSERT,UPDATE,DELETE ON billvision.documents,billvision.records TO banking_runtime;
GRANT SELECT,INSERT,DELETE ON billvision.versions TO banking_runtime;
GRANT SELECT,INSERT ON billvision.audit TO banking_runtime;
