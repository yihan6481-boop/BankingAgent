-- Additive candidate authentication. Existing factors/audit and business rows
-- remain untouched. Challenge consumption still uses the existing fund kernel.
ALTER TABLE pg02.security_challenges DROP CONSTRAINT security_challenges_method_check;
ALTER TABLE pg02.security_challenges ADD CONSTRAINT security_challenges_method_check
 CHECK(method IN ('totp','confirmation','payment'));
CREATE SCHEMA mobile_security AUTHORIZATION banking_migrator;
REVOKE ALL ON SCHEMA mobile_security FROM PUBLIC;
GRANT USAGE ON SCHEMA mobile_security TO banking_runtime;
CREATE TABLE mobile_security.credentials(
 owner_uuid UUID PRIMARY KEY DEFAULT core.require_owner() REFERENCES core.users(id),
 phone TEXT NOT NULL CHECK(phone ~ '^1[3-9][0-9]{9}$'),
 phone_verified BOOLEAN NOT NULL DEFAULT false,
 demo BOOLEAN NOT NULL DEFAULT true CHECK(demo),
 payment_salt TEXT NOT NULL,payment_hash TEXT NOT NULL,
 gesture_salt TEXT NOT NULL,gesture_hash TEXT NOT NULL,
 generation INTEGER NOT NULL DEFAULT 1 CHECK(generation>0),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE mobile_security.bootstrap(
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 token_hash TEXT NOT NULL,expires_at TIMESTAMPTZ NOT NULL,used_at TIMESTAMPTZ,
 purpose TEXT NOT NULL CHECK(purpose IN ('enroll','reset')),
 PRIMARY KEY(owner_uuid,token_hash)
);
CREATE TABLE mobile_security.challenges(
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 challenge_id TEXT PRIMARY KEY REFERENCES pg02.security_challenges(challenge_id),
 credential_generation INTEGER NOT NULL,gesture_required BOOLEAN NOT NULL,
 sms_required BOOLEAN NOT NULL,payment_ok BOOLEAN NOT NULL DEFAULT false,
 gesture_ok BOOLEAN NOT NULL DEFAULT false,sms_ok BOOLEAN NOT NULL DEFAULT false,device_hash TEXT,
 otp_hash TEXT,otp_version INTEGER NOT NULL DEFAULT 0,
 sent_at TIMESTAMPTZ,send_status TEXT CHECK(send_status IN ('sent','failed'))
);
CREATE TABLE mobile_security.limits(
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 scope TEXT NOT NULL,scope_hash TEXT NOT NULL,kind TEXT NOT NULL,
 window_start TIMESTAMPTZ NOT NULL,count INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(owner_uuid,scope,scope_hash,kind)
);
CREATE TABLE mobile_security.audit(
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 owner_uuid UUID NOT NULL DEFAULT core.require_owner() REFERENCES core.users(id),
 challenge_id TEXT,operation TEXT NOT NULL,outcome TEXT NOT NULL,
 reason TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE mobile_security.support_requests(
 owner_uuid UUID NOT NULL DEFAULT core.require_owner(),session_id TEXT NOT NULL,
 paused BOOLEAN NOT NULL DEFAULT true,updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(owner_uuid,session_id),FOREIGN KEY(owner_uuid,session_id) REFERENCES pg02.auth_sessions(owner_uuid,session_id)
);
DO $$ DECLARE t TEXT; BEGIN
 FOREACH t IN ARRAY ARRAY['credentials','bootstrap','challenges','limits','audit','support_requests'] LOOP
  EXECUTE format('ALTER TABLE mobile_security.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE mobile_security.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY owner_scope ON mobile_security.%I TO banking_runtime USING(owner_uuid=core.require_owner()) WITH CHECK(owner_uuid=core.require_owner())',t);
  EXECUTE format('GRANT SELECT,INSERT,UPDATE ON mobile_security.%I TO banking_runtime',t);
 END LOOP;
END $$;
GRANT USAGE ON SEQUENCE mobile_security.audit_id_seq TO banking_runtime;
-- Global source limiting exposes no counter rows to the application role.
CREATE TABLE mobile_security.source_limits(source_hash TEXT NOT NULL CHECK(source_hash ~ '^[a-f0-9]{64}$'),kind TEXT NOT NULL,window_start TIMESTAMPTZ NOT NULL,count INTEGER NOT NULL,PRIMARY KEY(source_hash,kind));
REVOKE ALL ON mobile_security.source_limits FROM PUBLIC;
CREATE FUNCTION mobile_security.source_rate(p_hash TEXT,p_kind TEXT,p_increment BOOLEAN,p_now TIMESTAMPTZ)
 RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE v_max INTEGER;v_count INTEGER;v_start TIMESTAMPTZ;
BEGIN
 PERFORM core.require_owner();
 IF p_hash !~ '^[a-f0-9]{64}$' OR p_kind NOT IN ('failure','send') THEN RAISE EXCEPTION 'invalid_rate_scope'; END IF;
 v_max:=CASE WHEN p_kind='failure' THEN 20 ELSE 15 END;
 PERFORM pg_advisory_xact_lock(hashtextextended('sms-source:'||p_hash,0));
 SELECT count,window_start INTO v_count,v_start FROM mobile_security.source_limits WHERE source_hash=p_hash AND kind=p_kind FOR UPDATE;
 IF v_start IS NULL OR v_start+interval '10 minutes'<=p_now THEN v_count:=0;v_start:=p_now;END IF;
 IF v_count>=v_max THEN RETURN false; END IF;
 IF p_increment THEN
  INSERT INTO mobile_security.source_limits VALUES(p_hash,p_kind,v_start,v_count+1)
  ON CONFLICT(source_hash,kind) DO UPDATE SET count=excluded.count,window_start=excluded.window_start;
 END IF;
 RETURN true;
END $$;
REVOKE ALL ON FUNCTION mobile_security.source_rate(TEXT,TEXT,BOOLEAN,TIMESTAMPTZ) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mobile_security.source_rate(TEXT,TEXT,BOOLEAN,TIMESTAMPTZ) TO banking_runtime;

-- Preserve all existing canonical wealth assertions; add matching payment proof.
CREATE OR REPLACE FUNCTION pg02.validate_wealth_commit() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg02.transactions t JOIN pg02.lifecycle_receipts r ON r.owner_uuid=t.owner_uuid
  JOIN pg02.security_authorizations a ON a.owner_uuid=r.owner_uuid AND a.authorization_id=NEW.authorization_id
  WHERE t.owner_uuid=NEW.owner_uuid AND t.source_wealth_order_id=NEW.order_id AND r.business_key=NEW.idempotency_key
  AND a.consumed=1 AND a.principal_id=NEW.principal_id AND a.session_id=NEW.session_id AND a.action=r.action
  AND a.action_digest=r.execution_digest AND r.action=CASE WHEN NEW.order_type='BUY' THEN 'buy_wealth_product' ELSE 'redeem_wealth_product' END
  AND (NEW.order_type!='BUY' OR (a.permission_level='red' AND a.strong_auth_method IN ('totp','payment') AND EXISTS(
    SELECT 1 FROM pg02.security_challenges h WHERE h.owner_uuid=a.owner_uuid AND h.challenge_id=a.challenge_id
    AND h.status='consumed' AND h.method=a.strong_auth_method AND h.action_digest=a.action_digest)))
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
