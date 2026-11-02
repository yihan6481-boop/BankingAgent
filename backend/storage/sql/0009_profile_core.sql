-- Derived analytics only. Requires 0008; no business-row changes or seed.
CREATE TABLE memory.profile_consents (
 owner_id UUID PRIMARY KEY DEFAULT core.require_owner() REFERENCES core.users(id),
 id UUID NOT NULL UNIQUE, purpose TEXT NOT NULL CHECK(purpose='financial_profile'),
 policy_version TEXT NOT NULL CHECK(policy_version='profile-consent-1'),
 version BIGINT NOT NULL CHECK(version>0), status TEXT NOT NULL CHECK(status IN ('granted','revoked')),
 granted_at TIMESTAMPTZ NOT NULL, revoked_at TIMESTAMPTZ,
 CHECK((status='granted' AND revoked_at IS NULL) OR (status='revoked' AND revoked_at IS NOT NULL)),
 UNIQUE(owner_id,id)
);
CREATE TABLE memory.profile_snapshots (
 id UUID PRIMARY KEY, owner_id UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 consent_id UUID NOT NULL, consent_version BIGINT NOT NULL,
 purpose TEXT NOT NULL CHECK(purpose='financial_profile'),
 window_start DATE NOT NULL, window_end DATE NOT NULL, timezone TEXT NOT NULL CHECK(timezone='Asia/Shanghai'),
 computed_at TIMESTAMPTZ NOT NULL, algorithm_version TEXT NOT NULL, metric_schema_version TEXT NOT NULL,
 sample_count INTEGER NOT NULL CHECK(sample_count>=0), coverage_summary JSONB NOT NULL,
 input_watermark TEXT NOT NULL CHECK(length(input_watermark)=64),
 status TEXT NOT NULL CHECK(status IN ('ready','stale')),
 CHECK(window_end>window_start), UNIQUE(owner_id,id),
 FOREIGN KEY(owner_id,consent_id) REFERENCES memory.profile_consents(owner_id,id),
 UNIQUE(owner_id,consent_id,consent_version,window_start,window_end,algorithm_version,metric_schema_version,input_watermark)
);
CREATE TABLE memory.profile_metrics (
 owner_id UUID NOT NULL DEFAULT core.require_owner(), snapshot_id UUID NOT NULL,
 metric_code TEXT NOT NULL CHECK(metric_code IN ('category_structure','merchant_frequency','recorded_income',
 'recurring_candidates','confirmed_obligations','spending_trend','classification_totals')),
 data_type TEXT NOT NULL CHECK(data_type='object'), value_typed JSONB NOT NULL CHECK(jsonb_typeof(value_typed)='object'),
 unit TEXT NOT NULL CHECK(unit='CNY_minor_and_count'), sample_count INTEGER NOT NULL CHECK(sample_count>=0),
 evidence_summary JSONB NOT NULL, uncertainty JSONB NOT NULL,
 PRIMARY KEY(owner_id,snapshot_id,metric_code),
 FOREIGN KEY(owner_id,snapshot_id) REFERENCES memory.profile_snapshots(owner_id,id) ON DELETE CASCADE
);
CREATE INDEX profile_window ON memory.profile_snapshots(owner_id,window_start,window_end);
DO $$ DECLARE t TEXT; BEGIN
 FOREACH t IN ARRAY ARRAY['profile_consents','profile_snapshots','profile_metrics'] LOOP
  EXECUTE format('ALTER TABLE memory.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE memory.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY owner_access ON memory.%I TO banking_runtime USING(owner_id=core.require_owner()) WITH CHECK(owner_id=core.require_owner())',t);
  EXECUTE format('CREATE POLICY migration_access ON memory.%I TO banking_migrator USING(true) WITH CHECK(true)',t);
  EXECUTE format('GRANT SELECT,INSERT,UPDATE,DELETE ON memory.%I TO banking_runtime',t);
 END LOOP;
END $$;
-- Conservative invalidation includes every relevant fact, never copies facts back.
CREATE FUNCTION memory.invalidate_profiles() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
BEGIN
 IF TG_TABLE_NAME='transactions' THEN
  UPDATE memory.profile_snapshots SET status='stale' WHERE owner_id=COALESCE(NEW.owner_uuid,OLD.owner_uuid);
 ELSE
  DELETE FROM memory.profile_snapshots WHERE owner_id=COALESCE(NEW.owner_id,OLD.owner_id);
 END IF;
 RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION memory.invalidate_profiles() FROM PUBLIC;
CREATE TRIGGER profile_ledger_change AFTER INSERT OR UPDATE OR DELETE ON pg02.transactions
 FOR EACH ROW EXECUTE FUNCTION memory.invalidate_profiles();
CREATE TRIGGER profile_fact_change AFTER INSERT OR UPDATE OR DELETE ON memory.facts
 FOR EACH ROW EXECUTE FUNCTION memory.invalidate_profiles();
CREATE FUNCTION memory.revoke_profiles() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
BEGIN
 IF NEW.status='revoked' THEN DELETE FROM memory.profile_snapshots WHERE owner_id=NEW.owner_id; END IF;
 RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION memory.revoke_profiles() FROM PUBLIC;
CREATE TRIGGER profile_consent_revoke AFTER UPDATE ON memory.profile_consents
 FOR EACH ROW EXECUTE FUNCTION memory.revoke_profiles();
