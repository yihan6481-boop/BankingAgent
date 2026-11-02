-- PG04 additive principal-only simulated wealth; no business seed.
CREATE TABLE pg02.wealth_products (
 product_id INTEGER PRIMARY KEY CHECK(product_id>0), name TEXT NOT NULL,
 risk_level TEXT NOT NULL CHECK(risk_level IN ('R1','R2','R3','R4','R5')),
 min_purchase_cents BIGINT NOT NULL CHECK(min_purchase_cents>0),
 status TEXT NOT NULL CHECK(status IN ('active','inactive')),
 reference_annual_rate_bps INTEGER NOT NULL CHECK(reference_annual_rate_bps>=0),
 duration_days INTEGER NOT NULL CHECK(duration_days>=0)
);
CREATE TABLE pg02.user_risk_profiles (
 user_id INTEGER PRIMARY KEY, risk_level TEXT NOT NULL CHECK(risk_level IN ('R1','R2','R3','R4','R5')),
 updated_at TEXT NOT NULL, owner_uuid UUID NOT NULL DEFAULT core.require_owner(),
 FOREIGN KEY(user_id,owner_uuid) REFERENCES core.users(public_id,id)
);
CREATE TABLE pg02.wealth_holdings (
 holding_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 user_id INTEGER NOT NULL, product_id INTEGER NOT NULL REFERENCES pg02.wealth_products(product_id),
 card_id INTEGER NOT NULL, principal_cents BIGINT NOT NULL CHECK(principal_cents>=0),
 status TEXT NOT NULL CHECK(status IN ('active','redeemed')),
 purchased_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 owner_id INTEGER GENERATED ALWAYS AS (user_id) STORED,
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(),
 FOREIGN KEY(user_id,owner_uuid) REFERENCES core.users(public_id,id),
 FOREIGN KEY(owner_uuid,card_id) REFERENCES core.cards(owner_id,public_id),
 UNIQUE(user_id,product_id,card_id), UNIQUE(owner_uuid,holding_id,product_id,card_id),
 CHECK((principal_cents=0 AND status='redeemed') OR (principal_cents>0 AND status='active'))
);
CREATE TABLE pg02.wealth_orders (
 order_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL, order_type TEXT NOT NULL CHECK(order_type IN ('BUY','REDEEM')),
 user_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, card_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
 holding_id INTEGER NOT NULL, amount_cents BIGINT NOT NULL CHECK(amount_cents>0),
 principal_before_cents BIGINT NOT NULL CHECK(principal_before_cents>=0),
 principal_after_cents BIGINT NOT NULL CHECK(principal_after_cents>=0),
 card_balance_after_cents BIGINT NOT NULL CHECK(card_balance_after_cents>=0),
 posted_before_cents BIGINT NOT NULL, posted_after_cents BIGINT NOT NULL,
 held_cents BIGINT NOT NULL CHECK(held_cents>=0),
 status TEXT NOT NULL CHECK(status='completed'), created_at TEXT NOT NULL,
 business_date TEXT NOT NULL CHECK(business_date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' AND business_date::DATE::TEXT=business_date),
 principal_id TEXT NOT NULL, session_id TEXT NOT NULL, authorization_id TEXT NOT NULL,
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(),
 UNIQUE(owner_uuid,idempotency_key), UNIQUE(owner_uuid,order_id), UNIQUE(owner_uuid,authorization_id),
 FOREIGN KEY(user_id,owner_uuid) REFERENCES core.users(public_id,id), CHECK(owner_id=user_id),
 FOREIGN KEY(owner_uuid,holding_id,product_id,card_id) REFERENCES pg02.wealth_holdings(owner_uuid,holding_id,product_id,card_id),
 FOREIGN KEY(owner_uuid,principal_id) REFERENCES pg02.principals(owner_uuid,principal_id),
 FOREIGN KEY(owner_uuid,session_id) REFERENCES pg02.auth_sessions(owner_uuid,session_id),
 FOREIGN KEY(owner_uuid,authorization_id) REFERENCES pg02.security_authorizations(owner_uuid,authorization_id),
 CHECK(posted_before_cents>=held_cents AND posted_after_cents>=held_cents AND card_balance_after_cents=posted_after_cents-held_cents),
 CHECK((order_type='BUY' AND principal_before_cents+amount_cents=principal_after_cents AND posted_before_cents-amount_cents=posted_after_cents)
    OR (order_type='REDEEM' AND principal_before_cents-amount_cents=principal_after_cents AND posted_before_cents+amount_cents=posted_after_cents))
);
CREATE TRIGGER wealth_order_immutable BEFORE UPDATE OR DELETE ON pg02.wealth_orders FOR EACH ROW EXECUTE FUNCTION pg02.immutable();

ALTER TABLE pg02.transactions ADD COLUMN source_wealth_order_id TEXT;
ALTER TABLE pg02.transactions ADD UNIQUE(owner_uuid,source_wealth_order_id);
ALTER TABLE pg02.transactions ADD FOREIGN KEY(owner_uuid,source_wealth_order_id) REFERENCES pg02.wealth_orders(owner_uuid,order_id);
ALTER TABLE pg02.transactions DROP CONSTRAINT transaction_source;
ALTER TABLE pg02.transactions ADD CONSTRAINT transaction_source CHECK (
 (transaction_type='transfer' AND source_order_id IS NOT NULL AND source_wealth_order_id IS NULL AND external_source_id IS NULL AND classification_source='transfer_service') OR
 (transaction_type IN ('wealth_buy','wealth_redeem') AND source_order_id IS NULL AND source_wealth_order_id IS NOT NULL AND external_source_id IS NULL AND classification_source='wealth_service') OR
 (transaction_type!='transfer' AND source_order_id IS NULL AND source_wealth_order_id IS NULL AND external_source_id IS NOT NULL AND length(external_source_id)>0 AND
  ((transaction_type='unknown' AND classification_source='unclassified') OR (transaction_type!='unknown' AND classification_source='trusted_fixture'))));

-- Keep the old transfer FK; each receipt now has one explicitly typed FK.
ALTER TABLE pg02.lifecycle_receipts DROP CONSTRAINT lifecycle_receipts_action_check;
ALTER TABLE pg02.lifecycle_receipts ADD CHECK(action IN ('transfer_money','buy_wealth_product','redeem_wealth_product'));
ALTER TABLE pg02.lifecycle_receipts DROP CONSTRAINT lifecycle_receipts_owner_uuid_business_key_fkey;
ALTER TABLE pg02.lifecycle_receipts ADD COLUMN transfer_business_key TEXT GENERATED ALWAYS AS (CASE WHEN action='transfer_money' THEN business_key END) STORED;
ALTER TABLE pg02.lifecycle_receipts ADD COLUMN wealth_business_key TEXT GENERATED ALWAYS AS (CASE WHEN action IN ('buy_wealth_product','redeem_wealth_product') THEN business_key END) STORED;
ALTER TABLE pg02.lifecycle_receipts ADD FOREIGN KEY(owner_uuid,transfer_business_key) REFERENCES pg02.transfer_orders(owner_uuid,idempotency_key);
ALTER TABLE pg02.lifecycle_receipts ADD FOREIGN KEY(owner_uuid,wealth_business_key) REFERENCES pg02.wealth_orders(owner_uuid,idempotency_key);

CREATE OR REPLACE FUNCTION pg02.validate_ledger_insert() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF NEW.source_wealth_order_id IS NOT NULL THEN
  IF NOT EXISTS(SELECT 1 FROM pg02.wealth_orders o JOIN pg02.wealth_products p ON p.product_id=o.product_id
   WHERE o.owner_uuid=NEW.owner_uuid AND o.order_id=NEW.source_wealth_order_id AND NEW.merchant=p.name
   AND o.card_id=NEW.card_id AND o.amount_cents=NEW.amount_cents AND o.business_date=NEW.date AND o.status='completed'
   AND ((o.order_type='BUY' AND NEW.transaction_type='wealth_buy' AND NEW.direction='expense') OR
        (o.order_type='REDEEM' AND NEW.transaction_type='wealth_redeem' AND NEW.direction='income'))
   AND NEW.classification_source='wealth_service') THEN RAISE EXCEPTION 'invalid wealth source' USING ERRCODE='23514'; END IF;
 ELSIF NEW.transaction_type!='transfer' AND current_user!='banking_migrator' THEN
  RAISE EXCEPTION 'trusted provenance required' USING ERRCODE='42501';
 END IF;
 IF NEW.transaction_type='transfer' AND NOT EXISTS(SELECT 1 FROM pg02.transfer_orders o WHERE o.owner_uuid=NEW.owner_uuid
  AND o.transfer_id=NEW.source_order_id AND o.from_card_id=NEW.card_id AND o.amount_cents=NEW.amount_cents
  AND o.business_date=NEW.date AND o.status='completed') THEN RAISE EXCEPTION 'invalid transfer source' USING ERRCODE='23514'; END IF;
 IF NEW.merchant_key IS NOT NULL AND NOT EXISTS(SELECT 1 FROM pg02.merchant_aliases a WHERE a.alias=NEW.merchant AND a.merchant_key=NEW.merchant_key)
  THEN RAISE EXCEPTION 'invalid merchant key' USING ERRCODE='23514'; END IF;
 IF NEW.refund_of_transaction_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM pg02.transactions p WHERE p.owner_uuid=NEW.owner_uuid
  AND p.transaction_id=NEW.refund_of_transaction_id AND p.transaction_type='purchase' AND p.date<=NEW.date
  AND (p.merchant=NEW.merchant OR EXISTS(SELECT 1 FROM pg02.merchant_aliases a JOIN pg02.merchant_aliases b ON a.merchant_key=b.merchant_key
   WHERE a.alias=p.merchant AND b.alias=NEW.merchant))) THEN RAISE EXCEPTION 'invalid refund link' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;

-- One credit per completed redemption, never a generic view credit.
CREATE TABLE pg02.wealth_credits(owner_uuid UUID NOT NULL, order_id TEXT NOT NULL, PRIMARY KEY(owner_uuid,order_id),
 FOREIGN KEY(owner_uuid,order_id) REFERENCES pg02.wealth_orders(owner_uuid,order_id));
CREATE FUNCTION pg02.credit_wealth_redemption(key TEXT) RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE o pg02.wealth_orders; account UUID;
BEGIN
 SELECT * INTO STRICT o FROM pg02.wealth_orders WHERE owner_uuid=core.require_owner() AND order_id=key AND order_type='REDEEM';
 IF NOT EXISTS(SELECT 1 FROM pg02.security_authorizations a WHERE a.owner_uuid=o.owner_uuid AND a.authorization_id=o.authorization_id
  AND a.action='redeem_wealth_product' AND a.consumed=1 AND a.session_id=o.session_id AND a.principal_id=o.principal_id) THEN
  RAISE EXCEPTION 'invalid redemption authorization'; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg02.wealth_holdings h WHERE h.owner_uuid=o.owner_uuid AND h.holding_id=o.holding_id
  AND h.product_id=o.product_id AND h.card_id=o.card_id AND h.principal_cents=o.principal_after_cents
  AND h.status=CASE WHEN o.principal_after_cents=0 THEN 'redeemed' ELSE 'active' END) THEN
  RAISE EXCEPTION 'invalid redemption principal snapshot'; END IF;
 SELECT c.account_id INTO STRICT account FROM core.cards c WHERE c.owner_id=o.owner_uuid AND c.public_id=o.card_id AND c.status='active';
 INSERT INTO pg02.wealth_credits VALUES(o.owner_uuid,o.order_id);
 UPDATE core.accounts SET posted_minor=posted_minor+o.amount_cents,version=version+1 WHERE id=account AND owner_id=o.owner_uuid
  AND status='active' AND posted_minor=o.posted_before_cents AND held_minor=o.held_cents;
 IF NOT FOUND THEN RAISE EXCEPTION 'conditional_redemption_credit_failed'; END IF;
END; $$;
REVOKE ALL ON FUNCTION pg02.credit_wealth_redemption(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION pg02.credit_wealth_redemption(TEXT) TO banking_runtime;
CREATE TRIGGER credit_immutable BEFORE UPDATE OR DELETE ON pg02.wealth_credits FOR EACH ROW EXECUTE FUNCTION pg02.immutable();

CREATE FUNCTION pg02.validate_wealth_commit() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg02.transactions t JOIN pg02.lifecycle_receipts r ON r.owner_uuid=t.owner_uuid
  JOIN pg02.security_authorizations a ON a.owner_uuid=r.owner_uuid AND a.authorization_id=NEW.authorization_id
  WHERE t.owner_uuid=NEW.owner_uuid AND t.source_wealth_order_id=NEW.order_id AND r.business_key=NEW.idempotency_key
  AND a.consumed=1 AND a.principal_id=NEW.principal_id AND a.session_id=NEW.session_id AND a.action=r.action
  AND a.action_digest=r.execution_digest AND r.action=CASE WHEN NEW.order_type='BUY' THEN 'buy_wealth_product' ELSE 'redeem_wealth_product' END
  AND (NEW.order_type!='BUY' OR (a.permission_level='red' AND a.strong_auth_method='totp' AND EXISTS(
    SELECT 1 FROM pg02.security_challenges h WHERE h.owner_uuid=a.owner_uuid AND h.challenge_id=a.challenge_id
    AND h.status='consumed' AND h.method='totp' AND h.action_digest=a.action_digest)))
  AND r.result::jsonb->'data'->>'order_id'=NEW.order_id
  AND r.result::jsonb->'data'->>'order_type'=NEW.order_type
  AND (r.result::jsonb->'data'->>'product_id')::INTEGER=NEW.product_id
  AND (r.result::jsonb->'data'->>'holding_id')::INTEGER=NEW.holding_id
  AND (r.result::jsonb->'data'->>'card_id')::INTEGER=NEW.card_id
  AND (r.result::jsonb->'data'->>'amount')::NUMERIC*100=NEW.amount_cents
  AND (r.result::jsonb->'data'->>'principal_after')::NUMERIC*100=NEW.principal_after_cents
  AND (r.result::jsonb->'data'->>'card_balance_after')::NUMERIC*100=NEW.card_balance_after_cents)
  THEN RAISE EXCEPTION 'incomplete wealth evidence' USING ERRCODE='23514'; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg02.wealth_holdings h JOIN core.cards c ON c.owner_id=h.owner_uuid AND c.public_id=h.card_id
  JOIN core.accounts a ON a.owner_id=c.owner_id AND a.id=c.account_id
  WHERE h.owner_uuid=NEW.owner_uuid AND h.holding_id=NEW.holding_id AND h.product_id=NEW.product_id AND h.card_id=NEW.card_id
  AND h.principal_cents=NEW.principal_after_cents AND a.posted_minor=NEW.posted_after_cents AND a.held_minor=NEW.held_cents)
  THEN RAISE EXCEPTION 'wealth commit snapshot mismatch' USING ERRCODE='23514'; END IF;
 IF NEW.order_type='REDEEM' AND NOT EXISTS(SELECT 1 FROM pg02.wealth_credits WHERE owner_uuid=NEW.owner_uuid AND order_id=NEW.order_id)
  THEN RAISE EXCEPTION 'missing redemption credit' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END; $$;
REVOKE ALL ON FUNCTION pg02.validate_wealth_commit() FROM PUBLIC;
CREATE CONSTRAINT TRIGGER wealth_commit_integrity AFTER INSERT ON pg02.wealth_orders DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION pg02.validate_wealth_commit();

GRANT SELECT ON pg02.wealth_products TO banking_runtime;
CREATE FUNCTION pg02.lock_wealth_resources(product INTEGER) RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 PERFORM 1 FROM pg02.wealth_products WHERE product_id=product FOR SHARE;
 PERFORM 1 FROM pg02.user_risk_profiles WHERE owner_uuid=core.require_owner() FOR SHARE;
END; $$;
REVOKE ALL ON FUNCTION pg02.lock_wealth_resources(INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION pg02.lock_wealth_resources(INTEGER) TO banking_runtime;
ALTER TABLE pg02.user_risk_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE pg02.wealth_holdings ENABLE ROW LEVEL SECURITY;
ALTER TABLE pg02.wealth_orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE pg02.wealth_credits ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.user_risk_profiles TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
CREATE POLICY owner_scope ON pg02.wealth_holdings TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
CREATE POLICY owner_scope ON pg02.wealth_orders TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
CREATE POLICY owner_scope ON pg02.wealth_credits TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.user_risk_profiles,pg02.wealth_holdings,pg02.wealth_orders,pg02.wealth_credits TO banking_runtime;
GRANT INSERT ON pg02.wealth_holdings,pg02.wealth_orders TO banking_runtime;
GRANT UPDATE(principal_cents,status,updated_at) ON pg02.wealth_holdings TO banking_runtime;
GRANT USAGE ON SEQUENCE pg02.wealth_holdings_holding_id_seq TO banking_runtime;
