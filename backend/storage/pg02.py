"""Internal PG02 application slice. Fixed SQL, verified identity, one transaction.

The compatibility surface is deliberately small: it reuses the existing risk,
confirmation, TOTP and lifecycle kernels, rather than duplicating those rules.
No request can select this backend. No SQLite connection is made in this mode.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import re
import sqlite3
from threading import RLock
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus

from .config import PostgresConfig,pg02_enabled as enabled,PG02_SUPPORTED_TOOLS as SUPPORTED_TOOLS
from .errors import Code, StorageError, CommitUnknown
from .postgres import PostgresStore

_context = ContextVar('pg02_verified_principal',default=None)
_transaction = ContextVar('pg02_transaction',default=None)
_read_snapshot = ContextVar('pg03_read_snapshot',default=None)
fault_hook = ContextVar('pg02_controlled_fault',default=None)
_lock = RLock()
_store = None


@dataclass(frozen=True)
class VerifiedContext:
    owner_uuid: UUID
    principal_id: str
    user_id: int
    credential_id: str
    session_id: str | None


def store():
    global _store
    with _lock:
        if _store is None:
            from .readiness import check_pg02
            config=PostgresConfig.from_env()
            ready=check_pg02(config)
            if ready.schema_version < 2:
                raise StorageError(Code.NOT_READY)
            _store=PostgresStore(config).open()
        return _store


def close_store():
    global _store
    with _lock:
        if _store is not None: _store.close()
        _store=None


def lookup(function, value):
    if function not in {'lookup_login','lookup_session'}:
        raise StorageError(Code.CONTEXT)
    with store()._pool.connection() as connection:
        return connection.execute(f'SELECT pg02.{function}(%s) AS value',(value,)).fetchone()['value']


def verified_session(session_id, now):
    from security.identity import IdentityError
    row=lookup('lookup_session',session_id)
    if row is None:
        raise IdentityError('unauthorized','凭据无效')
    if (row['revoked'] or row['credential_revoked'] or row['principal_status']!='active'
            or row['user_status']!='active' or row['protocol']!='v2'
            or row['kind']!='simulated_user' or datetime.fromisoformat(row['credential_expires_at'])<=now):
        raise IdentityError('credential_expired','凭据已过期或撤销')
    return VerifiedContext(UUID(row['owner_uuid']),row['principal_id'],row['user_id'],
                           row['credential_id'],session_id),row


@contextmanager
def principal_scope(context):
    if type(context) is not VerifiedContext:
        raise StorageError(Code.CONTEXT)
    token=_context.set(context)
    try: yield
    finally: _context.reset(token)


def current_context():
    context=_context.get()
    if type(context) is not VerifiedContext:
        raise StorageError(Code.CONTEXT)
    return context


def fault(point):
    hook=fault_hook.get()
    if hook: hook(point)


class Row(dict):
    # sqlite3.Row supports both column names and ordinals; preserved internally.
    def __getitem__(self,key):
        if isinstance(key,int): return tuple(self.values())[key]
        return super().__getitem__(key)


class Cursor:
    def __init__(self,cursor):
        self.cursor=cursor
    @property
    def rowcount(self): return self.cursor.rowcount
    def fetchone(self):
        row=self.cursor.fetchone()
        return exact_row(row) if row is not None else None
    def fetchall(self): return [exact_row(r) for r in self.cursor.fetchall()]
    def __iter__(self): return iter(self.fetchall())


def exact_row(row):
    result=Row(row)
    for key,value in result.items():
        if isinstance(value,Decimal):
            if not value.is_finite() or value!=value.to_integral_value():
                raise sqlite3.DataError('nonintegral_ledger_value')
            result[key]=int(value)
    return result


POSITIONAL_COLUMNS={
 'memory_messages':'id,owner_id,conversation_id,sequence,role,content,created_at,updated_at,status,supersedes',
 'memory_task_links':'owner_id,source_id,task_id',
 'memory_preferences':'id,owner_id,kind,value,source_id,created_at,updated_at,status,supersedes',
 'lifecycle_identity':'task_id,owner_id,principal_id',
 'lifecycle_confirmation_hashes':'principal_id,phrase_hash',
 'lifecycle_rollbacks':'event_id,task_id,revision,target_step,reason,affected_json,history_json,created_at',
 'authenticator_bindings':'challenge_id,task_id,step_id,preview_id,binding_json',
 'authenticator_factors':'user_id,principal_id,encrypted_secret,enrollment_session,enrollment_expires_at,confirmed,last_used_step,created_at',
}


def translate(query):
    # Only project-owned, parameterized SQL reaches here. No request/model SQL.
    query=query.strip()
    ignored=bool(re.match(r'INSERT OR IGNORE\b',query,re.I))
    query=re.sub(r'INSERT OR IGNORE\b','INSERT',query,flags=re.I)
    positional=re.match(r'INSERT INTO (\w+)\s+VALUES',query,re.I)
    if positional:
        table=positional[1]
        if table not in POSITIONAL_COLUMNS: raise sqlite3.OperationalError('pg02_unsupported_sql')
        query=re.sub(r'^(INSERT INTO \w+)\s+VALUES',r'\1 ('+POSITIONAL_COLUMNS[table]+') VALUES',query,flags=re.I)
    if ignored: query+=' ON CONFLICT DO NOTHING'
    query=re.sub(r'ON CONFLICT\(scope,', 'ON CONFLICT(owner_uuid,scope,',query,flags=re.I)
    query=re.sub(r'ON CONFLICT\(tool_name\)', 'ON CONFLICT(owner_uuid,tool_name)',query,flags=re.I)
    query=re.sub(r'substr\(card_number,\s*-4\)','right(card_number,4)',query,flags=re.I)
    query=re.sub(r'instr\((\w+\.\w+),\s*\?\)',r'strpos(\1,?)',query,flags=re.I)
    # psycopg treats literal percent signs as placeholders when parameters exist.
    # Project-owned SQL may already be written in psycopg style (`%s`); only the
    # legacy SQLite-style `?` form needs literal `%` escaping, so a mixed/escaped
    # query is never double-escaped (that would leave `%%s` unbound).
    if '%s' in query and '?' not in query:
        return query
    query=query.replace('%','%%').replace('?','%s')
    return query


class Connection:
    """Owns exactly one pooled connection/UoW; nested policy reads borrow it."""
    def __init__(self, *, borrowed=None, read_only=False, repeatable_read=False):
        self.borrowed=borrowed
        self.closed=False
        self.token=None
        self.read_only=borrowed.read_only if borrowed else read_only
        if borrowed:
            if borrowed.context!=current_context(): raise StorageError(Code.CONTEXT)
            self.raw=borrowed.raw
            self.context=borrowed.context
            return
        self.context=current_context()
        self.manager=store()._pool.connection()
        self.raw=self.manager.__enter__()
        try:
            self.raw.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY' if read_only else
                             'BEGIN ISOLATION LEVEL REPEATABLE READ' if repeatable_read else 'BEGIN')
            self.raw.execute("SET LOCAL TIME ZONE 'Asia/Shanghai'")
            self.raw.execute("SELECT set_config('banking.owner_id',%s,true)",(str(self.context.owner_uuid),))
            self.raw.execute('SET LOCAL search_path=pg02,pg_catalog')
        except Exception:
            self.raw.rollback()
            self.manager.__exit__(None,None,None)
            raise

    def lock_owner(self):
        if self.read_only: raise sqlite3.OperationalError('readonly_snapshot')
        if self.borrowed: return
        if self.raw.info.transaction_status==TransactionStatus.IDLE:
            self.raw.execute('BEGIN')
            self.raw.execute("SET LOCAL TIME ZONE 'Asia/Shanghai'")
            self.raw.execute("SELECT set_config('banking.owner_id',%s,true)",(str(self.context.owner_uuid),))
            self.raw.execute('SET LOCAL search_path=pg02,pg_catalog')
        if self.token is None: self.token=_transaction.set(self)
        # Every PG02 write takes this lock first, including revoke and TOTP CAS.
        self.raw.execute('SELECT pg02.lock_identity(%s,%s,%s)',
                         (self.context.principal_id,self.context.credential_id,self.context.session_id))

    def assert_identity(self, context, now):
        from security.execution import AuthorizationError
        c=self.context
        if (context.principal.user_id!=c.user_id or context.principal.principal_id!=c.principal_id
                or context.principal.credential_id!=c.credential_id or context.session_id!=c.session_id):
            raise AuthorizationError('authorization_principal_mismatch','授权身份或会话不匹配')
        row=self.raw.execute('''SELECT s.revoked,s.credential_expires_at,c.revoked AS credential_revoked,
            p.status,u.status AS user_status FROM pg02.auth_sessions s JOIN pg02.credentials c ON c.credential_id=s.credential_id
            JOIN pg02.principals p ON p.principal_id=s.principal_id
            JOIN core.users u ON u.id=s.owner_uuid AND u.public_id=s.user_id
            WHERE s.session_id=%s AND s.credential_id=%s AND s.principal_id=%s AND s.user_id=%s''',
            (c.session_id,c.credential_id,c.principal_id,c.user_id)).fetchone()
        if (row is None or row['revoked'] or row['credential_revoked'] or row['status']!='active' or row['user_status']!='active'
                or datetime.fromisoformat(row['credential_expires_at'])<=now):
            raise AuthorizationError('credential_expired','凭据已过期或撤销')

    def lock_transfer(self,card_id,recipient_id):
        # Stable ordering: user -> principal -> credential -> session -> card -> account -> recipient.
        card=self.raw.execute('SELECT id,account_id FROM core.cards WHERE public_id=%s FOR UPDATE',
                              (card_id,)).fetchone()
        if card:
            self.raw.execute('SELECT id FROM core.accounts WHERE id=%s FOR UPDATE',(card['account_id'],)).fetchone()
        self.raw.execute('SELECT pg02.lock_recipient(%s)',(recipient_id,))

    def lock_task(self,task_id):
        self.raw.execute('SELECT task_id FROM pg02.lifecycle_tasks WHERE task_id=%s FOR UPDATE',(task_id,)).fetchone()

    def assert_transfer_integrity(self,context,key,amount,before,after,result):
        """Independent relational read-back before COMMIT, on this same UoW."""
        import json
        rows=self.raw.execute('''SELECT o.transfer_id,o.from_card_id,o.amount_cents,
            o.balance_before_cents,o.balance_after_cents,
            t.card_id,t.amount_cents AS ledger_amount,t.direction,t.transaction_type,
            a.consumed,a.consumed_at,a.action_digest,r.result,r.execution_digest,
            c.balance_cents,h.status AS challenge_status
            FROM pg02.transfer_orders o
            JOIN pg02.transactions t ON t.owner_uuid=o.owner_uuid AND t.source_order_id=o.transfer_id
            JOIN pg02.security_authorizations a ON a.owner_uuid=o.owner_uuid AND a.authorization_id=o.authorization_id
            JOIN pg02.lifecycle_receipts r ON r.owner_uuid=o.owner_uuid AND r.business_key=o.idempotency_key
            JOIN pg02.cards c ON c.card_id=o.from_card_id
            LEFT JOIN pg02.security_challenges h ON h.owner_uuid=a.owner_uuid AND h.challenge_id=a.challenge_id
            WHERE o.idempotency_key=%s AND o.authorization_id=%s''',(key,context.grant.authorization_id)).fetchall()
        if len(rows)!=1: raise sqlite3.IntegrityError('pg02_record_integrity_failed')
        row=rows[0]
        if (row['amount_cents']!=amount or row['ledger_amount']!=amount or row['card_id']!=row['from_card_id']
                or row['direction']!='expense' or row['transaction_type']!='transfer'
                or row['balance_before_cents']!=before or row['balance_after_cents']!=after
                or before-amount!=after or row['balance_cents']!=after
                or row['consumed']!=1 or row['consumed_at'] is None
                or row['action_digest']!=context.grant.action_digest or row['execution_digest']!=row['action_digest']
                or json.loads(row['result'])!=result
                or context.challenge_id and row['challenge_status']!='consumed'):
            raise sqlite3.IntegrityError('pg02_record_integrity_failed')
        audits=self.raw.execute("""SELECT stage FROM pg02.security_audit_records
            WHERE authorization_id=%s AND stage IN ('execution','verification') AND outcome='ok'""",
            (context.grant.authorization_id,)).fetchall()
        if {r['stage'] for r in audits}!={'execution','verification'}:
            raise sqlite3.IntegrityError('pg02_audit_integrity_failed')

    def execute(self,query,parameters=()):
        if re.fullmatch(r'PRAGMA busy_timeout = [0-9]+',query.strip(),re.I): return self
        if query.strip().upper().startswith('PRAGMA'): raise sqlite3.OperationalError('pg02_unsupported_sql')
        if query.strip().upper() in {'BEGIN IMMEDIATE','BEGIN'}:
            if self.read_only and query.strip().upper()=='BEGIN': return self
            self.lock_owner()
            return self
        if not self.read_only and not self.borrowed and self.token is None and re.match(r'(?:INSERT|UPDATE|DELETE)\b',query.strip(),re.I):
            self.lock_owner()
        try:
            return Cursor(self.raw.execute(translate(query),parameters or None))
        except psycopg.Error as error:
            import logging
            logging.getLogger('pg02').error('PG02 SQL rejected: %s %s constraint=%s',
                type(error).__name__,error.sqlstate,error.diag.constraint_name)
            raise sqlite3.OperationalError('pg02_statement_failed') from None

    def commit(self):
        if self.borrowed: raise RuntimeError('borrowed_uow_cannot_commit')
        if self.raw.info.transaction_status!=TransactionStatus.INTRANS:
            raise sqlite3.OperationalError('pg02_transaction_aborted')
        try:
            if getattr(self,'financial',False): fault('commit_before')
            self.raw.commit()
            if getattr(self,'financial',False): fault('commit_after')
        except Exception:
            raise CommitUnknown('pg02_commit_unknown') from None

    def rollback(self):
        if not self.borrowed:
            try: self.raw.rollback()
            except psycopg.Error: self.raw.close()
            if self.token is not None:
                _transaction.reset(self.token)
                self.token=None

    def close(self):
        if self.closed: return
        self.closed=True
        if self.borrowed: return
        try:
            try: self.raw.rollback()
            except psycopg.Error: self.raw.close()
        finally:
            if self.token is not None: _transaction.reset(self.token)
            try: self.manager.__exit__(None,None,None)
            except psycopg.Error: self.raw.close()

    def __enter__(self): return self
    def __exit__(self,typ,error,tb):
        if self.borrowed: return
        if typ: self.rollback()
        else: self.commit()


def get_connection(*, repeatable_read=False):
    return Connection(borrowed=_read_snapshot.get() or _transaction.get(),repeatable_read=repeatable_read)


@contextmanager
def read_snapshot():
    active=_read_snapshot.get()
    if active is not None:
        if active.context!=current_context(): raise StorageError(Code.CONTEXT)
        yield active
        return
    if _transaction.get() is not None: raise sqlite3.OperationalError('write_uow_is_not_read_snapshot')
    connection=Connection(read_only=True)
    token=_read_snapshot.set(connection)
    try: yield connection
    finally:
        _read_snapshot.reset(token)
        connection.close()


def canonical_transfer(connection, row):
    """Verify committed transfer evidence, independent of subsequent balances.

    Caller supplies an owner-scoped transaction/read snapshot. Display fields
    are execution snapshots in the durable receipt, not current recipient data.
    """
    import json
    from security.args import transfer_args
    from security.schemas import compute_action_digest
    from execution.result_models import TransferCompletedData
    from agent.output_guard import contains_sensitive_result
    from utils import cents_to_yuan

    if row is None:
        raise sqlite3.IntegrityError('transfer_order_missing')
    receipt=connection.execute('SELECT * FROM lifecycle_receipts WHERE business_key=?',
        (row['idempotency_key'],)).fetchone()
    ledger=connection.execute('SELECT * FROM transactions WHERE source_order_id=?',
        (row['transfer_id'],)).fetchall()
    auth=connection.execute('SELECT * FROM security_authorizations WHERE authorization_id=?',
        (row['authorization_id'],)).fetchone()
    if not receipt or len(ledger)!=1 or not auth:
        raise sqlite3.IntegrityError('transfer_evidence_missing')
    result=json.loads(receipt['result'])
    data=result['data']
    typed=dict(data); typed.pop('write_verification',None)
    TransferCompletedData.model_validate(typed)
    expected=dict(transfer_id=row['transfer_id'],from_card_id=row['from_card_id'],
        recipient_id=row['recipient_id'],amount=cents_to_yuan(row['amount_cents']),
        balance_before=cents_to_yuan(row['balance_before_cents']),
        balance_after=cents_to_yuan(row['balance_after_cents']),remark=row['remark'],
        risk_level=row['risk_level'],completed_at=row['completed_at'],
        idempotent_replay=False,write_verification='passed')
    digest=compute_action_digest('transfer_money',transfer_args(row['from_card_id'],row['recipient_id'],
        row['amount_cents'],row['idempotency_key'],row['remark']))
    t=ledger[0]; context=current_context()
    if (result.get('success') is not True or any(data.get(k)!=v for k,v in expected.items())
        or type(data['idempotent_replay']) is not bool
        or contains_sensitive_result(result,tool_name='transfer_money')
        or row['status']!='completed' or row['amount_cents']<=0
        or row['balance_before_cents']-row['amount_cents']!=row['balance_after_cents']
        or row['balance_after_cents']<0
        or any(r['owner_uuid']!=context.owner_uuid for r in (row,receipt,auth,t))
        or any(uid!=context.user_id for uid in (row['owner_id'],receipt['owner_id'],t['owner_id'],auth['user_id']))
        or row['principal_id']!=context.principal_id
        or auth['principal_id']!=row['principal_id'] or auth['session_id']!=row['session_id']
        or auth['action']!='transfer_money' or receipt['action']!='transfer_money'
        or auth['action_digest']!=digest or receipt['execution_digest']!=digest
        or auth['business_transfer_id'] is not None and auth['business_transfer_id']!=row['transfer_id']
        or auth['consumed']!=1 or not auth['consumed_at']
        or t['card_id']!=row['from_card_id'] or t['amount_cents']!=row['amount_cents']
        or t['direction']!='expense' or t['transaction_type']!='transfer'
        or t['classification_source']!='transfer_service' or t['date']!=row['business_date']):
        raise sqlite3.IntegrityError('transfer_evidence_mismatch')
    if auth['permission_level']=='red' or auth['strong_auth_method']=='totp' or auth['challenge_id']:
        challenge=connection.execute('SELECT * FROM security_challenges WHERE challenge_id=?',
            (auth['challenge_id'],)).fetchone()
        if (not challenge or challenge['status']!='consumed' or not challenge['consumed_at']
            or challenge['action_digest']!=digest or challenge['action']!='transfer_money'
            or challenge['owner_uuid']!=context.owner_uuid or challenge['user_id']!=context.user_id
            or challenge['principal_id']!=auth['principal_id'] or challenge['session_id']!=auth['session_id']):
            raise sqlite3.IntegrityError('transfer_challenge_mismatch')
    return result


def verify_transfer_result(result):
    try:
        with read_snapshot() as connection:
            row=connection.execute('SELECT * FROM transfer_orders WHERE transfer_id=?',
                (result.data['transfer_id'],)).fetchone()
            canonical=canonical_transfer(connection,row)
            actual=dict(result.data)
            if type(actual.get('idempotent_replay')) is not bool:
                return False,'转账回放标记无效'
            actual['idempotent_replay']=False
            return actual==canonical['data'],'已核验转账历史订单、流水、授权、挑战和持久回执'
    except Exception:
        return False,'转账历史执行证据核验失败'


def recover_transfer(key,card_id,recipient_id,amount,remark,*,replay=False):
    """Read after releasing the failed connection, using the ORIGINAL key."""
    from transfer_service import _failure,_get_completed_transfer
    connection=None
    def unknown():
        from lifecycle.service import active_scope
        scope=active_scope.get()
        if scope and scope.record:
            for op in scope.record.operations.values():
                if op.key==key: op.state='unknown'
            scope.record.status='manual'
            scope.record.reason='提交状态未知，请使用原请求键核查回执'
        return _failure('commit_unknown','无法核实提交状态，已停止自动执行；请使用原请求键查询。')
    try:
        connection=get_connection()
        # Absence is authoritative only after the original transaction releases
        # the same owner lock. An in-flight remote COMMIT cannot race this read.
        connection.execute('BEGIN IMMEDIATE')
        row=_get_completed_transfer(connection,key)
        if row is None:
            # An authoritative successful read confirms no order for this owner/key.
            return _failure('commit_not_recorded','经原请求键核实未找到提交回执，资金未提交；请重新读取任务状态。')
        if (row['from_card_id'],row['recipient_id'],row['amount_cents'],row['remark'])!=(card_id,recipient_id,amount,remark):
            return _failure('idempotency_conflict','请求键已绑定其他参数')
        result=canonical_transfer(connection,row)
        # Return the immutable canonical result exactly; task validation compares
        # it byte-for-field with the persisted receipt after a response loss.
        if replay:
            from lifecycle.service import active_scope
            if active_scope.get() is None:
                result['data']['idempotent_replay']=True
        return result
    except Exception:
        return unknown()
    finally:
        if connection: connection.close()


def readiness():
    from .readiness import check_pg02
    value=check_pg02(PostgresConfig.from_env())
    from .config import PG03_BILL_TOOLS,PG04_WEALTH_TOOLS,PG05_EXISTING_TOOLS
    supported=SUPPORTED_TOOLS
    if value.schema_version<4: supported=supported-PG04_WEALTH_TOOLS
    if value.schema_version<3: supported=supported-PG03_BILL_TOOLS
    if value.schema_version<5: supported=supported-PG05_EXISTING_TOOLS
    if value.schema_version<6: supported=supported-{'query_uploaded_bills'}
    return dict(foundation_ready=value.foundation_ready,application_ready=value.schema_version>=5,
                pg02_transfer_ready=value.schema_version>=2,backend='pg02',
                pg03_bill_ready=value.schema_version>=3,
                pg04_wealth_ready=value.schema_version>=4,
                bill_vision_ready=value.schema_version>=6,
                r2_dialogue_ready=value.schema_version>=14,
                supported_tools=sorted(supported))
