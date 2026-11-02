-- Canonical profile writes keep the legacy mobile lookup in sync. No security
-- notification table is touched. Several explicitly selected cards of one named
-- beneficiary may share a phone; different beneficiaries may not.
DROP INDEX payments.contacts_phone_unique;
CREATE FUNCTION payments.validate_contact_phone() RETURNS trigger
 LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
BEGIN
 IF NEW.phone IS NOT NULL AND NEW.status='active' AND EXISTS (
  SELECT 1 FROM payments.contacts p JOIN pg02.recipients a
   ON a.owner_uuid=p.owner_uuid AND a.recipient_id=p.recipient_id
  JOIN pg02.recipients b ON b.owner_uuid=NEW.owner_uuid AND b.recipient_id=NEW.recipient_id
  WHERE p.owner_uuid=NEW.owner_uuid AND p.phone=NEW.phone AND p.status='active'
   AND p.recipient_id<>NEW.recipient_id AND a.name<>b.name
 ) THEN RAISE EXCEPTION 'contact phone conflicts' USING ERRCODE='23505'; END IF;
 RETURN NEW;
END; $$;
REVOKE ALL ON FUNCTION payments.validate_contact_phone() FROM PUBLIC;
CREATE TRIGGER contact_phone_integrity BEFORE INSERT OR UPDATE ON payments.contacts
 FOR EACH ROW EXECUTE FUNCTION payments.validate_contact_phone();
CREATE FUNCTION payments.sync_contact_phone() RETURNS trigger
 LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
BEGIN
 DELETE FROM mobile_security.recipient_phones WHERE owner_uuid=NEW.owner_uuid AND recipient_id=NEW.recipient_id;
 IF NEW.phone IS NOT NULL AND NEW.status='active' THEN
  INSERT INTO mobile_security.recipient_phones(owner_uuid,recipient_id,phone,note)
   VALUES(NEW.owner_uuid,NEW.recipient_id,right(NEW.phone,11),NEW.note);
 END IF;
 RETURN NEW;
END; $$;
REVOKE ALL ON FUNCTION payments.sync_contact_phone() FROM PUBLIC;
CREATE TRIGGER contact_phone_sync AFTER INSERT OR UPDATE ON payments.contacts
 FOR EACH ROW EXECUTE FUNCTION payments.sync_contact_phone();
