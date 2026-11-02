-- Local PostgreSQL 17 role/database bootstrap for the five-person demo.
-- Run by tools/pg17_local_bootstrap.ps1 as the postgres superuser against the
-- "postgres" database. Passwords arrive only through psql variables supplied by
-- that launcher; this file and the project never contain credentials.
--
-- Mirrors deploy/postgres/init-roles.sh for the role model and adds the two
-- application databases. Re-running is idempotent and never drops anything.
\set ON_ERROR_STOP on

-- Restricted roles: no superuser, no CREATEDB/CREATEROLE, no BYPASSRLS.
SELECT format('CREATE ROLE banking_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L', :'migration_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'banking_migrator')\gexec
ALTER ROLE banking_migrator WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'migration_password';

SELECT format('CREATE ROLE banking_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %L', :'runtime_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'banking_runtime')\gexec
ALTER ROLE banking_runtime WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD :'runtime_password';

-- banking_demo  : development database restored from the verified Docker backup
-- banking_test  : independent database for write/regression runs
-- banking_foundation : administrative anchor expected by existing test tooling
SELECT format('CREATE DATABASE %I OWNER postgres TEMPLATE template0', v.d)
FROM (VALUES ('banking_demo'), ('banking_test'), ('banking_foundation')) AS v(d)
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = v.d)\gexec

-- Database-level privileges match the retired Docker cluster exactly.
REVOKE ALL ON DATABASE banking_demo FROM PUBLIC;
GRANT CONNECT, CREATE ON DATABASE banking_demo TO banking_migrator;
GRANT CONNECT ON DATABASE banking_demo TO banking_runtime;

REVOKE ALL ON DATABASE banking_test FROM PUBLIC;
GRANT CONNECT, CREATE ON DATABASE banking_test TO banking_migrator;
GRANT CONNECT ON DATABASE banking_test TO banking_runtime;

REVOKE ALL ON DATABASE banking_foundation FROM PUBLIC;
GRANT CONNECT, CREATE ON DATABASE banking_foundation TO banking_migrator;
GRANT CONNECT ON DATABASE banking_foundation TO banking_runtime;

\echo roles_and_databases_ready
