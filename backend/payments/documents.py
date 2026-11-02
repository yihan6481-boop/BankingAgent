"""Payment document lifecycle: upload, extraction, review, obligation dedupe.

Extraction output is candidate data. Only the user's explicit review can make a
document ready to prepare, and preparation still goes through the existing
transfer draft/task preview + confirmation + strong-auth kernel.
"""
from __future__ import annotations

import re
from uuid import uuid4

from billvision.provider import VisionError, decode_image
from lifecycle.store import TaskError

from .errors import PaymentError
from .models import (DIGEST, Extraction, digest_payload, field_reasons,
                     selected_amount_cents)
from .service import (Jsonb, audit, begin_request, check_connection, finish_request,
                      iso, mask_account, now, recipient_fingerprint, resolve_selected,
                      sha256_json, transaction)

STATE_LABELS = {'uploaded', 'extracting', 'extraction_failed', 'review', 'ready', 'prepared', 'reconciled'}
PREPARE_BLOCKING_KINDS = {'unknown', 'invoice', 'receipt', 'bank_receipt'}


def _preparation_public(row):
    # Lazy import: prepare imports this module for document primitives.
    from .prepare import preparation_public
    return preparation_public(row)


def _preparation_recovery(connection, preparation):
    from .prepare import recovery_summary
    return recovery_summary(connection, preparation)


def review_digest(document_id, revision, kind, extraction, selected_item_ids, selected_payee_index):
    payload = digest_payload(document_id, revision, kind, extraction,
                             selected_item_ids, selected_payee_index)
    return sha256_json(payload)


def _document(connection, document_id, *, lock=False):
    row = connection.execute('SELECT * FROM payments.payment_documents WHERE document_id=%s'
                             + (' FOR UPDATE' if lock else ''), (document_id,)).fetchone()
    if row is None:
        raise PaymentError('payment_document_not_found', '未找到本人缴费单据', 404)
    return row


def _extraction(row):
    raw = row.get('fields') or row.get('extraction') or {}
    try:
        return Extraction.model_validate(raw)
    except Exception:
        raise PaymentError('payment_document_corrupt', '单据字段存储不合法，需重新提取或删除后重传', 409) from None


def _digest(row, extraction=None):
    extraction = extraction or _extraction(row)
    return review_digest(row['document_id'], row['revision'], row['kind'], extraction,
                         row.get('selected_item_ids') or [], row.get('selected_payee_index'))


def _version(connection, row, operation):
    connection.execute('''INSERT INTO payments.document_versions
        (document_id,revision,operation,kind,fields,digest) VALUES(%s,%s,%s,%s,%s,%s)''',
        (row['document_id'], row['revision'], operation, row['kind'], Jsonb(row['fields']),
         _digest(row)))


def selection_blockers(extraction, selected_item_ids, selected_payee_index):
    blockers = []
    if extraction.kind in PREPARE_BLOCKING_KINDS:
        blockers.append('kind_not_payable')
    if not extraction.payee_accounts:
        blockers.append('payee:qr_only')
    elif len(extraction.payee_accounts) > 1 and selected_payee_index is None:
        blockers.append('payee_selection_required')
    elif selected_payee_index is not None:
        if selected_payee_index >= len(extraction.payee_accounts):
            blockers.append('payee_selection_invalid')
        elif extraction.payee_accounts[selected_payee_index].account_number.value is None:
            blockers.append('payee_account_incomplete')
    if not selected_item_ids:
        blockers.append('item_selection_required')
    else:
        total = selected_amount_cents(extraction, selected_item_ids)
        if total is None:
            blockers.append('amount_unresolved')
        elif total <= 0:
            blockers.append('amount_invalid')
    if extraction.remark_requirement.value is not None and len(extraction.remark_requirement.value) > 100:
        blockers.append('remark_requirement_too_long')
    return blockers


def readiness(extraction, selected_item_ids, selected_payee_index):
    blockers = field_reasons(extraction) + selection_blockers(
        extraction, selected_item_ids, selected_payee_index)
    return sorted(set(blockers))


def sanitize_extraction(extraction, *, mask_accounts=True):
    value = extraction.model_dump(mode='json')
    if mask_accounts:
        for account in value.get('payee_accounts') or []:
            number = account.get('account_number') or {}
            if number.get('value'):
                number['value'] = mask_account(number['value'])
    return value


def obligation_identity(extraction):
    def value(field):
        return field.value if getattr(field, 'value', None) is not None else None
    return dict(
        kind=extraction.kind,
        institution=value(extraction.institution),
        bill_number=value(extraction.bill_number),
        payer_name=value(extraction.payer_name),
        period_start=value(extraction.period_start),
        period_end=value(extraction.period_end),
        period_label=value(extraction.period_label),
    )


def _upsert_obligation(connection, document, extraction, amount_cents):
    identity = obligation_identity(extraction)
    fingerprint = sha256_json(identity)
    prior = connection.execute('SELECT obligation_id FROM payments.document_obligations WHERE document_id=%s',
                               (document['document_id'],)).fetchone()
    existing = connection.execute('SELECT * FROM payments.obligations WHERE fingerprint=%s FOR UPDATE',
                                  (fingerprint,)).fetchone()
    if existing is None:
        obligation_id = 'ob_' + uuid4().hex
        connection.execute('''INSERT INTO payments.obligations
            (obligation_id,fingerprint,kind,institution,bill_number,payer_name,
             period_start,period_end,period_label,amount_cents)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (obligation_id, fingerprint, extraction.kind,
             identity['institution'], identity['bill_number'], identity['payer_name'],
             identity['period_start'], identity['period_end'], identity['period_label'], amount_cents))
        existing = connection.execute('SELECT * FROM payments.obligations WHERE obligation_id=%s',
                                      (obligation_id,)).fetchone()
    elif existing['amount_cents'] != amount_cents:
        same_document = prior is not None and prior['obligation_id'] == existing['obligation_id']
        if not same_document:
            # Same institution/bill/payer/period from a DIFFERENT image with a
            # different payable amount is a conflict, never a new obligation.
            raise PaymentError('obligation_amount_conflict',
                '同一缴费义务已存在但金额不同（已存 %d 分，本次 %d 分）；未生成新义务，请核对单据或联系管理员'
                % (existing['amount_cents'], amount_cents), 409,
                data=dict(obligation_id=existing['obligation_id'],
                          existing_amount_cents=existing['amount_cents'],
                          submitted_amount_cents=amount_cents))
        # The same document is being corrected: the obligation follows the
        # reviewed value, keeping one durable obligation (never a second one).
        connection.execute('''UPDATE payments.obligations SET amount_cents=%s,
            revision=revision+1,updated_at=now() WHERE obligation_id=%s''',
            (amount_cents, existing['obligation_id']))
        existing = connection.execute('SELECT * FROM payments.obligations WHERE obligation_id=%s',
                                      (existing['obligation_id'],)).fetchone()
    connection.execute('''INSERT INTO payments.document_obligations
        (document_id,obligation_id,document_revision) VALUES(%s,%s,%s)
        ON CONFLICT(owner_uuid,document_id) DO UPDATE
        SET obligation_id=EXCLUDED.obligation_id,document_revision=EXCLUDED.document_revision,
            linked_at=now()''', (document['document_id'], existing['obligation_id'], document['revision']))
    return existing


def _obligation_for(connection, document_id):
    return connection.execute('''SELECT o.* FROM payments.document_obligations d
        JOIN payments.obligations o ON o.owner_uuid=d.owner_uuid AND o.obligation_id=d.obligation_id
        WHERE d.document_id=%s''', (document_id,)).fetchone()


def _active_preparation(connection, document_id=None, obligation_id=None, lock=False):
    if obligation_id is not None:
        where, params = 'obligation_id=%s', (obligation_id,)
    else:
        where, params = 'document_id=%s', (document_id,)
    return connection.execute('''SELECT * FROM payments.preparations
        WHERE ''' + where + " AND state IN ('preparing','draft_ready','prepared')"
        + ' ORDER BY created_at DESC LIMIT 1'
        + (' FOR UPDATE' if lock else ''), params).fetchone()


def _task_status(connection, task_id):
    if not task_id:
        return None
    row = connection.execute('SELECT status FROM pg02.lifecycle_tasks WHERE task_id=%s',
                             (task_id,)).fetchone()
    return None if row is None else row['status']


def _preparations(connection, document_id):
    return connection.execute('''SELECT * FROM payments.preparations
        WHERE document_id=%s ORDER BY created_at''', (document_id,)).fetchall()


def _links(connection, document_id):
    return connection.execute('''SELECT * FROM payments.transaction_links
        WHERE document_id=%s ORDER BY created_at''', (document_id,)).fetchall()


def verification(connection, row, extraction):
    digest = _digest(row, extraction)
    links = _links(connection, row['document_id'])
    current = [link for link in links
               if link['document_revision'] == row['revision'] and link['document_digest'] == digest]
    stale = [link for link in links if link not in current]
    association = dict(status='unknown', evidence=[])
    if row.get('user_confirmed_own_bill'):
        association = dict(status='match', evidence=[dict(source='user_declaration',
            note='用户声明该单据与本人相关；用户声明不是机构认证')])
    payment = dict(
        status='found_in_this_system' if current else 'not_found_in_this_system',
        external_channels='unknown',
        evidence=[dict(source='payment.transaction_links', reference=link['link_id'],
                       transaction_id=link['transaction_id'],
                       document_revision=link['document_revision'],
                       document_digest=link['document_digest'],
                       note='仅表示本系统内存在与当前 revision+digest 匹配的流水；不证明外部渠道已清算')
                  for link in current],
        historical_evidence=[dict(source='payment.transaction_links', reference=link['link_id'],
                                  transaction_id=link['transaction_id'],
                                  document_revision=link['document_revision'],
                                  document_digest=link['document_digest'],
                                  stale=True,
                                  note='改单前的历史凭证关联；不进入当前付款状态判断')
                             for link in stale],
        stale_evidence_ignored=len(stale))
    payee = dict(status='not_applicable', evidence=[])
    if extraction.payee_accounts:
        payee = dict(status='unknown', evidence=[dict(source='simulated_recipient_directory',
            note='模拟目录/单据文字，不是现实银行账户归属认证')])
    return dict(
        user_association=association,
        document_authenticity=dict(status='unknown', evidence=[]),
        payee_ownership=payee,
        payment_status=payment,
    )


def public_document(connection, row, *, repeated_upload=False, replayed=False):
    extraction = _extraction(row)
    selected_item_ids = list(row.get('selected_item_ids') or [])
    blockers = readiness(extraction, selected_item_ids, row.get('selected_payee_index'))
    state = row['state']
    digest = _digest(row, extraction)
    obligation = _obligation_for(connection, row['document_id'])
    preparation = _active_preparation(connection, document_id=row['document_id'])
    links = _links(connection, row['document_id'])
    total = selected_amount_cents(extraction, selected_item_ids)
    limitations = ['识图与模拟收款目录只是候选证据，不是银行或收费机构的真实性/归属认证',
                   '关联流水或用户确认都不会生成扣款；付款仍须既有任务预览、确认和强验证']
    if any(link['document_revision'] != row['revision'] or link['document_digest'] != digest
           for link in links):
        limitations.append('改单前的旧凭证关联只作历史证据，不进入当前付款状态判断')
    return dict(
        document_id=row['document_id'], revision=row['revision'], state=state, kind=row['kind'],
        digest=digest, created_at=iso(row['created_at']),
        updated_at=iso(row['updated_at']), repeated_upload=repeated_upload,
        replayed=replayed,
        image=dict(sha256=row['image_hash'], mime=row['mime'],
                   width=row['width'], height=row['height']),
        fields=sanitize_extraction(extraction),
        original=None if row['extraction'] is None else sanitize_extraction(
            Extraction.model_validate(row['extraction'])),
        selected_item_ids=selected_item_ids,
        selected_payee_index=row.get('selected_payee_index'),
        total_amount_cents=total, blocking=blockers,
        ready=not blockers and state in {'review', 'ready', 'prepared', 'reconciled'},
        user_notes=row.get('user_notes'), user_confirmed_own_bill=row.get('user_confirmed_own_bill', False),
        verification=verification(connection, row, extraction),
        obligation=None if obligation is None else dict(
            obligation_id=obligation['obligation_id'], state=obligation['state'],
            amount_cents=obligation['amount_cents'], revision=obligation['revision']),
        preparation=None if preparation is None else dict(
            preparation_id=preparation['preparation_id'], state=preparation['state'],
            draft_reference=preparation['draft_reference'], task_id=preparation['task_id'],
            amount_cents=preparation['amount_cents'], from_card_id=preparation['from_card_id'],
            recovery=_preparation_recovery(connection, preparation)),
        links=[dict(link_id=link['link_id'], transaction_id=link['transaction_id'],
                    reconciliation_id=link['reconciliation_id'],
                    document_revision=link['document_revision'],
                    document_digest=link['document_digest'],
                    current=(link['document_revision'] == row['revision']
                             and link['document_digest'] == digest),
                    stale=not (link['document_revision'] == row['revision']
                               and link['document_digest'] == digest),
                    payment_status=link['payment_status'], created_at=iso(link['created_at']))
               for link in links],
        error_code=row.get('error_code'),
        limitations=limitations,
    )


# ---------------------------------------------------------------------------
# Upload / read.
# ---------------------------------------------------------------------------

def upload(principal, data, client_request_id):
    try:
        checked = decode_image(data)
    except VisionError as exc:
        raise PaymentError(exc.code, exc.message, exc.status) from None
    with transaction(principal, True) as connection:
        replay = begin_request(connection, 'document.upload', client_request_id,
                               dict(image_sha256=checked['sha256']))
        if replay is not None:
            row = _document(connection, replay['resource_id'])
            return public_document(connection, row, replayed=True)
        old = connection.execute('SELECT * FROM payments.payment_documents WHERE image_hash=%s',
                                 (checked['sha256'],)).fetchone()
        if old is not None:
            result = public_document(connection, old, repeated_upload=True)
            finish_request(connection, 'document.upload', client_request_id, old['document_id'], result)
            return result
        document_id = 'pd_' + uuid4().hex
        connection.execute('''INSERT INTO payments.payment_documents
            (document_id,client_request_id,image_hash,image,mime,width,height)
            VALUES(%s,%s,%s,%s,%s,%s,%s)''',
            (document_id, client_request_id, checked['sha256'], data, checked['mime'],
             checked['width'], checked['height']))
        row = _document(connection, document_id)
        _version(connection, row, 'upload')
        audit(connection, 'document.upload', document_id=document_id, revision=row['revision'])
        result = public_document(connection, row)
        finish_request(connection, 'document.upload', client_request_id, document_id, result)
        return result


def list_documents(principal):
    with transaction(principal) as connection:
        rows = connection.execute('''SELECT document_id,revision,state,kind,created_at,updated_at
            FROM payments.payment_documents ORDER BY created_at DESC LIMIT 101''').fetchall()
        return dict(documents=[dict(row, created_at=iso(row['created_at']),
                                    updated_at=iso(row['updated_at'])) for row in rows[:100]],
                    has_more=len(rows) > 100)


def get_document(principal, document_id):
    with transaction(principal) as connection:
        return public_document(connection, _document(connection, document_id))


def image(principal, document_id):
    with transaction(principal) as connection:
        row = _document(connection, document_id)
        return bytes(row['image']), row['mime']


def versions(principal, document_id):
    with transaction(principal) as connection:
        _document(connection, document_id)
        rows = connection.execute('''SELECT revision,operation,kind,fields,digest,created_at
            FROM payments.document_versions WHERE document_id=%s ORDER BY revision''',
            (document_id,)).fetchall()
        return [dict(row, created_at=iso(row['created_at']), fields=sanitize_extraction(
            Extraction.model_validate(row['fields'])) if row['fields'] else None) for row in rows]


# ---------------------------------------------------------------------------
# Extraction.
# ---------------------------------------------------------------------------

def _rewrite_evidence(extraction, document_id):
    value = extraction.model_dump(mode='json')
    def walk(node):
        if isinstance(node, dict):
            if 'asset_id' in node and isinstance(node.get('asset_id'), str):
                node['asset_id'] = document_id
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(value)
    return Extraction.model_validate(value)


def extract(principal, document_id, revision, client_request_id, provider=None):
    payload = dict(document_id=document_id, revision=revision)
    with transaction(principal, True) as connection:
        replay = begin_request(connection, 'document.extract', client_request_id, payload)
        if replay is not None:
            row = _document(connection, replay['resource_id'])
            return public_document(connection, row, replayed=True)
        row = _document(connection, document_id, lock=True)
        if row['revision'] != revision:
            raise PaymentError('document_revision_conflict',
                '单据版本已变化（当前 revision=%d）；请重新读取' % row['revision'], 409)
        stale = row['state'] == 'extracting' and row['extraction_started'] is not None and \
            (now() - row['extraction_started']).total_seconds() > 90
        if row['state'] == 'extracting' and not stale:
            raise PaymentError('extraction_in_progress',
                '该单据正在识别；请读取当前状态后再决定是否重试，避免重复扣费', 409,
                data=dict(document_id=document_id, revision=row['revision']))
        if row['state'] not in {'uploaded', 'extraction_failed'} and not stale:
            raise PaymentError('document_state_conflict',
                '该单据已有人工核对内容；重新识别会覆盖核对，当前未执行', 409,
                data=dict(document_id=document_id, revision=row['revision'], state=row['state']))
        token = uuid4().hex
        connection.execute("""UPDATE payments.payment_documents
            SET state='extracting',extraction_token=%s,extraction_started=now(),error_code=NULL
            WHERE document_id=%s""", (token, document_id))
        data = bytes(row['image'])
    from .provider import get_payment_provider
    try:
        result = (provider or get_payment_provider()).recognize(data)
        extraction = Extraction.model_validate(
            result.model_dump(mode='json') if isinstance(result, Extraction) else result)
        if not extraction.supported and extraction.items:
            raise PaymentError('extraction_output_invalid', '不支持的图片不能包含缴费项目', 502)
    except Exception as exc:
        failure = exc if isinstance(exc, PaymentError) else PaymentError(
            'extraction_failed', '识别失败，请读取当前状态后重试', 502)
        with transaction(principal, True) as connection:
            row = _document(connection, document_id, lock=True)
            if row['revision'] == revision and row['extraction_token'] == token and row['state'] == 'extracting':
                connection.execute("""UPDATE payments.payment_documents
                    SET state='extraction_failed',error_code=%s,extraction_token=NULL,updated_at=now()
                    WHERE document_id=%s""", (failure.code, document_id))
                audit(connection, 'document.extract_failed', document_id=document_id,
                      revision=row['revision'], detail=dict(code=failure.code))
        raise failure from None
    extraction = _rewrite_evidence(extraction, document_id)
    with transaction(principal, True) as connection:
        row = _document(connection, document_id, lock=True)
        if row['revision'] != revision:
            raise PaymentError('document_revision_conflict', '旧识别结果已失效；请重新读取', 409)
        if row['extraction_token'] != token or row['state'] != 'extracting':
            raise PaymentError('document_revision_conflict', '旧识别结果已失效；请重新读取', 409)
        fields = extraction.model_dump(mode='json')
        selected = []
        connection.execute('''UPDATE payments.payment_documents SET state='review',revision=revision+1,
            kind=%s,extraction=%s,fields=%s,selected_item_ids=%s,selected_payee_index=NULL,
            review_digest=NULL,extraction_token=NULL,error_code=NULL,updated_at=now()
            WHERE document_id=%s''',
            (extraction.kind, Jsonb(extraction.model_dump(mode='json')), Jsonb(fields), Jsonb(selected),
             document_id))
        row = _document(connection, document_id)
        _version(connection, row, 'extract')
        audit(connection, 'document.extract', document_id=document_id, revision=row['revision'],
              detail=dict(kind=extraction.kind, item_count=len(extraction.items),
                          payee_count=len(extraction.payee_accounts)))
        result = public_document(connection, row)
        finish_request(connection, 'document.extract', client_request_id, document_id, result)
        return result


# ---------------------------------------------------------------------------
# Review.
# ---------------------------------------------------------------------------

def restore_masked_accounts(stored, incoming):
    """The GET projection masks account numbers; review may echo them back."""
    value = incoming.model_dump(mode='json')
    stored_value = stored.model_dump(mode='json')
    stored_accounts = stored_value.get('payee_accounts') or []
    for index, account in enumerate(value.get('payee_accounts') or []):
        number = (account.get('account_number') or {}).get('value')
        if not number or not re.fullmatch(r'\*+[0-9]{4}', str(number)):
            continue
        match = None
        for candidate in stored_accounts:
            raw = (candidate.get('account_number') or {}).get('value')
            if raw and mask_account(raw) == number:
                match = raw
                break
        if match is None:
            raise PaymentError('payee_account_masked_unknown',
                '收款账号掩码与当前单据不一致；请重新读取后再核对，不自动补全', 422)
        account['account_number']['value'] = match
    return Extraction.model_validate(value)


def _review_plan(row, body):
    """Pure business validation of a review request against the current row."""
    current = _extraction(row)
    if body.digest != _digest(row, current):
        raise PaymentError('document_digest_mismatch',
            '核对摘要已失效；请重新读取当前单据后再提交', 409)
    extraction = restore_masked_accounts(current, body.fields)
    kind = body.kind or row['kind']
    if kind != extraction.kind:
        extraction = extraction.model_copy(update={'kind': kind})
    selected = list(body.selected_item_ids)
    known = {item.item_id for item in extraction.items}
    if any(item_id not in known for item_id in selected):
        raise PaymentError('item_selection_invalid', '所选缴费项目不属于当前单据', 422)
    if body.selected_payee_index is not None and body.selected_payee_index >= len(extraction.payee_accounts):
        raise PaymentError('payee_selection_invalid', '所选收款账户不属于当前单据', 422)
    blockers = readiness(extraction, selected, body.selected_payee_index)
    return dict(current=current, extraction=extraction, kind=kind, selected=selected,
                blockers=blockers, state='ready' if not blockers else 'review')


def review_preflight(principal, document_id, body):
    """Read-only validation; must complete before any task cancellation.

    Wrong owner/revision/digest, replayed requests and business-invalid fields
    all return here without side effects. The result either carries a stored
    replay receipt or the exact preparation/task that a valid edit may cancel.
    """
    with transaction(principal) as connection:
        existing = connection.execute('''SELECT * FROM payments.client_requests
            WHERE operation='document.review' AND client_request_id=%s''',
            (body.client_request_id,)).fetchone()
        payload_hash = sha256_json(dict(document_id=document_id, **body.model_dump(mode='json')))
        if existing is not None:
            if existing['request_hash'] != payload_hash:
                raise PaymentError('client_request_conflict',
                    '同一请求键已绑定不同参数；未重复执行，请使用新的请求键', 409)
            if existing['result']:
                return dict(replay=existing['result'], cancel_task_id=None, preparation_id=None)
        row = _document(connection, document_id)
        if row['revision'] != body.revision:
            raise PaymentError('document_revision_conflict',
                '单据版本已变化（当前 revision=%d）；请重新读取后再核对' % row['revision'], 409)
        _review_plan(row, body)
        cancel_task_id = None
        preparation_id = None
        if row['state'] == 'prepared':
            preparation = _active_preparation(connection, document_id=document_id)
            task_status = _task_status(connection, None if preparation is None else preparation['task_id'])
            if task_status == 'completed':
                raise PaymentError('document_payment_completed',
                    '该单据关联的付款任务已完成；不能修改已执行付款的核对内容', 409)
            if preparation is None:
                raise PaymentError('document_state_conflict',
                    '单据处于已准备状态但没有可核实的准备记录；请读取当前状态', 409)
            if task_status is None and preparation['task_id']:
                raise PaymentError('preparation_unverifiable',
                    '准备记录绑定的任务不可读；未取消未修改，请先核实', 503,
                    data=dict(preparation=_preparation_public(preparation),
                              task_id=preparation['task_id']))
            if task_status not in {'cancelled', 'failed', None}:
                cancel_task_id = preparation['task_id']
                preparation_id = preparation['preparation_id']
        return dict(replay=None, cancel_task_id=cancel_task_id,
                    preparation_id=preparation_id, expected_revision=row['revision'])


def _check_cancel_binding(record, preparation):
    """The task being cancelled must still be the transfer this prep created."""
    expected = dict(from_card_id=preparation['from_card_id'],
                    recipient_id=preparation['recipient_id'],
                    amount='%d.%02d' % (preparation['amount_cents'] // 100,
                                        preparation['amount_cents'] % 100))
    steps = record.task.steps
    arguments = dict(steps[0].arguments) if len(steps) == 1 and steps[0].tool_name == 'transfer_money' else None
    if arguments is None:
        raise PaymentError('preparation_state_mismatch',
            '准备记录与任务步骤不一致；未取消任务，请人工核实', 409,
            data=dict(task_id=record.task.task_id,
                      preparation=_preparation_public(preparation)))
    actual = {key: arguments.get(key) for key in expected}
    if any(str(actual[key]) != str(expected[key]) for key in expected):
        raise PaymentError('preparation_state_mismatch',
            '准备记录与任务参数不一致；未取消任务，请人工核实', 409,
            data=dict(task_id=record.task.task_id,
                      preparation=_preparation_public(preparation),
                      expected=expected, actual=actual))


def cancel_preparation_task(session_id, principal, document_id, *,
                            expected_preparation_id=None, expected_document_revision=None):
    """Cancel a task ONLY after the caller validated the edit request.

    The preparation/task binding is re-read immediately before cancellation; a
    changed, replaced or already-terminal task is never cancelled here.
    """
    from lifecycle.application import perform
    from lifecycle.models import RevisionRequest
    from lifecycle.store import TaskStore
    from lifecycle.routes import session_for
    with transaction(principal) as connection:
        preparation = _active_preparation(connection, document_id=document_id)
        if preparation is None or not preparation['task_id']:
            return []
        if expected_preparation_id is not None and preparation['preparation_id'] != expected_preparation_id:
            return []
        if expected_document_revision is not None:
            document = _document(connection, document_id)
            if document['revision'] != expected_document_revision:
                return []
        snapshot = dict(preparation)
    try:
        record = TaskStore().load(snapshot['task_id'], principal)
    except TaskError:
        return []
    if record.status not in {'active', 'waiting', 'paused', 'scheduled', 'manual'}:
        return []
    _check_cancel_binding(record, snapshot)
    # Last check immediately before the kernel call: same preparation, still
    # non-terminal, document revision unchanged.
    with transaction(principal) as connection:
        again = _active_preparation(connection, document_id=document_id)
        if again is None or again['preparation_id'] != snapshot['preparation_id']:
            return []
        if expected_document_revision is not None:
            document = _document(connection, document_id)
            if document['revision'] != expected_document_revision:
                return []
        if _task_status(connection, again['task_id']) not in {
                'active', 'waiting', 'paused', 'scheduled', 'manual'}:
            return []
    session_for(session_id)
    try:
        perform(session_id, record.task.task_id,
                RevisionRequest(revision=record.revision, reason='单据核对内容变化，旧预览失效'),
                'cancel', principal)
    except TaskError as error:
        if error.code not in {'stale_revision', 'terminal_task', 'invalid_transition'}:
            raise PaymentError('document_preparation_active',
                '已有付款任务无法安全取消（%s）；请先处理原任务，未修改单据' % error.code, 409)
    return [record.task.task_id]


def review(principal, document_id, body, *, expected_preparation_id=None):
    with transaction(principal, True) as connection:
        replay = begin_request(connection, 'document.review', body.client_request_id,
                               dict(document_id=document_id, **body.model_dump(mode='json')))
        if replay is not None:
            row = _document(connection, replay['resource_id'])
            return public_document(connection, row, replayed=True)
        row = _document(connection, document_id, lock=True)
        if row['revision'] != body.revision:
            raise PaymentError('document_revision_conflict',
                '单据版本已变化（当前 revision=%d）；请重新读取后再核对' % row['revision'], 409)
        plan = _review_plan(row, body)
        preparation = _active_preparation(connection, document_id=document_id, lock=True)
        if row['state'] == 'prepared':
            task_status = _task_status(connection, None if preparation is None else preparation['task_id'])
            if task_status == 'completed':
                raise PaymentError('document_payment_completed',
                    '该单据关联的付款任务已完成；不能修改已执行付款的核对内容', 409)
            if expected_preparation_id is not None and (
                    preparation is None or preparation['preparation_id'] != expected_preparation_id):
                raise PaymentError('preparation_recovery_required',
                    '付款准备在改单校验后发生变化；未修改单据，请重新读取后先核实准备状态', 409,
                    data=dict(document_id=document_id, revision=row['revision']))
            if preparation is None:
                raise PaymentError('document_state_conflict',
                    '单据处于已准备状态但没有可核实的准备记录；未修改单据', 409)
            if task_status is None and preparation['task_id']:
                raise PaymentError('preparation_unverifiable',
                    '准备记录绑定的任务不可读；未修改单据，请先核实', 503,
                    data=dict(preparation=_preparation_public(preparation),
                              task_id=preparation['task_id']))
            if task_status not in {'cancelled', 'failed', None}:
                raise PaymentError('document_state_conflict',
                    '该单据仍有进行中的付款任务；未修改单据，请先取消原任务', 409)
            connection.execute("""UPDATE payments.preparations SET state='cancelled',
                updated_at=now() WHERE preparation_id=%s""", (preparation['preparation_id'],))
            connection.execute("""UPDATE payments.obligations SET state='open',
                revision=revision+1,updated_at=now() WHERE obligation_id=%s""",
                (preparation['obligation_id'],))
        elif preparation is not None:
            # Another request prepared this document after our validation; do not
            # overwrite it and do not cancel its task from this edit.
            raise PaymentError('preparation_recovery_required',
                '单据存在在途付款准备；未修改单据，请先读取并核实准备/任务状态', 409,
                data=dict(preparation=_preparation_public(preparation)))
        extraction = plan['extraction']
        kind = plan['kind']
        selected = plan['selected']
        blockers = plan['blockers']
        state = plan['state']
        revision = row['revision'] + 1
        fields = extraction.model_dump(mode='json')
        connection.execute('''UPDATE payments.payment_documents SET state=%s,revision=%s,kind=%s,
            fields=%s,selected_item_ids=%s,selected_payee_index=%s,user_notes=%s,
            user_confirmed_own_bill=%s,review_digest=NULL,error_code=NULL,updated_at=now()
            WHERE document_id=%s''',
            (state, revision, kind, Jsonb(fields), Jsonb(selected), body.selected_payee_index,
             body.user_notes, body.user_confirmed_own_bill, document_id))
        row = _document(connection, document_id)
        digest = review_digest(document_id, revision, kind, extraction, selected,
                               body.selected_payee_index)
        connection.execute('UPDATE payments.payment_documents SET review_digest=%s WHERE document_id=%s',
                           (digest, document_id))
        row = _document(connection, document_id)
        if state == 'ready' and kind == 'payment_notice' and selected:
            _upsert_obligation(connection, row, extraction,
                               selected_amount_cents(extraction, selected))
        else:
            connection.execute('DELETE FROM payments.document_obligations WHERE document_id=%s',
                               (document_id,))
        _version(connection, row, 'review')
        audit(connection, 'document.review', document_id=document_id, revision=revision,
              detail=dict(state=state, blockers=blockers, kind=kind,
                          user_confirmed_own_bill=body.user_confirmed_own_bill))
        result = public_document(connection, row)
        finish_request(connection, 'document.review', body.client_request_id, document_id, result)
        return result


def delete(principal, document_id, revision):
    with transaction(principal, True) as connection:
        row = _document(connection, document_id, lock=True)
        if row['revision'] != revision:
            raise PaymentError('document_revision_conflict', '单据版本已变化；请重新读取', 409)
        preparation = _active_preparation(connection, document_id=document_id)
        if preparation is not None:
            task_status = _task_status(connection, preparation['task_id'])
            if preparation['state'] in {'preparing', 'draft_ready'} or task_status not in {'cancelled', 'failed'}:
                raise PaymentError('document_state_conflict',
                    '该单据仍有进行中的付款准备/任务，不能删除；请先处理原任务', 409,
                    data=dict(preparation=dict(preparation_id=preparation['preparation_id'],
                                               state=preparation['state'],
                                               task_id=preparation['task_id'])))
        if _links(connection, document_id):
            raise PaymentError('document_state_conflict',
                '该单据已关联流水证据，不能删除；请保留凭证审计记录', 409)
        connection.execute('DELETE FROM payments.payment_documents WHERE document_id=%s', (document_id,))
        audit(connection, 'document.delete', document_id=document_id, revision=revision)
        return dict(document_id=document_id, deleted=True)


def plan_review(document_id, revision, kind, extraction, selected_item_ids, selected_payee_index):
    """Read-only helper used by tests and contract examples."""
    digest = review_digest(document_id, revision, kind, extraction, selected_item_ids,
                           selected_payee_index)
    return dict(digest=digest, blocking=readiness(extraction, selected_item_ids,
                                                  selected_payee_index),
                amount_cents=selected_amount_cents(extraction, selected_item_ids))
