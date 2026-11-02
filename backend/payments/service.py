"""Owner-scoped persistence, contacts and unified recipient resolution.

All SQL here is fixed, parameterized, owner-scoped by ``core.require_owner()``
RLS plus an explicit per-owner advisory lock for write batches. Account numbers
are never returned in a response body: responses carry a masked tail and a
server-side fingerprint only.
"""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .errors import PaymentError
from .models import Extraction, mask_phone, normalize_phone

RESOLUTION_TTL = timedelta(minutes=30)
CONTACT_MAX = 100


def Jsonb(value):
    from psycopg.types.json import Jsonb as PGJsonb
    return PGJsonb(value)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha256_json(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc)


def iso(value):
    return value.isoformat() if value is not None else None


# ---------------------------------------------------------------------------
# Transaction / identity plumbing.
# ---------------------------------------------------------------------------

def require_migration(connection):
    if connection.execute("SELECT to_regclass('payments.payment_documents') AS t").fetchone()['t'] is None:
        raise PaymentError('payment_migration_required',
            '缴费单据功能需要管理员增量迁移0018；未降级为其他存储后端', 503)


def check_connection(connection, context, write):
    if write:
        connection.execute('SELECT pg02.lock_identity(%s,%s,%s)',
            (context.principal_id, context.credential_id, context.session_id))
    row = connection.execute('''SELECT s.credential_expires_at,s.revoked,
        c.revoked AS credential_revoked,p.status AS principal_status,u.status AS user_status
        FROM pg02.auth_sessions s
        JOIN pg02.credentials c ON c.owner_uuid=s.owner_uuid AND c.credential_id=s.credential_id
        JOIN pg02.principals p ON p.owner_uuid=s.owner_uuid AND p.principal_id=s.principal_id
        JOIN core.users u ON u.id=s.owner_uuid
        WHERE s.session_id=%s AND s.principal_id=%s AND s.credential_id=%s AND s.protocol='v2' ''',
        (context.session_id, context.principal_id, context.credential_id)).fetchone()
    from security.service import get_security_services
    current = get_security_services().identity._now()
    if (not row or row['revoked'] or row['credential_revoked']
            or row['principal_status'] != 'active' or row['user_status'] != 'active'
            or datetime.fromisoformat(row['credential_expires_at']) <= current):
        raise PaymentError('credential_expired', '会话已失效或撤销；未读取/修改缴费数据', 401)


@contextmanager
def transaction(principal, write=False):
    from storage.config import pg02_enabled
    if not pg02_enabled():
        raise PaymentError('payment_pg_required', '缴费单据功能仅支持PostgreSQL', 409)
    from storage.pg02 import current_context, read_snapshot, store, fault
    context = current_context()
    if principal is None or (principal.principal_id, principal.user_id) != (context.principal_id, context.user_id):
        raise PaymentError('payment_owner_invalid', '无权限', 403)
    if not write:
        with read_snapshot() as database:
            require_migration(database.raw)
            check_connection(database.raw, context, False)
            yield database.raw
        return
    with store()._pool.connection() as connection, connection.transaction():
        connection.execute("SELECT set_config('banking.owner_id',%s,true)", (str(context.owner_uuid),))
        # One short owner-wide write lock keeps request-key retries, obligation
        # dedupe and document revisions serialized for this owner across
        # processes. No provider HTTP happens inside this transaction.
        connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                           ('payments:' + str(context.owner_uuid),))
        require_migration(connection)
        check_connection(connection, context, True)
        yield connection
        fault('payment_before_commit')


# ---------------------------------------------------------------------------
# Request-key idempotency (owner + operation + request content).
# ---------------------------------------------------------------------------

def begin_request(connection, operation, client_request_id, payload):
    """Return ``(replay_row_or_None)``; same key/different params conflicts."""
    request_hash = sha256_json(payload)
    row = connection.execute('''SELECT * FROM payments.client_requests
        WHERE operation=%s AND client_request_id=%s''',
        (operation, client_request_id)).fetchone()
    if row is not None:
        if row['request_hash'] != request_hash:
            raise PaymentError('client_request_conflict',
                '同一请求键已绑定不同参数；未重复执行，请使用新的请求键', 409)
        return row
    connection.execute('''INSERT INTO payments.client_requests
        (operation,client_request_id,request_hash) VALUES(%s,%s,%s)''',
        (operation, client_request_id, request_hash))
    return None


def finish_request(connection, operation, client_request_id, resource_id=None, result=None):
    connection.execute('''UPDATE payments.client_requests SET resource_id=%s,result=%s
        WHERE operation=%s AND client_request_id=%s''',
        (resource_id, Jsonb(result) if result is not None else None, operation, client_request_id))


def audit(connection, operation, *, document_id=None, revision=None, detail=None):
    connection.execute('''INSERT INTO payments.audit(audit_id,operation,document_id,revision,detail)
        VALUES(%s,%s,%s,%s,%s)''', (uuid4(), operation, document_id, revision, Jsonb(detail or {})))


# ---------------------------------------------------------------------------
# Simulated recipient directory helpers.
# ---------------------------------------------------------------------------

def mask_account(number):
    if not number:
        return None
    compact = str(number)
    if len(compact) <= 4:
        return '*' * len(compact)
    return '*' * (len(compact) - 4) + compact[-4:]


def recipient_fingerprint(row):
    return sha256_json([
        row['recipient_id'], row['name'], row['bank_name'], row['account_number'],
        int(row['trusted']), row['status'], row.get('beneficiary_kind', 'person'),
    ])


def directory_recipient(connection, recipient_id):
    return connection.execute('SELECT * FROM pg02.recipients WHERE recipient_id=%s',
                              (recipient_id,)).fetchone()


def directory_recipients(connection):
    return connection.execute('SELECT * FROM pg02.recipients ORDER BY recipient_id LIMIT 500').fetchall()


def candidate_from_recipient(row):
    from security.payment_auth import simulated_external
    trusted = int(row['trusted']) == 1 and row['status'] == 'active'
    return dict(
        candidate_id='recipient:%d' % row['recipient_id'],
        recipient_id=row['recipient_id'],
        account_name=row['name'],
        bank_name=row['bank_name'],
        masked_account=mask_account(row['account_number']),
        beneficiary_kind=row.get('beneficiary_kind', 'person'),
        trusted=trusted,
        supports=['payment_preparation'] if trusted or simulated_external(row['recipient_id']) else [],
        reasons=[] if trusted else ['simulated_directory_recipient_not_trusted'],
        evidence=[dict(source='simulated_recipient_directory',
                       reference='pg02.recipients:%d' % row['recipient_id'],
                       note='模拟收款目录匹配，不是现实银行账户归属认证')],
        fingerprint=recipient_fingerprint(row),
    )


def public_candidate(candidate):
    return {key: value for key, value in candidate.items() if key != 'fingerprint'}


def normalize_account_number(value):
    return re.sub(r'[\s-]', '', str(value or ''))


def _match_accounts(rows, account_number, account_name):
    account_number = normalize_account_number(account_number)
    name = (account_name or '').strip()
    if not account_number:
        return []
    return [row for row in rows
            if normalize_account_number(row['account_number']) == account_number
            and (not name or row['name'].strip() == name)]


# ---------------------------------------------------------------------------
# Contacts.
# ---------------------------------------------------------------------------

def contact_public(connection, row):
    recipient = directory_recipient(connection, row['recipient_id'])
    return dict(
        contact_id=row['contact_id'],
        recipient_id=row['recipient_id'],
        display_name=row['display_name'], note=row.get('note',''),phone_source=row.get('phone_source','user'),
        phone_masked=mask_phone(row['phone']),
        phone_bound=row['phone'] is not None,
        revision=row['revision'],
        status=row['status'],
        created_at=iso(row['created_at']), updated_at=iso(row['updated_at']),
        directory=None if recipient is None else dict(
            recipient_id=recipient['recipient_id'], name=recipient['name'],
            bank_name=recipient['bank_name'], masked_account=mask_account(recipient['account_number']),
            beneficiary_kind=recipient.get('beneficiary_kind', 'person'),
            trusted=int(recipient['trusted']) == 1, active=recipient['status'] == 'active'),
    )


def list_contacts(principal):
    with transaction(principal) as connection:
        profiles = {row['recipient_id']: row for row in connection.execute(
            'SELECT * FROM payments.contacts ORDER BY recipient_id LIMIT %s', (CONTACT_MAX,)).fetchall()}
        entries = []
        for recipient in directory_recipients(connection):
            profile = profiles.get(recipient['recipient_id'])
            if profile is not None:
                entries.append(contact_public(connection, profile))
            else:
                entries.append(dict(
                    contact_id=None, recipient_id=recipient['recipient_id'],
                    display_name=recipient['name'], phone_masked=None, phone_bound=False,
                    revision=None, status='directory_only', created_at=None, updated_at=None,
                    directory=dict(recipient_id=recipient['recipient_id'], name=recipient['name'],
                        bank_name=recipient['bank_name'], masked_account=mask_account(recipient['account_number']),
                        beneficiary_kind=recipient.get('beneficiary_kind', 'person'),
                        trusted=int(recipient['trusted']) == 1, active=recipient['status'] == 'active')))
        return dict(contacts=entries, truncated=len(entries) > CONTACT_MAX)


def get_contact(principal, contact_id):
    with transaction(principal) as connection:
        return _contact(connection, contact_id)


def _contact(connection, contact_id, *, lock=False):
    row = connection.execute('SELECT * FROM payments.contacts WHERE contact_id=%s'
                             + (' FOR UPDATE' if lock else ''), (contact_id,)).fetchone()
    if row is None:
        raise PaymentError('contact_not_found', '未找到本人联系人', 404)
    return row


def _phone_value(raw):
    if raw is None:
        return None
    phone = normalize_phone(raw)
    if phone is None:
        raise PaymentError('phone_invalid', '手机号须为中国大陆11位手机号；不支持据手机号推算账户', 422)
    return phone


def create_contact(principal, body):
    with transaction(principal, True) as connection:
        payload = body.model_dump(mode='json')
        replay = begin_request(connection, 'contact.create', body.client_request_id, payload)
        if replay is not None:
            contact_id = replay['resource_id']
            row = _contact(connection, contact_id)
            return dict(contact=contact_public(connection, row), replayed=True)
        recipient = directory_recipient(connection, body.recipient_id)
        if recipient is None or recipient['status'] != 'active':
            raise PaymentError('recipient_not_found', '收款目录中不存在该有效收款人', 404)
        existing = connection.execute('SELECT contact_id FROM payments.contacts WHERE recipient_id=%s',
                                      (body.recipient_id,)).fetchone()
        if existing is not None:
            raise PaymentError('contact_exists', '该收款人已有联系人档案；请直接更新', 409,
                               data=dict(contact_id=existing['contact_id']))
        phone = _phone_value(body.phone)
        if phone is not None:
            bound = connection.execute("""SELECT p.contact_id FROM payments.contacts p JOIN pg02.recipients r
                ON r.owner_uuid=p.owner_uuid AND r.recipient_id=p.recipient_id
                WHERE p.phone=%s AND p.status='active' AND r.name<>%s""",(phone,recipient['name'])).fetchone()
            if bound is not None:
                raise PaymentError('phone_already_bound', '该手机号已绑定其他联系人；未创建', 409)
        display_name = (body.display_name or recipient['name']).strip()
        contact_id = 'ct_' + uuid4().hex
        connection.execute('''INSERT INTO payments.contacts
            (contact_id,recipient_id,display_name,phone,phone_bound_at)
            VALUES(%s,%s,%s,%s,CASE WHEN %s::text IS NULL THEN NULL ELSE now() END)''',
            (contact_id, body.recipient_id, display_name, phone, phone))
        audit(connection, 'contact.create', detail=dict(contact_id=contact_id,
              recipient_id=body.recipient_id, phone_bound=phone is not None))
        row = _contact(connection, contact_id)
        result = dict(contact=contact_public(connection, row), replayed=False)
        finish_request(connection, 'contact.create', body.client_request_id, contact_id, result)
        return result


def update_contact(principal, contact_id, body):
    with transaction(principal, True) as connection:
        payload = body.model_dump(mode='json')
        begin_request(connection, 'contact.update', body.client_request_id, dict(payload, contact_id=contact_id))
        row = _contact(connection, contact_id, lock=True)
        if row['revision'] != body.revision:
            raise PaymentError('stale_revision',
                '联系人版本已变化（当前 revision=%d），请重新读取' % row['revision'], 409)
        display_name = row['display_name'] if body.display_name is None else body.display_name.strip()
        archive = body.archive or row['status'] == 'archived'
        if archive:
            phone = None
        elif 'phone' in body.model_fields_set:
            phone = _phone_value(body.phone)
        else:
            phone = row['phone']
        if phone is not None:
            bound = connection.execute("""SELECT p.contact_id FROM payments.contacts p JOIN pg02.recipients r
                ON r.owner_uuid=p.owner_uuid AND r.recipient_id=p.recipient_id
                WHERE p.phone=%s AND p.status='active' AND p.contact_id<>%s
                AND r.name<>(SELECT name FROM pg02.recipients WHERE recipient_id=%s)""", (phone, contact_id,row['recipient_id'])).fetchone()
            if bound is not None:
                raise PaymentError('phone_already_bound', '该手机号已绑定其他联系人；未更新', 409)
        connection.execute('''UPDATE payments.contacts SET display_name=%s,phone=%s,
            phone_bound_at=CASE WHEN %s::text IS NULL THEN NULL ELSE COALESCE(phone_bound_at,now()) END,
            status=%s,revision=revision+1,updated_at=now()
            WHERE contact_id=%s AND revision=%s''',
            (display_name, phone, phone, 'archived' if archive else 'active', contact_id, body.revision))
        audit(connection, 'contact.update', detail=dict(contact_id=contact_id, revision=body.revision + 1,
              archived=archive))
        row = _contact(connection, contact_id)
        result = dict(contact=contact_public(connection, row), replayed=False)
        finish_request(connection, 'contact.update', body.client_request_id, contact_id, result)
        return result


# ---------------------------------------------------------------------------
# Unified recipient resolution.
# ---------------------------------------------------------------------------

def _resolution_public(row, *, revalidate=None):
    candidates = row['candidates'] or []
    return dict(
        resolution_id=row['resolution_id'], revision=row['revision'],
        selector_kind=row['selector_kind'], selector=row['selector'],
        status=row['status'],
        candidates=[public_candidate(candidate) for candidate in candidates],
        selected_candidate_id=row['selected_candidate_id'],
        expires_at=iso(row['expires_at']), created_at=iso(row['created_at']),
        limitations=list((revalidate or {}).get('limitations', [])),
    )


def _lookup_selector(connection, selector):
    kind = selector.kind
    if kind == 'contact':
        recipient = directory_recipient(connection, selector.recipient_id)
        if recipient is None:
            return dict(status='not_found', candidates=[], notes=['收款目录中不存在该收款人'])
        candidate = candidate_from_recipient(recipient)
        return dict(status='resolved' if recipient['status'] == 'active' else 'needs_information',
                    candidates=[candidate], notes=[] if recipient['status'] == 'active'
                    else ['收款人当前不可用'])
    if kind == 'phone':
        phone = _phone_value(selector.phone)
        contacts = connection.execute("""SELECT * FROM payments.contacts
            WHERE phone=%s AND status='active' ORDER BY recipient_id""", (phone,)).fetchall()
        if not contacts:
            return dict(status='not_found', candidates=[],
                        notes=['该手机号没有可信映射；手机号不能推算账户，需先在联系人中绑定'])
        candidates=[candidate_from_recipient(r) for contact in contacts
          if (r:=directory_recipient(connection,contact['recipient_id'])) and r['status']=='active']
        if not candidates:
            return dict(status='needs_information', candidates=[],
                        notes=['手机号映射的收款人已不可用，请重新核对联系人'])
        return dict(status='resolved' if len(candidates)==1 else 'ambiguous', candidates=candidates,
                    notes=[] if len(candidates)==1 else ['同一手机号有多个收款账户，请核对并选择'])
    if kind == 'bank_account':
        rows = directory_recipients(connection)
        matches = _match_accounts(rows, selector.account_number, selector.account_name)
        candidates = [candidate_from_recipient(row) for row in matches]
        if not candidates:
            return dict(status='not_found', candidates=[],
                        notes=['模拟收款目录没有匹配账户；不支持向目录外账户自动准备付款',
                               '手工账户不会被标记为可信，也不会据手机号推算账户'])
        if len(candidates) > 1:
            return dict(status='ambiguous', candidates=candidates,
                        notes=['存在多个匹配候选，必须由用户明确选择'])
        return dict(status='resolved', candidates=candidates, notes=[])
    if kind == 'document_payee':
        row = connection.execute('SELECT * FROM payments.payment_documents WHERE document_id=%s',
                                 (selector.document_id,)).fetchone()
        if row is None:
            raise PaymentError('payment_document_not_found', '未找到本人缴费单据', 404)
        if row['revision'] != selector.revision:
            raise PaymentError('document_revision_conflict',
                '单据版本已变化（当前 revision=%d）；请重新读取并核对' % row['revision'], 409)
        extraction = Extraction.model_validate(row['fields'] or row['extraction'] or {})
        if selector.payee_index >= len(extraction.payee_accounts):
            return dict(status='needs_information', candidates=[],
                        notes=['单据没有该序号的收款账户'])
        account = extraction.payee_accounts[selector.payee_index]
        if account.account_number.value is None:
            return dict(status='needs_information', candidates=[],
                        notes=['单据收款账户不完整（二维码/缴费编号类单据），不能准备转账'])
        rows = directory_recipients(connection)
        matches = _match_accounts(rows, account.account_number.value, account.account_name.value)
        candidates = [candidate_from_recipient(row) for row in matches]
        if not candidates:
            return dict(status='not_found', candidates=[],
                        notes=['单据收款账户不在模拟收款目录；不支持对未知账户自动准备付款'])
        if len(candidates) > 1:
            return dict(status='ambiguous', candidates=candidates,
                        notes=['存在多个匹配候选，必须由用户明确选择'])
        return dict(status='resolved', candidates=candidates, notes=[])
    raise PaymentError('invalid_selector', '不支持的收款解析类型', 422)


def create_resolution(principal, body):
    with transaction(principal, True) as connection:
        selector = body.selector.model_dump(mode='json')
        payload = dict(selector=selector)
        replay = begin_request(connection, 'resolution.create', body.client_request_id, payload)
        if replay is not None:
            row = connection.execute('SELECT * FROM payments.recipient_resolutions WHERE resolution_id=%s',
                                     (replay['resource_id'],)).fetchone()
            if row is not None:
                return dict(resolution=_resolution_public(row), replayed=True)
        lookup = _lookup_selector(connection, body.selector)
        safe_selector = dict(selector)
        if body.selector.kind == 'phone':
            safe_selector = dict(selector, phone=mask_phone(_phone_value(body.selector.phone)))
        elif body.selector.kind == 'bank_account':
            safe_selector = dict(selector, account_number=mask_account(
                normalize_account_number(body.selector.account_number)))
        resolution_id = 'rr_' + uuid4().hex
        expires_at = now() + RESOLUTION_TTL
        connection.execute('''INSERT INTO payments.recipient_resolutions
            (resolution_id,client_request_id,selector_kind,selector,selector_fingerprint,
             status,candidates,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
            (resolution_id, body.client_request_id, body.selector.kind, Jsonb(safe_selector),
             sha256_json(selector), lookup['status'], Jsonb(lookup['candidates']), expires_at))
        audit(connection, 'resolution.create', detail=dict(resolution_id=resolution_id,
              selector_kind=body.selector.kind, status=lookup['status']))
        row = connection.execute('SELECT * FROM payments.recipient_resolutions WHERE resolution_id=%s',
                                 (resolution_id,)).fetchone()
        result = dict(resolution=_resolution_public(row, revalidate=dict(limitations=lookup['notes'])),
                      replayed=False)
        finish_request(connection, 'resolution.create', body.client_request_id, resolution_id, result)
        return result


def get_resolution(principal, resolution_id):
    with transaction(principal) as connection:
        row = connection.execute('SELECT * FROM payments.recipient_resolutions WHERE resolution_id=%s',
                                 (resolution_id,)).fetchone()
        if row is None:
            raise PaymentError('resolution_not_found', '未找到本人收款解析记录', 404)
        validation = revalidate_resolution(connection, row)
        return dict(resolution=_resolution_public(row, revalidate=validation['summary']))


def select_candidate(principal, resolution_id, body):
    with transaction(principal, True) as connection:
        begin_request(connection, 'resolution.select', body.client_request_id,
                      dict(resolution_id=resolution_id, **body.model_dump(mode='json')))
        row = connection.execute('''SELECT * FROM payments.recipient_resolutions
            WHERE resolution_id=%s FOR UPDATE''', (resolution_id,)).fetchone()
        if row is None:
            raise PaymentError('resolution_not_found', '未找到本人收款解析记录', 404)
        if row['revision'] != body.revision:
            raise PaymentError('stale_revision',
                '收款解析版本已变化（当前 revision=%d），请重新读取' % row['revision'], 409)
        if row['expires_at'] <= now():
            raise PaymentError('resolution_expired', '收款解析已过期，请重新解析', 409)
        candidate = next((item for item in row['candidates']
                          if item['candidate_id'] == body.candidate_id), None)
        if candidate is None:
            raise PaymentError('candidate_not_found', '候选不属于当前收款解析，未保存选择', 422)
        connection.execute('''UPDATE payments.recipient_resolutions
            SET selected_candidate_id=%s,status='resolved',revision=revision+1,updated_at=now()
            WHERE resolution_id=%s AND revision=%s''',
            (body.candidate_id, resolution_id, body.revision))
        audit(connection, 'resolution.select', detail=dict(resolution_id=resolution_id,
              candidate_id=body.candidate_id))
        row = connection.execute('SELECT * FROM payments.recipient_resolutions WHERE resolution_id=%s',
                                 (resolution_id,)).fetchone()
        result = dict(resolution=_resolution_public(row), replayed=False)
        finish_request(connection, 'resolution.select', body.client_request_id, resolution_id, result)
        return result


def resolve_selected(connection, resolution_id):
    """Validate a stored resolution against the CURRENT directory state.

    Returns the selected candidate (with server-side fingerprint) or raises a
    typed mismatch. A user selection is never re-derived from name/amount order.
    """
    row = connection.execute('SELECT * FROM payments.recipient_resolutions WHERE resolution_id=%s',
                             (resolution_id,)).fetchone()
    if row is None:
        raise PaymentError('resolution_not_found', '收款解析不存在或不属于当前用户', 404)
    if row['expires_at'] <= now():
        raise PaymentError('resolution_expired', '收款解析已过期，请重新解析收款人', 409)
    if row['status'] not in {'resolved'}:
        raise PaymentError('recipient_selection_required', '收款人多候选尚未选择，不能准备付款', 422)
    candidate_id = row['selected_candidate_id']
    if candidate_id is None:
        if len(row['candidates']) == 1:
            candidate_id = row['candidates'][0]['candidate_id']
        else:
            raise PaymentError('recipient_selection_required', '收款人多候选尚未选择，不能准备付款', 422)
    stored = next((item for item in row['candidates'] if item['candidate_id'] == candidate_id), None)
    if stored is None:
        raise PaymentError('recipient_changed', '收款人候选已变化，请重新解析核对', 409)
    recipient_id = stored.get('recipient_id')
    if recipient_id is None:
        raise PaymentError('recipient_unsupported', '该候选不是可付款收款人', 422)
    current = directory_recipient(connection, recipient_id)
    if current is None:
        raise PaymentError('recipient_changed', '收款人已不在本人收款目录，请重新解析', 409)
    if recipient_fingerprint(current) != stored.get('fingerprint'):
        raise PaymentError('recipient_changed', '收款人信息已变化，请重新解析核对', 409)
    from security.payment_auth import simulated_external
    if (int(current['trusted']) != 1 and not simulated_external(current['recipient_id'],connection)) or current['status'] != 'active':
        raise PaymentError('recipient_unsupported', '该收款人未被标记为可信或已停用，不能准备付款', 422)
    return dict(row=row, stored=stored, recipient=current)


def revalidate_resolution(connection, row):
    summary = dict(limitations=[], status=None)
    if row['expires_at'] <= now():
        summary['status'] = 'needs_information'
        summary['limitations'].append('解析已过期，需要重新解析')
        return summary
    for candidate in row['candidates']:
        if candidate.get('recipient_id') is None:
            continue
        current = directory_recipient(connection, candidate['recipient_id'])
        if current is None or recipient_fingerprint(current) != candidate.get('fingerprint'):
            summary['status'] = 'needs_information'
            summary['limitations'].append('收款人目录信息已变化，需要重新解析核对')
            break
    return summary


