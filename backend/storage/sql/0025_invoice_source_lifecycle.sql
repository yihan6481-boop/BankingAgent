-- Source revision/retraction invalidates only dependent invoice imports.
-- Old versions and audit rows are retained; no financial ledger is changed.
CREATE FUNCTION mobile_security.invalidate_invoice_source() RETURNS trigger
 LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE d billvision.documents;
BEGIN
 IF TG_OP='UPDATE' AND OLD.revision=NEW.revision THEN RETURN NULL;END IF;
 FOR d IN UPDATE billvision.documents SET state='review',revision=revision+1,
   confirmation=NULL,updated_at=now()
   WHERE platform='invoice' AND original->>'source'='reviewed_invoice'
     AND original->>'payment_document_id'=OLD.document_id RETURNING * LOOP
  DELETE FROM billvision.records WHERE document_id=d.document_id AND owner_uuid=d.owner_uuid;
  INSERT INTO billvision.versions(owner_uuid,document_id,revision,operation,rows,platform)
   VALUES(d.owner_uuid,d.document_id,d.revision,'invoice_source_invalidated',d.rows,d.platform);
  INSERT INTO billvision.audit(audit_id,owner_uuid,document_id,revision,operation,row_count)
   VALUES(gen_random_uuid(),d.owner_uuid,d.document_id,d.revision,'invoice_source_invalidated',jsonb_array_length(d.rows));
 END LOOP;
 RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION mobile_security.invalidate_invoice_source() FROM PUBLIC;
CREATE TRIGGER invoice_source_updated AFTER UPDATE OF revision ON payments.payment_documents
 FOR EACH ROW WHEN (OLD.kind='invoice') EXECUTE FUNCTION mobile_security.invalidate_invoice_source();
CREATE TRIGGER invoice_source_deleted AFTER DELETE ON payments.payment_documents
 FOR EACH ROW WHEN (OLD.kind='invoice') EXECUTE FUNCTION mobile_security.invalidate_invoice_source();
