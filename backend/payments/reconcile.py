"""Receipt / bank-receipt reconciliation with existing ledger rows.

Reconciliation is read-only with respect to money: it never inserts a
transaction, never changes a balance and never creates an order. Linking a
voucher stores evidence only; an invoice or a user confirmation is not treated
as proof of payment.
"""
from __future__ import annotations

from .documents import (_digest, _document, _extraction, _obligation_for,
                        _version, public_document)
from .errors import PaymentError
from .models import selected_amount_cents
from .service import (Jsonb, audit, begin_request, finish_request, iso,
                      sha256_json, transaction)

LINKABLE_MATCH = {'unique_match', 'multiple_candidates'}
PAYMENT_EVIDENCE_KINDS = {'payment_notice', 'receipt', 'bank_receipt'}
MAX_RANGE_DAYS = 366


def _reconciliation(connection, reconciliation_id, *, lock=False):
    row = connection.execute('SELECT * FROM payments.reconciliation_runs WHERE reconciliation_id=%s'
                             + (' FOR UPDATE' if lock else ''), (reconciliation_id,)).fetchone()
    if row is None:
        raise PaymentError('reconciliation_not_found', '未找到本人对账记录', 404)
    return row


def reconciliation_public(row):
    return dict(
        reconciliation_id=row['reconciliation_id'], revision=row['revision'],
        document_id=row['document_id'], document_revision=row['document_revision'],
        document_digest=row['document_digest'], range=dict(
            start=row['range_start'], end_exclusive=row['range_end_exclusive']),
        match_status=row['match_status'], candidates=row['candidates'] or [],
        compared_fields=row['compared_fields'] or [],
        unknown_fields=row['unknown_fields'] or [],
        created_at=iso(row['created_at']))


def _document_amount(extraction, selected_item_ids):
    reconciliation = extraction.reconciliation
    declared = reconciliation.amount_cents.value
    selected = selected_amount_cents(extraction, selected_item_ids)
    if declared is not None and selected is not None and declared != selected:
        raise PaymentError('amount_conflict',
            '单据申报金额与所选缴费项目合计不一致（%d 分 vs %d 分），不能对账' % (declared, selected), 422)
    amount = declared if declared is not None else selected
    if amount is None:
        raise PaymentError('amount_required', '单据没有可对账的金额；请先核对金额', 422)
    return amount


def _candidate_rows(connection, start, end_exclusive):
    return connection.execute('''SELECT t.transaction_id,t.card_id,t.date,t.merchant,t.category,
        t.amount_cents,t.direction,t.transaction_type,t.external_source_id,t.source_order_id,
        k.masked_number
        FROM pg02.transactions t
        LEFT JOIN core.cards k ON k.public_id=t.card_id
        WHERE t.direction='expense' AND t.date>=%s AND t.date<%s
        ORDER BY t.date,t.transaction_id LIMIT 201''', (start, end_exclusive)).fetchall()


def _last4(masked):
    if not masked:
        return None
    digits = ''.join(ch for ch in str(masked) if ch.isdigit())
    return digits[-4:] if len(digits) >= 4 else None


def _compare(row, extraction):
    reconciliation = extraction.reconciliation
    compared = []
    contradictions = []
    unknown = []
    expected_amount = reconciliation.amount_cents.value
    if expected_amount is not None:
        compared.append(dict(field='amount_cents', expected=expected_amount,
                             actual=row['amount_cents'],
                             match=row['amount_cents'] == expected_amount))
        if row['amount_cents'] != expected_amount:
            contradictions.append('amount_cents')
    else:
        unknown.append('amount_cents')
    merchant = reconciliation.merchant.value
    if merchant:
        actual = (row['merchant'] or '').strip()
        equals = actual == merchant.strip()
        compared.append(dict(field='merchant', expected=merchant, actual=actual, match=equals))
        if not equals:
            contradictions.append('merchant')
    else:
        unknown.append('merchant')
    last4 = reconciliation.card_last4.value
    if last4:
        actual = _last4(row['masked_number'])
        equals = actual is not None and actual == last4.strip()
        compared.append(dict(field='card_last4', expected=last4, actual=actual, match=equals))
        if not equals:
            contradictions.append('card_last4')
    else:
        unknown.append('card_last4')
    reference = reconciliation.reference_number.value
    if reference:
        candidates = {value for value in (row['external_source_id'], row['source_order_id']) if value}
        equals = reference.strip() in candidates
        compared.append(dict(field='reference_number', expected=reference,
                             actual=sorted(candidates), match=equals))
        if not equals:
            contradictions.append('reference_number')
    else:
        unknown.append('reference_number')
    if reconciliation.paid_on.value:
        compared.append(dict(field='paid_on', expected=reconciliation.paid_on.value,
                             actual=row['date'], match=row['date'] == reconciliation.paid_on.value))
    else:
        unknown.append('paid_on')
    # Existing ledger rows have day precision only; time is never inferred.
    unknown.append('time')
    return compared, contradictions, sorted(set(unknown))


def _candidate_record(row, compared, contradictions):
    return dict(
        transaction_id=row['transaction_id'], date=row['date'], merchant=row['merchant'],
        amount_cents=row['amount_cents'], direction=row['direction'],
        transaction_type=row['transaction_type'], card_last4=_last4(row['masked_number']),
        compared=compared, contradictions=contradictions,
        evidence=[dict(source='pg02.transactions', reference=str(row['transaction_id']),
                       note='本系统流水快照；不证明外部渠道已清算')])


def reconcile(principal, document_id, body):
    payload = dict(document_id=document_id, **body.model_dump(mode='json'))
    with transaction(principal, True) as connection:
        replay = begin_request(connection, 'document.reconcile', body.client_request_id, payload)
        if replay is not None:
            row = _reconciliation(connection, replay['resource_id'])
            return dict(reconciliation=reconciliation_public(row), document=public_document(
                connection, _document(connection, document_id)), replayed=True)
        document = _document(connection, document_id, lock=True)
        if document['revision'] != body.revision:
            raise PaymentError('document_revision_conflict',
                '单据版本已变化（当前 revision=%d）；请重新读取' % document['revision'], 409)
        extraction = _extraction(document)
        digest = _digest(document, extraction)
        if body.digest != digest:
            raise PaymentError('document_digest_mismatch', '核对摘要已失效；请重新读取再对账', 409)
        amount = _document_amount(extraction, document['selected_item_ids'] or [])
        rows = _candidate_rows(connection, body.start, body.end_exclusive)
        amount_matches = [row for row in rows if row['amount_cents'] == amount]
        compared_fields = []
        compared_names = set()
        unknown_set = set()
        survivors = []
        conflicts = []
        for row in amount_matches:
            compared, contradictions, unknown = _compare(row, extraction)
            if not compared_fields:
                compared_fields = compared
            compared_names |= {item['field'] for item in compared}
            unknown_set |= set(unknown)
            record = _candidate_record(row, compared, contradictions)
            if contradictions:
                conflicts.append(record)
            else:
                survivors.append(record)
        unknown_fields = sorted(unknown_set)
        if not amount_matches:
            status = 'not_found'
            candidates = []
        elif conflicts and not survivors:
            status = 'conflict'
            candidates = conflicts
        elif len(survivors) == 1:
            status = 'unique_match'
            candidates = survivors
        else:
            status = 'multiple_candidates'
            candidates = survivors
        reconciliation_id = 'rc_' + sha256_json([body.client_request_id, document_id])[:32]
        connection.execute('''INSERT INTO payments.reconciliation_runs
            (reconciliation_id,client_request_id,document_id,document_revision,document_digest,
             range_start,range_end_exclusive,match_status,candidates,compared_fields,unknown_fields)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (reconciliation_id, body.client_request_id, document_id, document['revision'], digest,
             body.start, body.end_exclusive, status, Jsonb(candidates), Jsonb(sorted(compared_names)),
             Jsonb(unknown_fields)))
        audit(connection, 'document.reconcile', document_id=document_id,
              revision=document['revision'],
              detail=dict(reconciliation_id=reconciliation_id, match_status=status,
                          amount_cents=amount))
        row = _reconciliation(connection, reconciliation_id)
        result = dict(reconciliation=reconciliation_public(row),
                      document=public_document(connection, _document(connection, document_id)),
                      replayed=False)
        finish_request(connection, 'document.reconcile', body.client_request_id,
                       reconciliation_id, result)
        return result


def _current_transaction(connection, transaction_id):
    return connection.execute('''SELECT t.transaction_id,t.card_id,t.date,t.merchant,t.category,
        t.amount_cents,t.direction,t.transaction_type,t.external_source_id,t.source_order_id,
        k.masked_number
        FROM pg02.transactions t LEFT JOIN core.cards k ON k.public_id=t.card_id
        WHERE t.transaction_id=%s''', (transaction_id,)).fetchone()


def link(principal, document_id, body):
    payload = dict(document_id=document_id, **body.model_dump(mode='json'))
    with transaction(principal, True) as connection:
        replay = begin_request(connection, 'document.link', body.client_request_id, payload)
        if replay is not None and replay['result']:
            result = dict(replay['result'])
            result['replayed'] = True
            return result
        document = _document(connection, document_id, lock=True)
        if document['revision'] != body.revision:
            raise PaymentError('document_revision_conflict',
                '单据版本已变化（当前 revision=%d）；请重新读取' % document['revision'], 409)
        extraction = _extraction(document)
        if body.digest != _digest(document, extraction):
            raise PaymentError('document_digest_mismatch', '核对摘要已失效；请重新读取再关联', 409)
        if document['kind'] == 'invoice':
            raise PaymentError('invoice_not_payment_evidence',
                '发票不能作为已付款凭证；未关联流水，也未生成扣款', 422)
        if document['kind'] not in PAYMENT_EVIDENCE_KINDS:
            raise PaymentError('document_kind_not_payment_evidence',
                '该单据类型不能关联付款流水；未关联流水，也未生成扣款', 422)
        reconciliation = _reconciliation(connection, body.reconciliation_id)
        if reconciliation['document_id'] != document_id:
            raise PaymentError('reconciliation_stale', '对账记录不属于当前单据', 409)
        if reconciliation['revision'] != body.reconciliation_revision:
            raise PaymentError('reconciliation_stale',
                '对账记录版本已变化（当前 revision=%d）' % reconciliation['revision'], 409)
        if reconciliation['document_revision'] != document['revision'] or \
                reconciliation['document_digest'] != body.digest:
            raise PaymentError('reconciliation_stale', '单据已变化，旧对账结果失效；请重新对账', 409)
        if reconciliation['match_status'] not in LINKABLE_MATCH:
            raise PaymentError('reconciliation_not_linkable',
                '对账结果为 %s，不能关联流水' % reconciliation['match_status'], 422)
        candidate = next((item for item in reconciliation['candidates']
                          if item['transaction_id'] == body.transaction_id), None)
        if candidate is None:
            raise PaymentError('transaction_not_in_candidates',
                '所选流水不在本次对账候选中，未关联', 422)
        current = _current_transaction(connection, body.transaction_id)
        if current is None or current['direction'] != 'expense':
            raise PaymentError('transaction_not_found', '未找到本人该笔支出流水', 404)
        # Re-read the current row and prove every compared field still matches.
        recheck, contradictions, _unknown = _compare(current, extraction)
        if contradictions:
            raise PaymentError('transaction_changed',
                '流水在核对后已变化（%s），旧证据失效；请重新对账' % '、'.join(contradictions), 409)
        snapshot_mismatch = []
        for field, expected in (('amount_cents', candidate['amount_cents']),
                                ('date', candidate['date']),
                                ('merchant', candidate['merchant']),
                                ('transaction_type', candidate['transaction_type']),
                                ('card_last4', candidate['card_last4'])):
            actual = _last4(current['masked_number']) if field == 'card_last4' else current[field]
            if actual != expected:
                snapshot_mismatch.append(field)
        if snapshot_mismatch:
            raise PaymentError('transaction_changed',
                '流水关键字段在核对后已变化（%s），旧证据失效；请重新对账'
                % '、'.join(snapshot_mismatch), 409)
        duplicate = connection.execute('''SELECT * FROM payments.transaction_links
            WHERE document_id=%s AND transaction_id=%s''', (document_id, body.transaction_id)).fetchone()
        if duplicate is not None:
            duplicate_result = dict(link=dict(link_id=duplicate['link_id'],
                                              document_id=duplicate['document_id'],
                                              transaction_id=duplicate['transaction_id'],
                                              reconciliation_id=duplicate['reconciliation_id'],
                                              payment_status=duplicate['payment_status'],
                                              evidence=duplicate['evidence'],
                                              created_at=iso(duplicate['created_at'])),
                                    document=public_document(connection, document), replayed=True)
            finish_request(connection, 'document.link', body.client_request_id,
                           duplicate['link_id'], duplicate_result)
            return duplicate_result
        obligation = _obligation_for(connection, document_id)
        link_id = 'tl_' + sha256_json([body.client_request_id, document_id])[:32]
        evidence = dict(transaction=candidate, recheck=recheck,
                        reconciliation=dict(reconciliation_id=reconciliation['reconciliation_id'],
                                            revision=reconciliation['revision'],
                                            match_status=reconciliation['match_status']),
                        user_selected=True,
                        limitation='本系统流水证据；不证明外部渠道已清算，也未生成扣款')
        connection.execute('''INSERT INTO payments.transaction_links
            (link_id,client_request_id,document_id,document_revision,document_digest,
             reconciliation_id,reconciliation_revision,transaction_id,evidence,payment_status)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'found_in_this_system')''',
            (link_id, body.client_request_id, document_id, document['revision'], body.digest,
             reconciliation['reconciliation_id'], reconciliation['revision'],
             body.transaction_id, Jsonb(evidence)))
        if obligation is not None:
            connection.execute("""UPDATE payments.obligations SET state='linked',
                revision=revision+1,updated_at=now() WHERE obligation_id=%s""",
                (obligation['obligation_id'],))
        if document['state'] != 'prepared':
            # Evidence-only transition: reviewed fields and revision are
            # unchanged, so the link stays bound to the same revision+digest.
            # Document revisions remain content revisions (upload/extract/review/
            # prepare); linking never rewrites the reviewed document version.
            connection.execute("""UPDATE payments.payment_documents SET state='reconciled',
                updated_at=now() WHERE document_id=%s""", (document_id,))
        audit(connection, 'document.link', document_id=document_id, revision=document['revision'],
              detail=dict(link_id=link_id, transaction_id=body.transaction_id,
                          reconciliation_id=reconciliation['reconciliation_id']))
        row = connection.execute('SELECT * FROM payments.transaction_links WHERE link_id=%s',
                                 (link_id,)).fetchone()
        result = dict(link=dict(link_id=row['link_id'], document_id=row['document_id'],
                                transaction_id=row['transaction_id'],
                                reconciliation_id=row['reconciliation_id'],
                                payment_status=row['payment_status'], evidence=row['evidence'],
                                created_at=iso(row['created_at'])),
                      document=public_document(connection, _document(connection, document_id)),
                      ledger_unchanged=True, replayed=False)
        finish_request(connection, 'document.link', body.client_request_id, link_id, result)
        return result
