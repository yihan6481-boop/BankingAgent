-- PG03: one ledger, controlled reference aliases, no application data seed.
CREATE TABLE pg02.merchant_aliases (
 alias TEXT PRIMARY KEY CHECK(length(alias)>0),
 merchant_key TEXT NOT NULL CHECK(length(merchant_key)>0),
 source TEXT NOT NULL CHECK(source='controlled_v1')
);
INSERT INTO pg02.merchant_aliases VALUES ('淘宝','taobao','controlled_v1'),('淘宝网','taobao','controlled_v1');
GRANT SELECT ON pg02.merchant_aliases TO banking_runtime;

ALTER TABLE pg02.transactions DROP CONSTRAINT transactions_transaction_type_check;
ALTER TABLE pg02.transactions ALTER COLUMN source_order_id DROP NOT NULL;
ALTER TABLE pg02.transactions ADD COLUMN merchant_key TEXT;
ALTER TABLE pg02.transactions ADD COLUMN refund_of_transaction_id BIGINT;
ALTER TABLE pg02.transactions ADD COLUMN external_source_id TEXT;
ALTER TABLE pg02.transactions ADD UNIQUE(owner_uuid,transaction_id);
ALTER TABLE pg02.transactions ADD UNIQUE(owner_uuid,external_source_id);
ALTER TABLE pg02.transactions ADD FOREIGN KEY(owner_uuid,refund_of_transaction_id)
 REFERENCES pg02.transactions(owner_uuid,transaction_id);
ALTER TABLE pg02.transactions ADD CONSTRAINT transaction_kind CHECK (
 transaction_type IN ('purchase','transfer','wealth_buy','wealth_redeem','refund','income','unknown'));
ALTER TABLE pg02.transactions ADD CONSTRAINT transaction_direction CHECK (
 (transaction_type IN ('purchase','transfer','wealth_buy') AND direction='expense') OR
 (transaction_type IN ('refund','income','wealth_redeem') AND direction='income') OR transaction_type='unknown');
ALTER TABLE pg02.transactions ADD CONSTRAINT transaction_source CHECK (
 (transaction_type='transfer' AND source_order_id IS NOT NULL AND external_source_id IS NULL
  AND classification_source='transfer_service') OR
 (transaction_type!='transfer' AND source_order_id IS NULL AND external_source_id IS NOT NULL AND length(external_source_id)>0
  AND ((transaction_type='unknown' AND classification_source='unclassified') OR
       (transaction_type!='unknown' AND classification_source='trusted_fixture'))));
ALTER TABLE pg02.transactions ADD CONSTRAINT transaction_date CHECK (
 date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' AND date::DATE::TEXT=date);
ALTER TABLE pg02.transactions ADD CONSTRAINT refund_kind CHECK (
 refund_of_transaction_id IS NULL OR transaction_type='refund');

CREATE FUNCTION pg02.validate_ledger_insert() RETURNS trigger
 LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF NEW.transaction_type!='transfer' AND current_user!='banking_migrator' THEN
  RAISE EXCEPTION 'trusted provenance required' USING ERRCODE='42501';
 END IF;
 IF NEW.transaction_type='transfer' AND NOT EXISTS (
  SELECT 1 FROM pg02.transfer_orders o WHERE o.owner_uuid=NEW.owner_uuid
  AND o.transfer_id=NEW.source_order_id AND o.from_card_id=NEW.card_id
  AND o.amount_cents=NEW.amount_cents AND o.business_date=NEW.date AND o.status='completed') THEN
  RAISE EXCEPTION 'invalid transfer source' USING ERRCODE='23514';
 END IF;
 IF NEW.merchant_key IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM pg02.merchant_aliases a WHERE a.alias=NEW.merchant AND a.merchant_key=NEW.merchant_key) THEN
  RAISE EXCEPTION 'invalid merchant key' USING ERRCODE='23514';
 END IF;
 IF NEW.refund_of_transaction_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM pg02.transactions p WHERE p.owner_uuid=NEW.owner_uuid
  AND p.transaction_id=NEW.refund_of_transaction_id AND p.transaction_type='purchase'
  AND p.date<=NEW.date AND (p.merchant=NEW.merchant OR EXISTS (
   SELECT 1 FROM pg02.merchant_aliases a JOIN pg02.merchant_aliases b ON a.merchant_key=b.merchant_key
   WHERE a.alias=p.merchant AND b.alias=NEW.merchant))) THEN
  RAISE EXCEPTION 'invalid refund link' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END; $$;
REVOKE ALL ON FUNCTION pg02.validate_ledger_insert() FROM PUBLIC;
CREATE TRIGGER ledger_insert_integrity BEFORE INSERT ON pg02.transactions
 FOR EACH ROW EXECUTE FUNCTION pg02.validate_ledger_insert();
CREATE INDEX ledger_query_owner_date ON pg02.transactions(owner_uuid,date,transaction_type,merchant_key);
