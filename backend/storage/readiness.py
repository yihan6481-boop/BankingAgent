"""Explicit foundation/schema checks. Application readiness stays false."""
from dataclasses import dataclass

import psycopg

from .errors import Code, StorageError, controlled_error
from .migrations import load_migrations
from .postgres import assert_runtime_role, connect


@dataclass(frozen=True)
class Readiness:
    foundation_ready: bool
    schema_version: int
    capabilities: tuple[str, ...]
    application_ready: bool = False


def check_foundation(config) -> Readiness:
    try:
        with connect(config) as connection:
            assert_runtime_role(connection)
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                rows = connection.execute("SELECT version,name,checksum FROM ops.schema_migrations ORDER BY version").fetchall()
                expected = load_migrations()
                if not rows or len(rows)>len(expected) or [(r["version"],r["name"],r["checksum"]) for r in rows] != [
                        (m.version,m.name,m.checksum) for m in expected[:len(rows)]]:
                    raise StorageError(Code.NOT_READY)
                relations = connection.execute("""SELECT c.relname,c.relrowsecurity,
                    pg_has_role(current_user,c.relowner,'USAGE') AS owner
                    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='core' AND c.relkind='r'""").fetchall()
                if (set(r["relname"] for r in relations) != {"users","accounts","cards"}
                        or any(not r["relrowsecurity"] or r["owner"] for r in relations)):
                    raise StorageError(Code.NOT_READY)
                policies = connection.execute("""SELECT tablename,policyname,roles,cmd,
                    qual,with_check FROM pg_policies WHERE schemaname='core'""").fetchall()
                if (len(policies) != 3 or
                        {(p['tablename'],p['policyname']) for p in policies} != {
                            ('users','users_owner'),('accounts','accounts_owner'),('cards','cards_owner')}
                        or any(p['roles'] != ['banking_runtime'] or p['cmd'] != 'ALL'
                               or p['qual'] != ('(id = core.require_owner())' if p['tablename']=='users'
                                                else '(owner_id = core.require_owner())')
                               or p['with_check'] != p['qual'] for p in policies)):
                    raise StorageError(Code.NOT_READY)
                privileges = connection.execute("""SELECT
                    has_database_privilege(current_user,current_database(),'CREATE') AS db_create,
                    has_database_privilege(current_user,current_database(),'TEMP') AS db_temp,
                    has_schema_privilege(current_user,'public','CREATE') AS public_create,
                    has_schema_privilege(current_user,'core','CREATE') AS core_create,
                    has_schema_privilege(current_user,'ops','CREATE') AS ops_create,
                    has_table_privilege(current_user,'ops.schema_migrations','INSERT,UPDATE,DELETE,TRUNCATE') AS metadata_write
                """).fetchone()
                if any(privileges.values()):
                    raise StorageError(Code.PERMISSION)
                return Readiness(True, rows[-1]['version'], ("core_read", "trusted_test_uow"))
    except psycopg.Error as error:
        raise controlled_error(error) from None


def check_pg02(config):
    """Slice readiness is separate from full-application readiness."""
    foundation=check_foundation(config)
    if foundation.schema_version<2:
        raise StorageError(Code.NOT_READY)
    scoped={'principals','credentials','auth_sessions','security_challenges','security_authorizations',
            'security_counters','breaker_states','security_audit_records','recipients','transfer_orders',
            'transactions','operation_logs','agent_task_events','authenticator_factors','lifecycle_identity',
            'lifecycle_tasks','lifecycle_confirmation_hashes','lifecycle_receipts','lifecycle_rollbacks','authenticator_bindings'}
    if foundation.schema_version>=4:
        scoped |= {'user_risk_profiles','wealth_holdings','wealth_orders','wealth_credits'}
    if foundation.schema_version>=5:
        scoped |= {'subscriptions','memory_messages','memory_task_links','memory_preferences',
                   'memory_sources','demo_events','mutation_orders'}
    if foundation.schema_version>=7:
        scoped |= {'planning_drafts'}
    if foundation.schema_version>=10:
        scoped |= {'tool_goals'}
    if foundation.schema_version>=11:
        scoped |= {'session_links'}
    if foundation.schema_version>=12:
        scoped |= {'dialogues','dialogue_turns','dialogue_calls'}
    if foundation.schema_version>=13:
        scoped |= {'dialogue_links'}
    if foundation.schema_version>=17:
        scoped |= {'daily_message_admissions'}
    if foundation.schema_version>=26:
        scoped |= {'transaction_times'}
    try:
        with connect(config) as connection:
            rows=connection.execute("""SELECT c.relname,c.relrowsecurity,
                pg_has_role(current_user,c.relowner,'USAGE') AS owner
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='pg02' AND c.relkind='r'""").fetchall()
            selected=[r for r in rows if r['relname'] in scoped]
            if {r['relname'] for r in selected}!=scoped or any(not r['relrowsecurity'] or r['owner'] for r in selected):
                raise StorageError(Code.NOT_READY)
            policies=connection.execute("SELECT tablename,roles,qual,with_check FROM pg_policies WHERE schemaname='pg02'").fetchall()
            if ({r['tablename'] for r in policies}!=scoped or any(r['roles']!=['banking_runtime']
                 or r['qual']!='(owner_uuid = core.require_owner())' or r['with_check']!=r['qual'] for r in policies)):
                raise StorageError(Code.NOT_READY)
            forbidden=connection.execute("""SELECT
                has_schema_privilege(current_user,'pg02','CREATE') AS ddl,
                has_table_privilege(current_user,'pg02.credentials','INSERT,UPDATE,DELETE') AS identity_write,
                has_table_privilege(current_user,'pg02.principals','INSERT,UPDATE,DELETE') AS principal_write,
                has_table_privilege(current_user,'pg02.lifecycle_receipts','UPDATE,DELETE,TRUNCATE') AS receipt_write,
                has_table_privilege(current_user,'pg02.transfer_orders','UPDATE,DELETE,TRUNCATE') AS order_write,
                has_table_privilege(current_user,'pg02.login_failures','SELECT,INSERT,UPDATE,DELETE') AS login_counter_direct
            """).fetchone()
            if any(forbidden.values()): raise StorageError(Code.PERMISSION)
            if foundation.schema_version>=26:
                if connection.execute("SELECT has_table_privilege(current_user,'pg02.transaction_times','INSERT,UPDATE,DELETE,TRUNCATE') bad").fetchone()['bad']:
                    raise StorageError(Code.PERMISSION)
                views=connection.execute("SELECT relname,reloptions FROM pg_class WHERE oid IN ('pg02.transaction_history'::regclass,'pg02.visible_transactions'::regclass)").fetchall()
                if len(views)!=2 or any('security_invoker=true' not in (v['reloptions'] or []) for v in views):
                    raise StorageError(Code.NOT_READY)
            if foundation.schema_version>=4:
                forbidden=connection.execute("""SELECT
                    has_table_privilege(current_user,'pg02.wealth_products','INSERT,UPDATE,DELETE,TRUNCATE') AS catalog_write,
                    has_table_privilege(current_user,'pg02.user_risk_profiles','INSERT,UPDATE,DELETE,TRUNCATE') AS risk_write,
                    has_table_privilege(current_user,'pg02.wealth_orders','UPDATE,DELETE,TRUNCATE') AS wealth_evidence_write,
                    has_table_privilege(current_user,'pg02.wealth_credits','INSERT,UPDATE,DELETE,TRUNCATE') AS generic_credit
                """).fetchone()
                if any(forbidden.values()): raise StorageError(Code.PERMISSION)
            if foundation.schema_version>=5:
                for table in ['subscriptions','memory_messages','memory_task_links','memory_preferences','memory_sources','demo_events','mutation_orders']:
                    if not connection.execute('SELECT has_table_privilege(current_user,%s,\'SELECT\') AS ok',('pg02.'+table,)).fetchone()['ok']:
                        raise StorageError(Code.PERMISSION)
                checks=connection.execute("""SELECT
                    has_column_privilege(current_user,'pg02.cards','status','UPDATE') AS card_status,
                    has_column_privilege(current_user,'pg02.cards','daily_limit_cents','UPDATE') AS card_limit,
                    has_column_privilege(current_user,'pg02.subscriptions','status','UPDATE') AS subscription_status,
                    has_table_privilege(current_user,'pg02.mutation_orders','INSERT') AS receipt_insert,
                    has_table_privilege(current_user,'pg02.memory_messages','INSERT') AS message_insert,
                    has_column_privilege(current_user,'pg02.memory_messages','content','UPDATE') AS message_edit
                """).fetchone()
                if not all(checks.values()):raise StorageError(Code.PERMISSION)
                forbidden=connection.execute("""SELECT
                    has_table_privilege(current_user,'pg02.demo_events','INSERT,UPDATE,DELETE,TRUNCATE') AS seed_write,
                    has_table_privilege(current_user,'ops.demo_datasets','SELECT,INSERT,UPDATE,DELETE,TRUNCATE') AS seed_registry,
                    has_table_privilege(current_user,'pg02.mutation_orders','UPDATE,DELETE,TRUNCATE') AS mutation_evidence_write
                """).fetchone()
                if any(forbidden.values()):raise StorageError(Code.PERMISSION)
        if foundation.schema_version>=6:
            with connect(config) as connection:
                tables=connection.execute("""SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,
                    pg_has_role(current_user,c.relowner,'USAGE') AS owner FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='billvision' AND c.relkind='r'""").fetchall()
                if {r['relname'] for r in tables}!={'documents','versions','records','audit'} or any(not r['relrowsecurity'] or not r['relforcerowsecurity'] or r['owner'] for r in tables):raise StorageError(Code.NOT_READY)
                policies=connection.execute("SELECT tablename,roles,cmd,qual,with_check FROM pg_policies WHERE schemaname='billvision'").fetchall()
                if len(policies)!=4 or any(p['roles']!=['banking_runtime'] or p['cmd']!='ALL' or p['qual']!='(owner_uuid = core.require_owner())' or p['with_check']!=p['qual'] for p in policies):raise StorageError(Code.NOT_READY)
                if connection.execute("SELECT has_schema_privilege(current_user,'billvision','CREATE') AS bad").fetchone()['bad']:raise StorageError(Code.PERMISSION)
                for table,required in [('documents','SELECT,INSERT,UPDATE,DELETE'),('records','SELECT,INSERT,UPDATE,DELETE'),('versions','SELECT,INSERT,DELETE'),('audit','SELECT,INSERT')]:
                    if not connection.execute("SELECT has_table_privilege(current_user,%s,%s) AS ok",('billvision.'+table,required)).fetchone()['ok']:raise StorageError(Code.PERMISSION)
                if connection.execute("SELECT has_table_privilege(current_user,'billvision.audit','UPDATE,DELETE,TRUNCATE') OR has_table_privilege(current_user,'billvision.versions','UPDATE,TRUNCATE') AS bad").fetchone()['bad']:raise StorageError(Code.PERMISSION)
        if foundation.schema_version>=8:
            with connect(config) as connection:
                tables=connection.execute("""SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,
                    pg_has_role(current_user,c.relowner,'USAGE') AS owner FROM pg_class c
                    JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='memory' AND c.relkind='r'""").fetchall()
                expected_memory={'entities','facts','fact_revisions','fact_sources'}
                if foundation.schema_version>=9:
                    expected_memory|={'profile_consents','profile_snapshots','profile_metrics'}
                if ({r['relname'] for r in tables}!=expected_memory
                        or any(not r['relrowsecurity'] or not r['relforcerowsecurity'] or r['owner'] for r in tables)):
                    raise StorageError(Code.NOT_READY)
                policies=connection.execute("SELECT tablename,roles,cmd,qual,with_check FROM pg_policies WHERE schemaname='memory' AND policyname='owner_scope'").fetchall()
                if (len(policies)!=4 or any(p['roles']!=['banking_runtime'] or p['cmd']!='ALL'
                    or p['qual']!='(owner_id = core.require_owner())' or p['with_check']!=p['qual'] for p in policies)):
                    raise StorageError(Code.NOT_READY)
                if connection.execute("SELECT has_schema_privilege(current_user,'memory','CREATE') AS bad").fetchone()['bad']:
                    raise StorageError(Code.PERMISSION)
                for table in ['entities','facts','fact_revisions','fact_sources']:
                    if not connection.execute("SELECT has_table_privilege(current_user,%s,'SELECT,INSERT,UPDATE') AS ok",('memory.'+table,)).fetchone()['ok']:
                        raise StorageError(Code.PERMISSION)
                    if connection.execute("SELECT has_table_privilege(current_user,%s,'DELETE,TRUNCATE') AS bad",('memory.'+table,)).fetchone()['bad']:
                        raise StorageError(Code.PERMISSION)
                if foundation.schema_version>=9:
                    names={'profile_consents','profile_snapshots','profile_metrics'}
                    policies=connection.execute("SELECT tablename,roles,cmd,qual,with_check FROM pg_policies WHERE schemaname='memory' AND policyname='owner_access'").fetchall()
                    if ({p['tablename'] for p in policies}!=names or len(policies)!=3 or
                            any(p['roles']!=['banking_runtime'] or p['cmd']!='ALL' or
                                p['qual']!='(owner_id = core.require_owner())' or p['with_check']!=p['qual'] for p in policies)):
                        raise StorageError(Code.NOT_READY)
                    for table in names:
                        if not connection.execute("SELECT has_table_privilege(current_user,%s,'SELECT,INSERT,UPDATE,DELETE') AS ok",('memory.'+table,)).fetchone()['ok']:
                            raise StorageError(Code.PERMISSION)
                        if connection.execute("SELECT has_table_privilege(current_user,%s,'TRUNCATE') AS bad",('memory.'+table,)).fetchone()['bad']:
                            raise StorageError(Code.PERMISSION)
                    hooks=connection.execute("""SELECT t.tgname,t.tgenabled,p.prosecdef FROM pg_trigger t
                        JOIN pg_proc p ON p.oid=t.tgfoid WHERE t.tgname IN
                        ('profile_ledger_change','profile_fact_change','profile_consent_revoke') AND NOT t.tgisinternal""").fetchall()
                    if ({h['tgname'] for h in hooks}!={'profile_ledger_change','profile_fact_change','profile_consent_revoke'}
                            or len(hooks)!=3 or any(h['tgenabled']!='O' or h['prosecdef'] for h in hooks)):
                        raise StorageError(Code.NOT_READY)
        return foundation
    except psycopg.Error as error: raise controlled_error(error) from None
