-- Foundation only: deliberately no seed, credentials, transfer, or ledger.
CREATE SCHEMA core AUTHORIZATION banking_migrator;
REVOKE ALL ON SCHEMA core FROM PUBLIC;

CREATE TABLE core.users (
    id UUID PRIMARY KEY,
    public_id INTEGER NOT NULL UNIQUE CHECK (public_id >= 0),
    display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 128),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
    business_timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'
      CHECK (business_timezone = 'Asia/Shanghai'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE core.accounts (
    id UUID PRIMARY KEY,
    public_id INTEGER NOT NULL UNIQUE CHECK (public_id >= 0),
    owner_id UUID NOT NULL REFERENCES core.users(id),
    currency TEXT NOT NULL DEFAULT 'CNY' CHECK (currency = 'CNY'),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','frozen','closed')),
    posted_minor BIGINT NOT NULL DEFAULT 0,
    held_minor BIGINT NOT NULL DEFAULT 0,
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id,id),
    CHECK (posted_minor >= held_minor AND held_minor >= 0)
);
CREATE INDEX accounts_owner ON core.accounts(owner_id);
CREATE TABLE core.cards (
    id UUID PRIMARY KEY,
    public_id INTEGER NOT NULL UNIQUE CHECK (public_id >= 0),
    owner_id UUID NOT NULL REFERENCES core.users(id),
    account_id UUID NOT NULL,
    masked_number TEXT NOT NULL CHECK (masked_number ~ '^\*{4} [0-9]{4}$'),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','lost','frozen')),
    daily_limit_minor BIGINT NOT NULL CHECK (daily_limit_minor > 0),
    per_tx_limit_minor BIGINT NOT NULL CHECK (per_tx_limit_minor > 0
      AND per_tx_limit_minor <= daily_limit_minor),
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id,id),
    FOREIGN KEY (owner_id,account_id) REFERENCES core.accounts(owner_id,id)
);
CREATE INDEX cards_owner_account ON core.cards(owner_id,account_id);

CREATE FUNCTION core.require_owner() RETURNS UUID
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path = pg_catalog AS $$
DECLARE owner_text TEXT;
BEGIN
    owner_text := current_setting('banking.owner_id', true);
    IF owner_text IS NULL OR owner_text = '' THEN
        RAISE EXCEPTION 'trusted_context_required' USING ERRCODE = '42501';
    END IF;
    RETURN owner_text::uuid;
END;
$$;
REVOKE ALL ON FUNCTION core.require_owner() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.require_owner() TO banking_runtime;

ALTER TABLE core.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.cards ENABLE ROW LEVEL SECURITY;
-- Migrator owns tables for fixture/bootstrap. Runtime never owns/inherits it.
CREATE POLICY users_owner ON core.users TO banking_runtime
  USING (id=core.require_owner()) WITH CHECK (id=core.require_owner());
CREATE POLICY accounts_owner ON core.accounts TO banking_runtime
  USING (owner_id=core.require_owner()) WITH CHECK (owner_id=core.require_owner());
CREATE POLICY cards_owner ON core.cards TO banking_runtime
  USING (owner_id=core.require_owner()) WITH CHECK (owner_id=core.require_owner());
GRANT USAGE ON SCHEMA core TO banking_runtime;
GRANT SELECT ON core.users TO banking_runtime;
-- Internal fixture UoW only in PG-01; no public mutation/authentication route.
GRANT SELECT, INSERT, UPDATE ON core.accounts,core.cards TO banking_runtime;
