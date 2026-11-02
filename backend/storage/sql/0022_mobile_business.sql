-- Additional simulated product categories and settlement evidence. No existing
-- account, credential, transaction, holding or catalog row is updated/deleted.
ALTER TABLE pg02.wealth_products ADD COLUMN category TEXT NOT NULL DEFAULT 'wealth'
 CHECK(category IN ('wealth','deposit','fund','insurance'));
ALTER TABLE pg02.wealth_products ADD COLUMN terms JSONB NOT NULL DEFAULT '{}'::jsonb;
CREATE TABLE mobile_security.product_contracts(
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(),order_id TEXT PRIMARY KEY,
 category TEXT NOT NULL,product_id INTEGER NOT NULL,amount_cents BIGINT NOT NULL,
 units NUMERIC(20,4),terms JSONB NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 FOREIGN KEY(owner_uuid,order_id) REFERENCES pg02.wealth_orders(owner_uuid,order_id)
);
CREATE FUNCTION mobile_security.record_product_contract() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE p pg02.wealth_products;
BEGIN
 SELECT * INTO p FROM pg02.wealth_products WHERE product_id=NEW.product_id;
 IF p.category='insurance' THEN
  IF NEW.order_type!='BUY' OR NEW.amount_cents<>p.min_purchase_cents THEN RAISE EXCEPTION 'insurance_premium_or_redemption_invalid'; END IF;
 END IF;
 INSERT INTO mobile_security.product_contracts(owner_uuid,order_id,category,product_id,amount_cents,units,terms)
 VALUES(NEW.owner_uuid,NEW.order_id,p.category,p.product_id,NEW.amount_cents,
  CASE WHEN p.category='fund' THEN NEW.amount_cents::numeric/100 ELSE NULL END,
  p.terms||jsonb_build_object('order_type',NEW.order_type,'duration_days',p.duration_days,'reference_annual_rate_bps',CASE WHEN p.category IN ('wealth','deposit') THEN p.reference_annual_rate_bps ELSE NULL END));
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION mobile_security.record_product_contract() FROM PUBLIC;
CREATE TRIGGER mobile_product_contract AFTER INSERT ON pg02.wealth_orders FOR EACH ROW EXECUTE FUNCTION mobile_security.record_product_contract();
CREATE SEQUENCE mobile_security.external_recipient_ids START WITH 1000000;
GRANT USAGE ON SEQUENCE mobile_security.external_recipient_ids TO banking_runtime;
CREATE TABLE mobile_security.external_accounts(
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(),recipient_id INTEGER NOT NULL,
 identity_verified BOOLEAN NOT NULL DEFAULT false CHECK(NOT identity_verified),
 registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),PRIMARY KEY(owner_uuid,recipient_id),
 FOREIGN KEY(owner_uuid,recipient_id) REFERENCES pg02.recipients(owner_uuid,recipient_id)
);
CREATE TABLE mobile_security.demo_ledger(
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(),source_key TEXT NOT NULL,
 card_id INTEGER NOT NULL,day DATE NOT NULL,direction TEXT NOT NULL CHECK(direction IN ('income','expense')),
 amount_cents BIGINT NOT NULL CHECK(amount_cents>0),label TEXT NOT NULL,
 closing_snapshot_cents BIGINT NOT NULL,future_scenario BOOLEAN NOT NULL,
 PRIMARY KEY(owner_uuid,source_key),FOREIGN KEY(owner_uuid,card_id) REFERENCES core.cards(owner_id,public_id)
);
DO $$ DECLARE t TEXT; BEGIN
 FOREACH t IN ARRAY ARRAY['product_contracts','external_accounts','demo_ledger'] LOOP
  EXECUTE format('ALTER TABLE mobile_security.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE mobile_security.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY owner_scope ON mobile_security.%I TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner())',t);
 END LOOP;
END $$;
GRANT SELECT,INSERT ON mobile_security.product_contracts,mobile_security.external_accounts TO banking_runtime;
GRANT SELECT ON mobile_security.demo_ledger TO banking_runtime;
