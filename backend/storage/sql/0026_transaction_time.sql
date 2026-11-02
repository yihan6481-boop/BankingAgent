-- Additive event metadata: immutable original ledger and date contract survive.
CREATE TABLE pg02.transaction_times (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(),transaction_id BIGINT NOT NULL,
 event_at TIMESTAMPTZ NOT NULL,
 time_source TEXT NOT NULL CHECK(time_source IN ('transfer_receipt','wealth_receipt','demo_rule_v2')),
 PRIMARY KEY(owner_uuid,transaction_id),
 FOREIGN KEY(owner_uuid,transaction_id) REFERENCES pg02.transactions(owner_uuid,transaction_id)
);
ALTER TABLE pg02.transaction_times ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.transaction_times TO banking_runtime
 USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.transaction_times TO banking_runtime;
CREATE TRIGGER time_immutable BEFORE UPDATE OR DELETE ON pg02.transaction_times
 FOR EACH ROW EXECUTE FUNCTION pg02.immutable();

-- Derive actual new times only from the validated completed order, never import time.
CREATE FUNCTION pg02.record_transaction_time() RETURNS trigger
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE stamp TEXT; provenance TEXT;
BEGIN
 IF NEW.source_order_id IS NOT NULL THEN
  SELECT completed_at INTO stamp FROM pg02.transfer_orders
   WHERE owner_uuid=NEW.owner_uuid AND transfer_id=NEW.source_order_id AND status='completed';
  provenance:='transfer_receipt';
 ELSIF NEW.source_wealth_order_id IS NOT NULL THEN
  SELECT created_at INTO stamp FROM pg02.wealth_orders
   WHERE owner_uuid=NEW.owner_uuid AND order_id=NEW.source_wealth_order_id AND status='completed';
  provenance:='wealth_receipt';
 END IF;
 IF stamp IS NOT NULL THEN
  INSERT INTO pg02.transaction_times VALUES(NEW.owner_uuid,NEW.transaction_id,stamp::timestamptz,provenance);
 END IF;
 RETURN NEW;
END; $$;
REVOKE ALL ON FUNCTION pg02.record_transaction_time() FROM PUBLIC;
CREATE TRIGGER transaction_event_time AFTER INSERT ON pg02.transactions
 FOR EACH ROW EXECUTE FUNCTION pg02.record_transaction_time();
INSERT INTO pg02.transaction_times
 SELECT t.owner_uuid,t.transaction_id,o.completed_at::timestamptz,'transfer_receipt'
 FROM pg02.transactions t JOIN pg02.transfer_orders o ON o.owner_uuid=t.owner_uuid AND o.transfer_id=t.source_order_id
 WHERE o.status='completed';
INSERT INTO pg02.transaction_times
 SELECT t.owner_uuid,t.transaction_id,o.created_at::timestamptz,'wealth_receipt'
 FROM pg02.transactions t JOIN pg02.wealth_orders o ON o.owner_uuid=t.owner_uuid AND o.order_id=t.source_wealth_order_id
 WHERE o.status='completed' ON CONFLICT DO NOTHING;
CREATE VIEW pg02.transaction_history WITH (security_invoker=true) AS
 SELECT t.*,m.event_at,m.time_source FROM pg02.transactions t
 LEFT JOIN pg02.transaction_times m USING(owner_uuid,transaction_id);
CREATE VIEW pg02.visible_transactions WITH (security_invoker=true) AS
 SELECT * FROM pg02.transaction_history
 WHERE date <= (statement_timestamp() AT TIME ZONE 'Asia/Shanghai')::date::text
 AND (event_at IS NULL OR event_at<=statement_timestamp());
GRANT SELECT ON pg02.transaction_history,pg02.visible_transactions TO banking_runtime;

-- Retain useful relationships without altering names, trust or stable recipient IDs.
ALTER TABLE payments.contacts ADD COLUMN note TEXT NOT NULL DEFAULT '' CHECK(length(note)<=100);
ALTER TABLE payments.contacts ADD COLUMN phone_source TEXT NOT NULL DEFAULT 'user';
