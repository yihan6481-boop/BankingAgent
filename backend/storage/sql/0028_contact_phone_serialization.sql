-- Keep full-phone conflict checks valid under concurrent profile writes.
-- The owner/phone pair is serialized; this grants no data access or trust.
CREATE FUNCTION payments.lock_contact_phone() RETURNS trigger
 LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
BEGIN
 IF NEW.phone IS NOT NULL AND NEW.status='active' THEN
  PERFORM pg_advisory_xact_lock(hashtextextended(NEW.owner_uuid::text||':'||NEW.phone,0));
 END IF;
 RETURN NEW;
END; $$;
REVOKE ALL ON FUNCTION payments.lock_contact_phone() FROM PUBLIC;
CREATE TRIGGER contact_phone_00_lock BEFORE INSERT OR UPDATE ON payments.contacts
 FOR EACH ROW EXECUTE FUNCTION payments.lock_contact_phone();
