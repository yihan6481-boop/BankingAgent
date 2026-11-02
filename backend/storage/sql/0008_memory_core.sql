-- Incremental Memory Core; never rewrites business rows or earlier migrations.
CREATE SCHEMA memory AUTHORIZATION banking_migrator;
REVOKE ALL ON SCHEMA memory FROM PUBLIC;
GRANT USAGE ON SCHEMA memory TO banking_runtime;

CREATE TABLE memory.entities (
 id UUID PRIMARY KEY, owner_id UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 subject_key TEXT NOT NULL CHECK(length(subject_key)=64),
 display_name TEXT NOT NULL CHECK(length(display_name)<=80),
 entity_type TEXT NOT NULL CHECK(entity_type IN ('self','contact')),
 UNIQUE(owner_id,id), UNIQUE(owner_id,subject_key)
);
CREATE TABLE memory.facts (
 id UUID PRIMARY KEY, owner_id UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 subject_id UUID NOT NULL, predicate TEXT NOT NULL CHECK(length(predicate)<=80),
 slot_key TEXT NOT NULL CHECK(length(slot_key)=64),
 fact_type TEXT NOT NULL CHECK(fact_type IN ('preference','important_date','recurring_payment','relationship')),
 type_version INTEGER NOT NULL CHECK(type_version=1),
 current_version INTEGER NOT NULL CHECK(current_version>0),
 status TEXT NOT NULL CHECK(status IN ('candidate','confirmed','conflict','withdrawn','deleted','invalidated')),
 sensitivity TEXT NOT NULL CHECK(sensitivity IN ('personal','sensitive')),
 visibility_scope TEXT NOT NULL CHECK(visibility_scope='private'),
 valid_from TIMESTAMPTZ, valid_until TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CHECK(valid_until IS NULL OR valid_from IS NULL OR valid_until>valid_from),
 UNIQUE(owner_id,id), UNIQUE(owner_id,slot_key),
 FOREIGN KEY(owner_id,subject_id) REFERENCES memory.entities(owner_id,id)
);
CREATE INDEX memory_facts_active ON memory.facts(owner_id,status,updated_at DESC);
CREATE INDEX memory_facts_subject ON memory.facts(owner_id,subject_id);
CREATE TABLE memory.fact_revisions (
 owner_id UUID NOT NULL DEFAULT core.require_owner(), fact_id UUID NOT NULL,
 version_no INTEGER NOT NULL CHECK(version_no>0),
 value_typed JSONB CHECK(value_typed IS NULL OR jsonb_typeof(value_typed)='object'),
 status TEXT NOT NULL CHECK(status IN ('candidate','confirmed','conflict','withdrawn','deleted','invalidated')),
 reason TEXT NOT NULL CHECK(reason IN ('create','update','confirm','withdraw','delete','source_changed')),
 recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(owner_id,fact_id,version_no),
 FOREIGN KEY(owner_id,fact_id) REFERENCES memory.facts(owner_id,id)
);
ALTER TABLE memory.facts ADD CONSTRAINT current_revision
 FOREIGN KEY(owner_id,id,current_version) REFERENCES memory.fact_revisions(owner_id,fact_id,version_no)
 DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE memory.fact_sources (
 owner_id UUID NOT NULL DEFAULT core.require_owner(), fact_id UUID NOT NULL, version_no INTEGER NOT NULL,
 source_kind TEXT NOT NULL CHECK(source_kind IN ('message','user_api')),
 message_id TEXT, request_id TEXT,
 recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(owner_id,fact_id,version_no),
 FOREIGN KEY(owner_id,fact_id,version_no) REFERENCES memory.fact_revisions(owner_id,fact_id,version_no),
 FOREIGN KEY(owner_id,message_id) REFERENCES pg02.memory_messages(owner_uuid,id),
 CHECK((source_kind='message' AND message_id IS NOT NULL AND request_id IS NULL)
    OR (source_kind='user_api' AND message_id IS NULL))
);
CREATE INDEX memory_fact_message ON memory.fact_sources(owner_id,message_id) WHERE message_id IS NOT NULL;

-- Validate typed snapshots also at the database write boundary. The service
-- performs the richer Pydantic validation before entering this transaction.
CREATE FUNCTION memory.guard_revision() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE f RECORD; v JSONB; keys TEXT[]; txt TEXT; BEGIN
 IF TG_OP='UPDATE' THEN
  IF NEW.owner_id!=OLD.owner_id OR NEW.fact_id!=OLD.fact_id OR NEW.version_no!=OLD.version_no
   OR NEW.status!=OLD.status OR NEW.reason!=OLD.reason OR NEW.recorded_at!=OLD.recorded_at
   OR NEW.value_typed IS NOT NULL
  THEN RAISE EXCEPTION 'memory_revision_immutable' USING ERRCODE='23514'; END IF;
  RETURN NEW;
 END IF;
 SELECT * INTO f FROM memory.facts WHERE owner_id=NEW.owner_id AND id=NEW.fact_id;
 IF NOT FOUND OR f.current_version!=NEW.version_no OR f.status!=NEW.status THEN
  RAISE EXCEPTION 'memory_revision_mismatch' USING ERRCODE='23514'; END IF;
 v:=NEW.value_typed;
 IF NEW.status IN ('deleted','withdrawn','invalidated') THEN
  IF v IS NOT NULL THEN RAISE EXCEPTION 'memory_terminal_value' USING ERRCODE='23514'; END IF;
  RETURN NEW;
 END IF;
 IF v IS NULL OR jsonb_typeof(v)!='object' OR v->>'kind' IS DISTINCT FROM f.fact_type THEN
  RAISE EXCEPTION 'memory_invalid_typed_value' USING ERRCODE='23514'; END IF;
 SELECT array_agg(k ORDER BY k) INTO keys FROM jsonb_object_keys(v) k;
 IF f.fact_type='recurring_payment' THEN
  IF keys!=ARRAY['amount_minor','currency','cycle','day','kind','timezone']
   OR jsonb_typeof(v->'amount_minor')!='number' OR (v->>'amount_minor')!~'^[1-9][0-9]{0,12}$'
   OR (v->>'amount_minor')::numeric>1000000000000 OR v->>'currency'!='CNY' OR v->>'cycle'!='monthly'
   OR jsonb_typeof(v->'day')!='number' OR (v->>'day')!~'^[0-9]{1,2}$' OR (v->>'day')::int NOT BETWEEN 1 AND 28
  THEN RAISE EXCEPTION 'memory_invalid_payment' USING ERRCODE='23514'; END IF;
 ELSIF f.fact_type='important_date' THEN
  IF keys!=ARRAY['day','kind','month','timezone','year'] OR jsonb_typeof(v->'month')!='number'
   OR jsonb_typeof(v->'day')!='number' OR (v->>'month')!~'^[0-9]{1,2}$' OR (v->>'day')!~'^[0-9]{1,2}$'
   OR (v->>'year' IS NOT NULL AND (jsonb_typeof(v->'year')!='number' OR (v->>'year')!~'^[0-9]{4}$'
    OR (v->>'year')::int NOT BETWEEN 1900 AND 2200))
  THEN RAISE EXCEPTION 'memory_invalid_date' USING ERRCODE='23514'; END IF;
  PERFORM make_date(COALESCE((v->>'year')::int,2000),(v->>'month')::int,(v->>'day')::int);
 ELSIF f.fact_type='preference' THEN
  IF keys!=ARRAY['kind','value'] OR jsonb_typeof(v->'value')!='string' THEN
   RAISE EXCEPTION 'memory_invalid_preference' USING ERRCODE='23514'; END IF;
  txt:=v->>'value';
 ELSIF f.fact_type='relationship' THEN
  IF keys!=ARRAY['description','kind','relation'] OR jsonb_typeof(v->'description')!='string'
   OR v->>'relation' NOT IN ('parent','child','spouse','sibling','friend','landlord','other_contact')
  THEN RAISE EXCEPTION 'memory_invalid_relationship' USING ERRCODE='23514'; END IF;
  txt:=v->>'description';
 END IF;
 IF f.fact_type IN ('recurring_payment','important_date') AND NOT EXISTS(
   SELECT 1 FROM pg_timezone_names WHERE name=v->>'timezone') THEN
  RAISE EXCEPTION 'memory_invalid_timezone' USING ERRCODE='23514'; END IF;
 IF txt IS NOT NULL AND (length(trim(txt)) NOT BETWEEN 1 AND 120
   OR txt ~* 'otp|password|secret|api.?key|密码|口令|密钥|验证码|动态码|system[[:space:]]*:|^[0-9]{6}$') THEN
  RAISE EXCEPTION 'memory_unsafe_content' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
REVOKE ALL ON FUNCTION memory.guard_revision() FROM PUBLIC;
CREATE TRIGGER memory_revision_guard BEFORE INSERT OR UPDATE ON memory.fact_revisions
 FOR EACH ROW EXECUTE FUNCTION memory.guard_revision();

CREATE FUNCTION memory.guard_source() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE state TEXT; role_value TEXT; status_value TEXT; BEGIN
 IF TG_OP='UPDATE' THEN
  IF NEW.owner_id!=OLD.owner_id OR NEW.fact_id!=OLD.fact_id OR NEW.version_no!=OLD.version_no
   OR NEW.source_kind!=OLD.source_kind OR NEW.message_id IS DISTINCT FROM OLD.message_id
   OR NEW.recorded_at!=OLD.recorded_at OR NEW.request_id IS NOT NULL
  THEN RAISE EXCEPTION 'memory_source_immutable' USING ERRCODE='23514'; END IF;
  RETURN NEW;
 END IF;
 SELECT status INTO state FROM memory.fact_revisions WHERE owner_id=NEW.owner_id AND fact_id=NEW.fact_id AND version_no=NEW.version_no;
 IF NEW.source_kind='user_api' AND state IN ('candidate','confirmed','conflict') AND
   (NEW.request_id IS NULL OR NEW.request_id !~ '^[0-9a-f-]{32,36}$') THEN
  RAISE EXCEPTION 'memory_request_source_required' USING ERRCODE='23514'; END IF;
 IF NEW.source_kind='message' AND state IN ('candidate','confirmed','conflict') THEN
  SELECT role,status INTO role_value,status_value FROM pg02.memory_messages WHERE owner_uuid=NEW.owner_id AND id=NEW.message_id;
  IF NOT FOUND OR status_value!='active' OR (state='confirmed' AND role_value!='user') THEN
   RAISE EXCEPTION 'memory_source_not_trusted' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END; $$;
REVOKE ALL ON FUNCTION memory.guard_source() FROM PUBLIC;
CREATE TRIGGER memory_source_guard BEFORE INSERT OR UPDATE ON memory.fact_sources
 FOR EACH ROW EXECUTE FUNCTION memory.guard_source();

DO $$ DECLARE t TEXT; BEGIN
 FOREACH t IN ARRAY ARRAY['entities','facts','fact_revisions','fact_sources'] LOOP
  EXECUTE format('ALTER TABLE memory.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE memory.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY owner_scope ON memory.%I TO banking_runtime USING(owner_id=core.require_owner()) WITH CHECK(owner_id=core.require_owner())',t);
  EXECUTE format('CREATE POLICY migration_scope ON memory.%I TO banking_migrator USING(true) WITH CHECK(true)',t);
  EXECUTE format('GRANT SELECT,INSERT,UPDATE ON memory.%I TO banking_runtime',t);
 END LOOP;
END $$;

-- Enforce monotonic revisions/terminal tombstones even for direct runtime SQL.
CREATE FUNCTION memory.guard_fact() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
BEGIN
 IF TG_OP='UPDATE' THEN
  IF NEW.id!=OLD.id OR NEW.owner_id!=OLD.owner_id OR NEW.subject_id!=OLD.subject_id OR NEW.slot_key!=OLD.slot_key
    OR NEW.fact_type!=OLD.fact_type OR NEW.type_version!=OLD.type_version
    OR (NEW.predicate IS DISTINCT FROM OLD.predicate AND NOT (NEW.status='deleted' AND NEW.predicate=''))
    OR NEW.current_version!=OLD.current_version+1
    OR (OLD.status='deleted') OR (OLD.status='withdrawn' AND NEW.status!='deleted')
  THEN RAISE EXCEPTION 'memory_transition_rejected' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END; $$;
REVOKE ALL ON FUNCTION memory.guard_fact() FROM PUBLIC;
CREATE TRIGGER memory_guard BEFORE UPDATE ON memory.facts FOR EACH ROW EXECUTE FUNCTION memory.guard_fact();

CREATE FUNCTION memory.check_current() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM memory.facts f JOIN memory.fact_revisions r
   ON r.owner_id=f.owner_id AND r.fact_id=f.id AND r.version_no=f.current_version
   JOIN memory.fact_sources s ON s.owner_id=r.owner_id AND s.fact_id=r.fact_id AND s.version_no=r.version_no
   WHERE f.id=NEW.id AND f.owner_id=NEW.owner_id AND r.status=f.status
     AND (f.status NOT IN ('candidate','confirmed','conflict') OR r.value_typed IS NOT NULL))
 THEN RAISE EXCEPTION 'memory_current_inconsistent' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END; $$;
REVOKE ALL ON FUNCTION memory.check_current() FROM PUBLIC;
CREATE CONSTRAINT TRIGGER memory_current AFTER INSERT OR UPDATE ON memory.facts
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION memory.check_current();

-- Source mutation and dependent invalidation are the same DB transaction.
-- No trigger carries raw text into revisions/audit; privacy invalidation erases values.
CREATE FUNCTION memory.source_changed() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE f RECORD; BEGIN
 IF NEW.content IS NOT DISTINCT FROM OLD.content AND NEW.status IS NOT DISTINCT FROM OLD.status THEN RETURN NEW; END IF;
 FOR f IN SELECT DISTINCT x.id,x.owner_id,x.current_version,x.status FROM memory.facts x
   JOIN memory.fact_sources s ON s.owner_id=x.owner_id AND s.fact_id=x.id
   WHERE s.message_id=OLD.id AND s.owner_id=OLD.owner_uuid
 LOOP
  UPDATE memory.fact_revisions SET value_typed=NULL WHERE owner_id=f.owner_id AND fact_id=f.id;
  UPDATE memory.fact_sources SET request_id=NULL WHERE owner_id=f.owner_id AND fact_id=f.id;
  IF f.status NOT IN ('deleted','withdrawn','invalidated') THEN
   UPDATE memory.facts SET current_version=current_version+1,status='invalidated',updated_at=now()
    WHERE owner_id=f.owner_id AND id=f.id;
   INSERT INTO memory.fact_revisions(owner_id,fact_id,version_no,value_typed,status,reason)
    VALUES(f.owner_id,f.id,f.current_version+1,NULL,'invalidated','source_changed');
   INSERT INTO memory.fact_sources(owner_id,fact_id,version_no,source_kind,message_id)
    VALUES(f.owner_id,f.id,f.current_version+1,'message',OLD.id);
  END IF;
 END LOOP;
 RETURN NEW;
END; $$;
REVOKE ALL ON FUNCTION memory.source_changed() FROM PUBLIC;
CREATE TRIGGER memory_source_changed AFTER UPDATE OF content,status ON pg02.memory_messages
 FOR EACH ROW EXECUTE FUNCTION memory.source_changed();
