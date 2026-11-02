-- Existing application completion. Old migration bytes stay frozen.
CREATE TABLE pg02.subscriptions (
 subscription_id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL,
 name TEXT NOT NULL, amount_cents BIGINT NOT NULL CHECK(amount_cents>0),
 cycle TEXT NOT NULL CHECK(cycle IN ('monthly','yearly')), next_charge_date TEXT NOT NULL,
 card_id INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','cancelled')),
 source_key TEXT NOT NULL, source_version TEXT NOT NULL,
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(),
 UNIQUE(owner_uuid,subscription_id), UNIQUE(owner_uuid,source_key),
 FOREIGN KEY(owner_id,owner_uuid) REFERENCES core.users(public_id,id),
 FOREIGN KEY(owner_uuid,card_id) REFERENCES core.cards(owner_id,public_id)
);
CREATE TABLE pg02.memory_messages (
 id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, conversation_id TEXT NOT NULL,
 sequence INTEGER NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','assistant')),
 content TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('active','replaced','deleted')), supersedes TEXT,
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(),
 UNIQUE(owner_uuid,id), UNIQUE(owner_id,conversation_id,sequence),
 FOREIGN KEY(owner_id,owner_uuid) REFERENCES core.users(public_id,id),
 FOREIGN KEY(owner_uuid,supersedes) REFERENCES pg02.memory_messages(owner_uuid,id)
);
CREATE TABLE pg02.memory_task_links (
 owner_id INTEGER NOT NULL, source_id TEXT NOT NULL, task_id TEXT NOT NULL,
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(), PRIMARY KEY(owner_id,source_id,task_id),
 FOREIGN KEY(owner_id,owner_uuid) REFERENCES core.users(public_id,id),
 FOREIGN KEY(owner_uuid,source_id) REFERENCES pg02.memory_messages(owner_uuid,id),
 FOREIGN KEY(owner_uuid,task_id) REFERENCES pg02.lifecycle_identity(owner_uuid,task_id)
);
CREATE TABLE pg02.memory_preferences (
 id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
 source_id TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('active','replaced','deleted')), supersedes TEXT,
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(), UNIQUE(owner_uuid,id), UNIQUE(owner_id,kind,source_id),
 FOREIGN KEY(owner_id,owner_uuid) REFERENCES core.users(public_id,id),
 FOREIGN KEY(owner_uuid,source_id) REFERENCES pg02.memory_messages(owner_uuid,id),
 FOREIGN KEY(owner_uuid,supersedes) REFERENCES pg02.memory_preferences(owner_uuid,id)
);
CREATE UNIQUE INDEX memory_active_preference ON pg02.memory_preferences(owner_id,kind) WHERE status='active';
CREATE INDEX memory_owner_time ON pg02.memory_messages(owner_uuid,status,created_at);
-- Provenance contains no second copy of retractable content.
CREATE TABLE pg02.memory_sources (
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(), message_id TEXT NOT NULL,
 source_key TEXT NOT NULL, source_kind TEXT NOT NULL, source_version TEXT NOT NULL,
 PRIMARY KEY(owner_uuid,source_key),
 FOREIGN KEY(owner_uuid,message_id) REFERENCES pg02.memory_messages(owner_uuid,id)
);
-- Administrative import registry is not accessible to the application role.
CREATE TABLE ops.demo_datasets (
 dataset_key TEXT PRIMARY KEY, content_hash TEXT NOT NULL, generator_version TEXT NOT NULL,
 manifest JSONB NOT NULL, imported_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE pg02.demo_events (
 event_key TEXT PRIMARY KEY, dataset_key TEXT NOT NULL REFERENCES ops.demo_datasets(dataset_key),
 owner_uuid UUID NOT NULL, card_id INTEGER NOT NULL, transaction_id BIGINT,
 event_kind TEXT NOT NULL, amount_cents BIGINT NOT NULL CHECK(amount_cents>=0),
 business_date TEXT NOT NULL, source_version TEXT NOT NULL, detail JSONB NOT NULL,
 UNIQUE(owner_uuid,event_key),
 FOREIGN KEY(owner_uuid,card_id) REFERENCES core.cards(owner_id,public_id),
 FOREIGN KEY(owner_uuid,transaction_id) REFERENCES pg02.transactions(owner_uuid,transaction_id)
);
CREATE TABLE pg02.mutation_orders (
 operation_id TEXT PRIMARY KEY, business_key TEXT NOT NULL, action TEXT NOT NULL,
 authorization_id TEXT NOT NULL, execution_digest TEXT NOT NULL, result TEXT NOT NULL,
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(), UNIQUE(owner_uuid,business_key),
 UNIQUE(owner_uuid,authorization_id),
 FOREIGN KEY(owner_uuid,authorization_id) REFERENCES pg02.security_authorizations(owner_uuid,authorization_id)
);
CREATE TRIGGER mutation_immutable BEFORE UPDATE OR DELETE ON pg02.mutation_orders FOR EACH ROW EXECUTE FUNCTION pg02.immutable();
ALTER TABLE pg02.lifecycle_receipts DROP CONSTRAINT lifecycle_receipts_action_check;
ALTER TABLE pg02.lifecycle_receipts ADD CHECK(action IN ('transfer_money','buy_wealth_product','redeem_wealth_product',
 'report_card_lost','set_daily_limit','cancel_subscription'));
ALTER TABLE pg02.lifecycle_receipts ADD COLUMN mutation_business_key TEXT GENERATED ALWAYS AS
 (CASE WHEN action IN ('report_card_lost','set_daily_limit','cancel_subscription') THEN business_key END) STORED;
ALTER TABLE pg02.lifecycle_receipts ADD FOREIGN KEY(owner_uuid,mutation_business_key)
 REFERENCES pg02.mutation_orders(owner_uuid,business_key);
CREATE FUNCTION pg02.validate_mutation_commit() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg02.lifecycle_receipts r JOIN pg02.security_authorizations a
 ON a.owner_uuid=r.owner_uuid AND a.authorization_id=NEW.authorization_id
 WHERE r.owner_uuid=NEW.owner_uuid AND r.operation_id=NEW.operation_id AND r.business_key=NEW.business_key
 AND r.action=NEW.action AND r.result=NEW.result AND r.execution_digest=NEW.execution_digest
 AND a.action=NEW.action AND a.action_digest=NEW.execution_digest AND a.consumed=1 AND a.consumed_at IS NOT NULL)
 THEN RAISE EXCEPTION 'mutation receipt missing' USING ERRCODE='23514'; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg02.security_audit_records WHERE owner_uuid=NEW.owner_uuid
 AND authorization_id=NEW.authorization_id AND stage='verification' AND outcome='ok')
 THEN RAISE EXCEPTION 'mutation audit missing' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END; $$;
REVOKE ALL ON FUNCTION pg02.validate_mutation_commit() FROM PUBLIC;
CREATE CONSTRAINT TRIGGER mutation_commit_integrity AFTER INSERT ON pg02.mutation_orders
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION pg02.validate_mutation_commit();

-- Only the original debit column or the existing card metadata can be changed.
-- Granting status/limit never grants generic balance credits.
CREATE OR REPLACE FUNCTION pg02.debit_card() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
BEGIN
 IF NEW.card_id IS DISTINCT FROM OLD.card_id OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
 OR NEW.account_id IS DISTINCT FROM OLD.account_id OR NEW.card_uuid IS DISTINCT FROM OLD.card_uuid
 OR NEW.bank_name IS DISTINCT FROM OLD.bank_name OR NEW.card_number IS DISTINCT FROM OLD.card_number
 OR NEW.per_tx_limit_cents IS DISTINCT FROM OLD.per_tx_limit_cents
 OR NEW.overseas_enabled IS DISTINCT FROM OLD.overseas_enabled
 OR NEW.online_payment_enabled IS DISTINCT FROM OLD.online_payment_enabled
 THEN RAISE EXCEPTION 'invalid card fields'; END IF;
 IF NEW.status IS DISTINCT FROM OLD.status OR NEW.daily_limit_cents IS DISTINCT FROM OLD.daily_limit_cents THEN
  IF NEW.balance_cents IS DISTINCT FROM OLD.balance_cents OR OLD.status!='normal'
   OR NEW.status NOT IN ('normal','lost') OR NEW.daily_limit_cents<=0
  THEN RAISE EXCEPTION 'invalid card mutation'; END IF;
  UPDATE core.cards SET status=CASE WHEN NEW.status='normal' THEN 'active' ELSE NEW.status END,
   daily_limit_minor=NEW.daily_limit_cents,per_tx_limit_minor=least(per_tx_limit_minor,NEW.daily_limit_cents),version=version+1
  WHERE id=OLD.card_uuid AND owner_id=core.require_owner();
 ELSE
  IF NEW.balance_cents>OLD.balance_cents OR NEW.balance_cents<0 THEN RAISE EXCEPTION 'invalid_debit'; END IF;
  UPDATE core.accounts SET posted_minor=posted_minor-(OLD.balance_cents-NEW.balance_cents),version=version+1
  WHERE id=OLD.account_id AND owner_id=core.require_owner() AND status='active'
   AND posted_minor-held_minor>=OLD.balance_cents-NEW.balance_cents;
 END IF;
 IF NOT FOUND THEN RAISE EXCEPTION 'conditional_card_update_failed'; END IF;
 RETURN NEW;
END; $$;
GRANT UPDATE(status,daily_limit_cents) ON pg02.cards TO banking_runtime;
GRANT UPDATE(status,daily_limit_minor,per_tx_limit_minor,version) ON core.cards TO banking_runtime;

ALTER TABLE pg02.subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE pg02.memory_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE pg02.memory_task_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE pg02.memory_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE pg02.memory_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE pg02.demo_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE pg02.mutation_orders ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_scope ON pg02.subscriptions TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
CREATE POLICY owner_scope ON pg02.memory_messages TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
CREATE POLICY owner_scope ON pg02.memory_task_links TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
CREATE POLICY owner_scope ON pg02.memory_preferences TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
CREATE POLICY owner_scope ON pg02.memory_sources TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
CREATE POLICY owner_scope ON pg02.demo_events TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
CREATE POLICY owner_scope ON pg02.mutation_orders TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner());
GRANT SELECT ON pg02.subscriptions,pg02.memory_messages,pg02.memory_task_links,pg02.memory_preferences,
 pg02.memory_sources,pg02.demo_events,pg02.mutation_orders TO banking_runtime;
GRANT UPDATE(status) ON pg02.subscriptions TO banking_runtime;
GRANT INSERT ON pg02.memory_messages,pg02.memory_task_links,pg02.memory_preferences,pg02.mutation_orders TO banking_runtime;
GRANT UPDATE(content,status,updated_at) ON pg02.memory_messages TO banking_runtime;
GRANT UPDATE(value,status,updated_at) ON pg02.memory_preferences TO banking_runtime;
GRANT DELETE ON pg02.memory_task_links TO banking_runtime;
GRANT UPDATE(message_id) ON pg02.memory_sources TO banking_runtime;

-- Author-only historical import: no fake user confirmations or TOTP grants.
ALTER TABLE pg02.transactions DROP CONSTRAINT transaction_source;
ALTER TABLE pg02.transactions ADD CONSTRAINT transaction_source CHECK (
 (transaction_type='transfer' AND source_order_id IS NOT NULL AND source_wealth_order_id IS NULL AND external_source_id IS NULL AND classification_source='transfer_service') OR
 (transaction_type IN ('wealth_buy','wealth_redeem') AND source_order_id IS NULL AND source_wealth_order_id IS NOT NULL AND external_source_id IS NULL AND classification_source='wealth_service') OR
 (transaction_type!='transfer' AND source_order_id IS NULL AND source_wealth_order_id IS NULL AND external_source_id IS NOT NULL AND length(external_source_id)>0 AND
  ((transaction_type='unknown' AND classification_source='unclassified') OR (transaction_type!='unknown' AND classification_source='trusted_fixture'))) OR
 (source_order_id IS NULL AND source_wealth_order_id IS NULL AND external_source_id IS NOT NULL AND classification_source='demo_seed_v1'));
CREATE OR REPLACE FUNCTION pg02.validate_ledger_insert() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF NEW.classification_source='demo_seed_v1' THEN
  IF current_user!='banking_migrator' OR NOT EXISTS(SELECT 1 FROM pg02.demo_events e
   WHERE e.owner_uuid=NEW.owner_uuid AND e.event_key=NEW.external_source_id AND e.card_id=NEW.card_id
    AND e.amount_cents=NEW.amount_cents AND e.business_date=NEW.date AND e.event_kind=NEW.transaction_type)
  THEN RAISE EXCEPTION 'controlled seed provenance required' USING ERRCODE='42501'; END IF;
 ELSIF NEW.source_wealth_order_id IS NOT NULL THEN
  IF NOT EXISTS(SELECT 1 FROM pg02.wealth_orders o JOIN pg02.wealth_products p ON p.product_id=o.product_id
   WHERE o.owner_uuid=NEW.owner_uuid AND o.order_id=NEW.source_wealth_order_id AND NEW.merchant=p.name
   AND o.card_id=NEW.card_id AND o.amount_cents=NEW.amount_cents AND o.business_date=NEW.date AND o.status='completed'
   AND ((o.order_type='BUY' AND NEW.transaction_type='wealth_buy' AND NEW.direction='expense') OR
        (o.order_type='REDEEM' AND NEW.transaction_type='wealth_redeem' AND NEW.direction='income'))
   AND NEW.classification_source='wealth_service') THEN RAISE EXCEPTION 'invalid wealth source' USING ERRCODE='23514'; END IF;
 ELSIF NEW.transaction_type!='transfer' AND current_user!='banking_migrator' THEN
  RAISE EXCEPTION 'trusted provenance required' USING ERRCODE='42501';
 END IF;
 IF NEW.transaction_type='transfer' AND NEW.classification_source!='demo_seed_v1' AND NOT EXISTS(SELECT 1 FROM pg02.transfer_orders o WHERE o.owner_uuid=NEW.owner_uuid
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

-- No context-free enumeration of personal tasks. The worker first gets only
-- due task identities, then opens an owner scope derived from durable rows.
CREATE FUNCTION pg02.due_owners(at_time TEXT) RETURNS TABLE(owner_uuid UUID,principal_id TEXT,user_id INTEGER,credential_id TEXT)
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT DISTINCT i.owner_uuid,i.principal_id,i.owner_id,c.credential_id
 FROM pg02.lifecycle_tasks t JOIN pg02.lifecycle_identity i ON i.task_id=t.task_id AND i.owner_uuid=t.owner_uuid
 JOIN pg02.principals p ON p.owner_uuid=i.owner_uuid AND p.principal_id=i.principal_id
 JOIN core.users u ON u.id=i.owner_uuid AND u.public_id=i.owner_id
 JOIN pg02.credentials c ON c.owner_uuid=i.owner_uuid AND c.principal_id=i.principal_id
 WHERE t.status IN ('scheduled','active') AND t.due_at<=at_time AND (t.lease_until IS NULL OR t.lease_until<=at_time)
 AND p.status='active' AND u.status='active' AND c.revoked=0;
$$;
REVOKE ALL ON FUNCTION pg02.due_owners(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION pg02.due_owners(TEXT) TO banking_runtime;
