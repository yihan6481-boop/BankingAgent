"""Prepare-payment: bind a reviewed payment obligation to the EXISTING transfer
draft/task kernel. No new fund-execution path is introduced here.

Cross-transaction sequencing is explicit:

1. one short owner-scoped transaction validates the document revision, digest,
   recipient resolution, obligation dedupe and the client request key, then
   durably reserves a preparation row before any external call;
2. the existing foundation draft and task.prepare flow runs outside that
   transaction (``foundation.service.run``);
3. a second transaction attaches the resulting draft/task and document version.

An interrupted step leaves a durable ``preparing``/``draft_ready`` row that a
same-key replay resumes; it is never reported as successfully prepared.
"""
from __future__ import annotations

import json

from foundation.contracts import Call
from foundation.service import run as run_tool

from .documents import (_active_preparation, _digest, _document, _extraction,
                        _obligation_for, _task_status, _version, public_document,
                        readiness)
from .errors import PaymentError
from .models import DocumentPayeeSelector, selected_amount_cents
from .service import (Jsonb, _lookup_selector, audit, begin_request, finish_request, iso,
                      recipient_fingerprint, resolve_selected, sha256_json,
                      transaction)

TERMINAL_TASK = {'completed', 'cancelled', 'failed'}
TOOL_STATUS = {
    'conflict': 409, 'ambiguous': 409, 'invalid_parameters': 422,
    'missing_information': 422, 'unsupported': 422, 'denied': 403,
    'service_failure': 503, 'commit_unknown': 503, 'internal_error': 500,
    'business_condition': 409,
}
TASK_IN_PROGRESS_CODES = {'task_in_progress'}


def yuan(cents):
    return '%d.%02d' % (cents // 100, cents % 100)


def _preparation(connection, preparation_id, *, lock=False):
    row = connection.execute('SELECT * FROM payments.preparations WHERE preparation_id=%s'
                             + (' FOR UPDATE' if lock else ''), (preparation_id,)).fetchone()
    if row is None:
        raise PaymentError('preparation_not_found', '未找到本人付款准备记录', 404)
    return row


def preparation_public(row):
    return dict(
        preparation_id=row['preparation_id'], client_request_id=row['client_request_id'],
        state=row['state'], document_id=row['document_id'],
        document_revision=row['document_revision'], obligation_id=row['obligation_id'],
        item_ids=list(row['item_ids'] or []), amount_cents=row['amount_cents'],
        currency=row['currency'], recipient_resolution_id=row['recipient_resolution_id'],
        candidate_id=row['candidate_id'], recipient_id=row['recipient_id'],
        from_card_id=row['from_card_id'], remark=row['remark'],
        draft_reference=row['draft_reference'], draft_revision=row['draft_revision'],
        task_id=row['task_id'], task_revision=row['task_revision'],
        created_at=iso(row['created_at']), updated_at=iso(row['updated_at']))


def _tool_error(outcome, *, operation):
    code = outcome.get('code') or 'tool_failure'
    status = TOOL_STATUS.get(outcome.get('status'), 409)
    data = outcome.get('data') if isinstance(outcome.get('data'), dict) else {}
    safe = {key: data[key] for key in ('draft', 'field_errors', 'candidates', 'missing_fields')
            if key in data}
    raise PaymentError(code, '现有%s流程未完成（%s）；未创建新的资金执行路径' % (operation, code),
                       status, data=safe)


# ---------------------------------------------------------------------------
# Interrupted-preparation query / recovery contract.
# ---------------------------------------------------------------------------

def _draft_state(connection, reference):
    """Read-only view of the existing transfer draft; never mutates money."""
    if not reference:
        return None
    row = connection.execute('SELECT status,revision,payload FROM pg02.tool_goals WHERE reference=%s',
                             (reference,)).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row['payload'])
    except (TypeError, ValueError):
        return dict(reference=reference, status=row['status'], revision=row['revision'],
                    task_id=None, payload_invalid=True)
    task_id = None
    if isinstance(payload, dict):
        attempt = payload.get('_preparation')
        if isinstance(attempt, dict):
            task_id = attempt.get('task_id')
        if not task_id:
            task_id = payload.get('task_id')
    return dict(reference=reference, status=row['status'], revision=row['revision'],
                task_id=task_id, payload_invalid=False)


def recovery_summary(connection, preparation):
    draft = _draft_state(connection, preparation['draft_reference'])
    task_status = _task_status(connection, preparation['task_id'])
    required = preparation['state'] in {'preparing', 'draft_ready'}
    return dict(
        required=required,
        reason='interrupted_preparation' if required else None,
        draft=draft, task_status=task_status,
        next_actions=['payment.preparation.recover'] if required else [])


def _recovery_public(connection, preparation, *, status, released, verification):
    document = _document(connection, preparation['document_id'])
    return dict(contract_version='payment-1.0', data=dict(
        preparation=dict(preparation_public(preparation),
                         recovery=recovery_summary(connection, preparation)),
        status=status, released=released, verification=verification,
        document=public_document(connection, document)))


def _recovery_required(connection, preparation):
    """A different request key must verify before any release/reopen."""
    data = dict(preparation=preparation_public(preparation),
                draft=_draft_state(connection, preparation['draft_reference']),
                recovery=recovery_summary(connection, preparation),
                recover_path='/payment-preparations/%s/recover' % preparation['preparation_id'])
    return PaymentError('preparation_recovery_required',
        '存在未核实的中断付款准备；请先调用恢复接口核实原草稿与任务，未取消也未重新开放缴费义务',
        409, data=data)


def _release(principal, preparation, *, verification, reason):
    """Release only AFTER the caller verified no task exists/was created."""
    with transaction(principal, True) as connection:
        row = _preparation(connection, preparation['preparation_id'], lock=True)
        if row['state'] == 'cancelled':
            return _recovery_public(connection, row, status='cancelled', released=True,
                                    verification=verification)
        if row['state'] == 'prepared':
            return _recovery_public(connection, row, status='prepared', released=False,
                                    verification=verification)
        if row['draft_reference'] and row['draft_reference'] != preparation['draft_reference']:
            raise PaymentError('preparation_unverifiable',
                '付款准备草稿引用在恢复过程中变化；未取消未修改，请重新读取', 503,
                data=dict(preparation=preparation_public(row)))
        current_task_status = _task_status(connection, row['task_id'])
        if row['task_id'] and current_task_status not in {'cancelled', 'failed'}:
            raise PaymentError('preparation_task_active',
                '恢复核实发现任务仍活动；恢复接口不代取消任务，请先读取/取消原任务', 409,
                data=dict(preparation=preparation_public(row), task_id=row['task_id'],
                          next_actions=['task.read', 'task.cancel']))
        draft_after = _draft_state(connection, row['draft_reference']) if row['draft_reference'] else None
        draft_task = (draft_after or {}).get('task_id')
        if draft_task:
            draft_task_status = _task_status(connection, draft_task)
            if draft_task_status is None:
                raise PaymentError('preparation_unverifiable',
                    '草稿关联的任务不可读；未取消未修改，请人工核实', 503,
                    data=dict(preparation=preparation_public(row), task_id=draft_task))
            if draft_task_status not in {'cancelled', 'failed'}:
                raise PaymentError('preparation_task_active',
                    '恢复核实发现草稿已关联活动任务；恢复接口不代取消', 409,
                    data=dict(preparation=preparation_public(row), task_id=draft_task,
                              next_actions=['task.read', 'task.cancel']))
        connection.execute("""UPDATE payments.preparations SET state='cancelled',updated_at=now()
            WHERE preparation_id=%s""", (row['preparation_id'],))
        connection.execute("""UPDATE payments.obligations SET state='open',
            revision=revision+1,updated_at=now() WHERE obligation_id=%s""", (row['obligation_id'],))
        connection.execute("""UPDATE payments.payment_documents SET state='ready',updated_at=now()
            WHERE document_id=%s AND state='prepared'""", (row['document_id'],))
        audit(connection, 'document.prepare_recover', document_id=row['document_id'],
              revision=row['document_revision'],
              detail=dict(preparation_id=row['preparation_id'], released=True, reason=reason))
        row = _preparation(connection, row['preparation_id'])
        return _recovery_public(connection, row, status='released', released=True,
                                verification=verification)


def _finalize_recovered(principal, preparation, draft, task, verification):
    task_id = task['task']['task_id']
    task_revision = task['revision']
    with transaction(principal, True) as connection:
        row = _preparation(connection, preparation['preparation_id'], lock=True)
        if row['state'] == 'prepared':
            return _recovery_public(connection, row, status='prepared', released=False,
                                    verification=verification)
        document = _document(connection, row['document_id'], lock=True)
        if document['revision'] != row['document_revision']:
            raise PaymentError('document_changed',
                '单据在中断恢复前已变化；旧任务不属于当前核对版本，未自动附着，请重新核对', 409,
                data=dict(document_id=document['document_id'], revision=document['revision'],
                          task_id=task_id))
        connection.execute("""UPDATE payments.preparations SET task_id=%s,task_revision=%s,
            state='prepared',updated_at=now() WHERE preparation_id=%s""",
            (task_id, task_revision, row['preparation_id']))
        connection.execute("""UPDATE payments.obligations SET state='prepared',
            revision=revision+1,updated_at=now() WHERE obligation_id=%s""", (row['obligation_id'],))
        connection.execute("""UPDATE payments.payment_documents SET state='prepared',
            revision=revision+1,error_code=NULL,updated_at=now() WHERE document_id=%s""",
            (document['document_id'],))
        document = _document(connection, row['document_id'])
        _version(connection, document, 'prepare')
        row = _preparation(connection, row['preparation_id'])
        audit(connection, 'document.prepare_recover', document_id=document['document_id'],
              revision=document['revision'],
              detail=dict(preparation_id=row['preparation_id'], released=False,
                          recovered_task_id=task_id))
        result = _recovery_public(connection, row, status='prepared_recovered', released=False,
                                  verification=verification)
        result['data']['task'] = task
        result['data']['next_actions'] = ['task.read', 'task.verify']
        return result


def recover(principal, session_id, preparation_id, client_request_id):
    """Verify (never blindly cancel) an interrupted preparation, owner-scoped.

    Cross-session is allowed for the same owner; another owner gets 404. Active
    tasks are never cancelled here. Repeated calls are idempotent by state.
    """
    with transaction(principal) as connection:
        preparation = _preparation(connection, preparation_id)
        draft = _draft_state(connection, preparation['draft_reference'])
        # The draft may already know the created task even when the preparation
        # row has not been attached yet (response lost after task creation).
        task_status = _task_status(connection, preparation['task_id']) if preparation['task_id'] else None
        if draft and draft.get('task_id') and task_status is None:
            task_status = _task_status(connection, draft['task_id'])
        state = preparation['state']
        if state == 'cancelled':
            return _recovery_public(connection, preparation, status='cancelled', released=True,
                                    verification=dict(draft_status=draft and draft['status'],
                                                      task_status=task_status, verified_no_task=True))
        if state == 'prepared':
            return _recovery_public(connection, preparation, status='prepared', released=False,
                                    verification=dict(draft_status=draft and draft['status'],
                                                      task_status=task_status, verified_no_task=False))
    if preparation['draft_reference'] is None:
        # Design invariant: a task is only prepared after the draft reference is
        # durably attached, so a preparation without draft reference cannot own a
        # task and touched no funds. This is verified, not assumed from the state.
        return _release(principal, preparation, reason='no_draft_reference',
                        verification=dict(draft_status=None, task_status=None,
                                          verified_no_task=True))
    if draft is None:
        raise PaymentError('preparation_unverifiable',
            '草稿引用存在但原草稿不可读；未取消未修改，请人工核实后再处理', 503,
            data=dict(preparation=preparation_public(preparation),
                      draft_reference=preparation['draft_reference']))
    if draft['status'] == 'preparing':
        # Recovery path of the existing kernel; never re-submit blindly.
        outcome = run_tool(Call(operation='task.recover', reference=draft['reference'],
                                revision=draft['revision']), principal, session_id)
        if outcome.get('status') != 'completed':
            raise PaymentError('payment_preparation_unknown',
                '原付款任务创建状态未知；请保留原草稿引用并稍后再次核实，不重新创建', 503,
                data=dict(preparation=preparation_public(preparation),
                          draft=dict(reference=draft['reference'], status=draft['status'],
                                     revision=draft['revision']),
                          next_actions=['payment.preparation.recover']))
        data = outcome.get('data') if isinstance(outcome.get('data'), dict) else {}
        task = data.get('task')
        if data.get('task_created') and isinstance(task, dict):
            return _finalize_recovered(principal, preparation, draft, task,
                                       dict(draft_status='linked', task_status=task.get('status'),
                                            verified_no_task=False, recovered=True))
        raise PaymentError('preparation_unverifiable',
            '任务恢复未返回可确认的任务；未取消未修改，请人工核实', 503,
            data=dict(preparation=preparation_public(preparation),
                      draft=dict(reference=draft['reference'], status=draft['status'],
                                 revision=draft['revision'])))
    if draft['status'] == 'linked':
        if not draft['task_id']:
            raise PaymentError('preparation_unverifiable',
                '草稿已关联但缺少任务引用；未取消未修改，请人工核实', 503,
                data=dict(preparation=preparation_public(preparation)))
        if task_status in {'cancelled', 'failed'}:
            return _release(principal, preparation, reason='terminal_task',
                            verification=dict(draft_status='linked', task_status=task_status,
                                              verified_no_task=False))
        raise PaymentError('preparation_task_active',
            '中断准备已关联活动任务；恢复接口不代取消，请先读取/取消原任务', 409,
            data=dict(preparation=preparation_public(preparation), task_id=draft['task_id'],
                      next_actions=['task.read', 'task.cancel']))
    if draft['status'] in {'closed'}:
        return _release(principal, preparation, reason='closed_draft',
                        verification=dict(draft_status='closed', task_status=None,
                                          verified_no_task=True))
    if draft['status'] == 'open':
        # Draft exists but task.prepare was never called; close it through the
        # existing non-funds draft contract before releasing the obligation.
        outcome = run_tool(Call(operation='draft.close', reference=draft['reference'],
                                revision=draft['revision']), principal, session_id)
        if outcome.get('status') not in {'completed', 'business_condition'}:
            raise PaymentError('preparation_unverifiable',
                '原草稿无法安全关闭；未取消未修改，请人工核实', 503,
                data=dict(preparation=preparation_public(preparation),
                          draft=dict(reference=draft['reference'], status=draft['status'],
                                     revision=draft['revision'])))
        return _release(principal, preparation, reason='open_draft_no_task',
                        verification=dict(draft_status='closed', task_status=None,
                                          verified_no_task=True))
    raise PaymentError('preparation_unverifiable',
        '草稿状态不可识别；未取消未修改，请人工核实', 503,
        data=dict(preparation=preparation_public(preparation),
                  draft=dict(reference=draft['reference'], status=draft['status'],
                             revision=draft['revision'])))


def _bound_draft_fields(preparation):
    fields = dict(from_card_id=preparation['from_card_id'],
                  recipient_id=preparation['recipient_id'],
                  amount=yuan(preparation['amount_cents']))
    if preparation['remark']:
        fields['remark'] = preparation['remark']
    return fields


def _attach_draft(principal, preparation, draft):
    with transaction(principal, True) as connection:
        row = _preparation(connection, preparation['preparation_id'], lock=True)
        if row['draft_reference'] is None:
            connection.execute("""UPDATE payments.preparations SET draft_reference=%s,
                draft_revision=%s,state='draft_ready',updated_at=now()
                WHERE preparation_id=%s""",
                (draft['reference'], draft['revision'], row['preparation_id']))
        row = _preparation(connection, preparation['preparation_id'])
        return row


def _create_draft(principal, session_id, preparation):
    fields = _bound_draft_fields(preparation)
    outcome = run_tool(Call(operation='draft.create', kind='transfer', fields=fields),
                       principal, session_id)
    if outcome.get('status') != 'completed':
        _tool_error(outcome, operation='转账草稿')
    draft = outcome['data']['draft']
    return _attach_draft(principal, preparation, draft)


def _read_draft(principal, session_id, preparation):
    outcome = run_tool(Call(operation='draft.read', reference=preparation['draft_reference']),
                       principal, session_id)
    if outcome.get('status') not in {'completed', 'saved_incomplete', 'ambiguous',
                                     'invalid_parameters', 'business_condition'}:
        _tool_error(outcome, operation='转账草稿读取')
    return outcome['data']['draft']


def _draft_fields(preparation, draft):
    fields = draft.get('fields') or {}
    return dict(reference=draft['reference'], revision=draft['revision'],
                status=draft['status'], kind='transfer',
                amount=fields.get('amount', yuan(preparation['amount_cents'])),
                from_card_id=fields.get('from_card_id', preparation['from_card_id']),
                recipient_id=fields.get('recipient_id', preparation['recipient_id']),
                remark=fields.get('remark'))


def _prepared_response(connection, preparation, *, replayed, prepared=None, draft=None):
    document = _document(connection, preparation['document_id'])
    data = dict(document=public_document(connection, document),
                preparation=preparation_public(preparation), replayed=replayed)
    if draft is not None:
        data['draft'] = _draft_fields(preparation, draft)
    if prepared is not None:
        data['task'] = prepared
    return dict(contract_version='payment-1.0', data=data)


def _validate_recipient_binding(connection, resolution, document, extraction):
    """Bind a valid resolution to the CURRENT reviewed document payee.

    A resolution being valid for the owner is not enough: it must identify the
    same directory recipient as the current document revision's selected payee.
    Changing the payee therefore requires a new document revision/review first.
    Runs inside the owner-wide write transaction (advisory lock + document row
    lock); a rejection happens before any preparation/draft/task write.
    """
    index = document['selected_payee_index']
    if index is None:
        raise PaymentError('document_payee_required',
            '单据尚未核对收款方，不能准备付款；请先核对并选择收款账户', 409)
    expected = _lookup_selector(connection, DocumentPayeeSelector(
        kind='document_payee', document_id=document['document_id'],
        revision=document['revision'], payee_index=index))
    if expected['status'] != 'resolved' or len(expected['candidates']) != 1:
        raise PaymentError('document_payee_unresolved',
            '当前核对版本的收款方无法唯一解析到可信收款目录；未准备付款，请重新核对收款方', 409,
            data=dict(document_id=document['document_id'], revision=document['revision'],
                      payee_index=index))
    expected_candidate = expected['candidates'][0]
    row = resolution['row']
    selected_id = row['selected_candidate_id']
    if selected_id is None and len(row['candidates'] or []) == 1:
        selected_id = row['candidates'][0]['candidate_id']
    selected = next((item for item in (row['candidates'] or [])
                     if item['candidate_id'] == selected_id), None)
    if selected is None:
        selected = resolution['stored']
    if (selected.get('recipient_id') != expected_candidate.get('recipient_id')
            or selected.get('fingerprint') != expected_candidate.get('fingerprint')):
        raise PaymentError('recipient_document_mismatch',
            '收款解析与当前单据已核对收款方不一致；未准备付款，请按当前核对版本重新解析', 409,
            data=dict(document_id=document['document_id'], revision=document['revision'],
                      expected_masked=expected_candidate.get('masked_account'),
                      selected_masked=selected.get('masked_account')))
    selector = row['selector'] or {}
    if row['selector_kind'] == 'document_payee':
        if (selector.get('document_id') != document['document_id']
                or selector.get('revision') != document['revision']
                or selector.get('payee_index') != index):
            raise PaymentError('recipient_document_mismatch',
                '收款解析绑定的是其他单据版本或收款方；未准备付款，请重新解析', 409,
                data=dict(document_id=document['document_id'], revision=document['revision']))


def _resume(principal, session_id, preparation, client_request_id):
    """Continue an interrupted preparation using its durable references only."""
    if preparation['state'] == 'cancelled':
        raise PaymentError('preparation_cancelled', '该付款准备已取消；请重新核对并使用新的请求键', 409)
    draft = None
    if preparation['draft_reference']:
        draft = _read_draft(principal, session_id, preparation)
    else:
        preparation = _create_draft(principal, session_id, preparation)
        draft = _read_draft(principal, session_id, preparation)
    task_id = draft['fields'].get('task_id') if isinstance(draft.get('fields'), dict) else None
    if draft['status'] == 'closed':
        raise PaymentError('preparation_cancelled', '转账草稿已关闭；请重新核对并使用新的请求键', 409)
    if draft['status'] == 'linked' and task_id:
        from lifecycle.store import TaskStore
        from lifecycle.views import task_response
        from lifecycle.routes import session_for
        try:
            record = TaskStore().load(task_id, principal)
        except Exception:
            raise PaymentError('preparation_not_resumable',
                '草稿已关联任务但任务不可读；请读取任务状态核实', 503,
                data=dict(draft=_draft_fields(preparation, draft), task_id=task_id)) from None
        if record.status in {'cancelled', 'failed'}:
            raise PaymentError('preparation_cancelled',
                '付款任务已取消或失败；请重新核对并使用新的请求键', 409,
                data=dict(draft=_draft_fields(preparation, draft), task_id=task_id))
        prepared = task_response(record, session_for(session_id))
        return _finalize(principal, session_id, preparation, draft, prepared, client_request_id)
    if preparation['state'] == 'prepared' and preparation['task_id']:
        with transaction(principal, True) as connection:
            row = _preparation(connection, preparation['preparation_id'])
            stored = connection.execute('''SELECT result FROM payments.client_requests
                WHERE operation='document.prepare' AND client_request_id=%s''',
                (client_request_id,)).fetchone()
            if stored and stored['result']:
                result = dict(stored['result'])
                result['data'] = dict(result['data'], replayed=True)
                return result
        raise PaymentError('preparation_not_resumable', '付款准备已完成但未找到回执；请读取任务状态', 409,
                           data=dict(preparation=preparation_public(preparation)))
    if draft['status'] == 'preparing':
        outcome = run_tool(Call(operation='task.recover', reference=draft['reference'],
                                revision=draft['revision']), principal, session_id)
        if outcome.get('status') == 'commit_unknown':
            raise PaymentError('payment_preparation_unknown',
                '付款任务创建状态未知；请使用原草稿引用核实，不重复创建', 503,
                data=dict(submission_unknown=True, draft=_draft_fields(preparation, draft)))
        if outcome.get('status') != 'completed':
            _tool_error(outcome, operation='付款任务恢复')
        prepared = outcome['data'].get('prepared') or outcome['data']
        return _finalize(principal, session_id, preparation, draft, prepared, client_request_id)
    outcome = run_tool(Call(operation='task.prepare', reference=draft['reference'],
                            revision=draft['revision']), principal, session_id)
    if outcome.get('status') == 'commit_unknown':
        raise PaymentError('payment_preparation_unknown',
            '付款任务创建状态未知；请使用原草稿引用核实，不重复创建', 503,
            data=dict(submission_unknown=True, draft=_draft_fields(preparation, draft)))
    if outcome.get('status') != 'completed':
        raise _preparation_retryable(outcome, preparation, draft)
    prepared = outcome['data']['prepared']
    return _finalize(principal, session_id, preparation, draft, prepared, client_request_id)


def _preparation_retryable(outcome, preparation, draft):
    error = PaymentError(outcome.get('code') or 'task_prepare_failed',
        '付款任务未创建（%s）；草稿与单据保留，可处理后使用原请求键重试' % (outcome.get('code') or ''),
        TOOL_STATUS.get(outcome.get('status'), 409),
        data=dict(draft=_draft_fields(preparation, draft),
                  preparation=preparation_public(preparation),
                  next_actions=['task.read', 'task.cancel', 'document.prepare']))
    error.retryable = True
    return error


def _finalize(principal, session_id, preparation, draft, prepared, client_request_id):
    task_id = prepared['task']['task']['task_id']
    task_revision = prepared['task']['revision']
    with transaction(principal, True) as connection:
        row = _preparation(connection, preparation['preparation_id'], lock=True)
        document = _document(connection, row['document_id'], lock=True)
        if document['revision'] != row['document_revision']:
            raise PaymentError('document_changed',
                '单据在准备过程中已变化；旧授权失效，请重新核对', 409,
                data=dict(document_id=document['document_id'], revision=document['revision']))
        if row['state'] == 'prepared':
            stored = connection.execute('''SELECT result FROM payments.client_requests
                WHERE operation='document.prepare' AND client_request_id=%s''',
                (client_request_id,)).fetchone()
            if stored and stored['result']:
                result = dict(stored['result'])
                result['data'] = dict(result['data'], replayed=True)
                return result
        connection.execute("""UPDATE payments.preparations SET task_id=%s,task_revision=%s,
            state='prepared',updated_at=now() WHERE preparation_id=%s""",
            (task_id, task_revision, row['preparation_id']))
        connection.execute("""UPDATE payments.obligations SET state='prepared',revision=revision+1,
            updated_at=now() WHERE obligation_id=%s""", (row['obligation_id'],))
        connection.execute("""UPDATE payments.payment_documents SET state='prepared',
            revision=revision+1,error_code=NULL,updated_at=now() WHERE document_id=%s""",
            (document['document_id'],))
        document = _document(connection, row['document_id'])
        _version(connection, document, 'prepare')
        row = _preparation(connection, row['preparation_id'])
        audit(connection, 'document.prepare', document_id=document['document_id'],
              revision=document['revision'],
              detail=dict(preparation_id=row['preparation_id'], task_id=task_id,
                          obligation_id=row['obligation_id'], amount_cents=row['amount_cents']))
        result = _prepared_response(connection, row, replayed=False, prepared=prepared, draft=draft)
        finish_request(connection, 'document.prepare', client_request_id,
                       row['preparation_id'], result)
        return result


def prepare(principal, session_id, document_id, body):
    payload = dict(document_id=document_id, **body.model_dump(mode='json'))
    # --- Phase 1: validate + durably reserve one preparation. -----------------
    with transaction(principal, True) as connection:
        existing_request = connection.execute('''SELECT * FROM payments.client_requests
            WHERE operation='document.prepare' AND client_request_id=%s''',
            (body.client_request_id,)).fetchone()
        if existing_request is not None:
            if existing_request['request_hash'] != sha256_json(payload):
                raise PaymentError('client_request_conflict',
                    '同一请求键已绑定不同参数；未重复执行，请使用新的请求键', 409)
            if existing_request['result']:
                result = dict(existing_request['result'])
                result['data'] = dict(result['data'], replayed=True)
                return result
            preparation = _preparation(connection, existing_request['resource_id'])
        else:
            preparation = None
        if preparation is None:
            document = _document(connection, document_id, lock=True)
            if document['revision'] != body.revision:
                raise PaymentError('document_revision_conflict',
                    '单据版本已变化（当前 revision=%d）；请重新读取并核对' % document['revision'], 409)
            extraction = _extraction(document)
            digest = _digest(document, extraction)
            if body.digest != digest:
                raise PaymentError('document_digest_mismatch',
                    '核对摘要已失效；请重新读取当前单据后再准备', 409)
            early = _active_preparation(connection, document_id=document_id, lock=True)
            if early is not None:
                early_status = _task_status(connection, early['task_id'])
                if early_status == 'completed':
                    raise PaymentError('payment_obligation_already_paid',
                        '该单据已有已完成的付款任务，禁止再次准备付款', 409,
                        data=dict(preparation=preparation_public(early)))
                if early['task_id']:
                    if early_status in TERMINAL_TASK:
                        # Verified terminal task: releasing the stale preparation
                        # cannot duplicate money; no unknown task is abandoned.
                        connection.execute("""UPDATE payments.preparations SET state='cancelled',
                            updated_at=now() WHERE preparation_id=%s""", (early['preparation_id'],))
                        connection.execute("""UPDATE payments.obligations SET state='open',
                            revision=revision+1,updated_at=now() WHERE obligation_id=%s""",
                            (early['obligation_id'],))
                    elif early_status is None:
                        raise PaymentError('preparation_unverifiable',
                            '准备记录绑定的任务不可读；未取消未重新准备，请先核实', 503,
                            data=dict(preparation=preparation_public(early), task_id=early['task_id']))
                    else:
                        raise PaymentError('payment_obligation_already_prepared',
                            '同一单据已有进行中的付款任务，禁止重复准备；请先处理原任务', 409,
                            data=dict(preparation=preparation_public(early)))
                else:
                    # preparing/draft_ready without a task must be verified by the
                    # recovery contract; a different key never cancels it directly.
                    raise _recovery_required(connection, early)
            if document['state'] != 'ready':
                raise PaymentError('document_not_ready',
                    '单据尚未完成核对或已准备过；请读取当前状态', 409,
                    data=dict(state=document['state'], blocking=readiness(
                        extraction, document['selected_item_ids'] or [],
                        document['selected_payee_index'])))
            if sorted(body.item_ids) != sorted(document['selected_item_ids'] or []):
                raise PaymentError('selection_changed',
                    '所选缴费项目与已核对选择不一致；请重新核对', 409)
            blockers = readiness(extraction, document['selected_item_ids'] or [],
                                 document['selected_payee_index'])
            if blockers:
                raise PaymentError('payment_document_blocked',
                    '单据仍有未解决疑点，不能准备付款：' + '、'.join(blockers), 422,
                    data=dict(blocking=blockers))
            amount_cents = selected_amount_cents(extraction, document['selected_item_ids'] or [])
            if amount_cents is None:
                raise PaymentError('amount_unresolved', '所选项目金额未明确，不能准备付款', 422)
            obligation = _obligation_for(connection, document_id)
            if obligation is None:
                raise PaymentError('obligation_missing', '该单据尚无稳定缴费义务标识，不能准备付款', 422)
            if obligation['kind'] != 'payment_notice':
                raise PaymentError('kind_not_payable',
                    '只有缴费通知单可以准备付款；发票/收据/回单只能用于核对关联', 422)
            if obligation['amount_cents'] != amount_cents:
                raise PaymentError('obligation_amount_conflict',
                    '已登记缴费义务金额与本次选择不一致，未准备付款', 409)
            resolution = resolve_selected(connection, body.recipient_resolution_id)
            _validate_recipient_binding(connection, resolution, document, extraction)
            recipient = resolution['recipient']
            card = connection.execute('SELECT public_id,status FROM core.cards WHERE public_id=%s',
                                      (body.from_card_id,)).fetchone()
            if card is None:
                raise PaymentError('card_not_found', '银行卡不存在', 404)
            if card['status'] != 'active':
                raise PaymentError('card_unavailable', '当前银行卡状态不允许转账', 422)
            active = _active_preparation(connection, obligation_id=obligation['obligation_id'], lock=True)
            if active is not None:
                task_status = _task_status(connection, active['task_id'])
                if task_status == 'completed':
                    raise PaymentError('payment_obligation_already_paid',
                        '该缴费义务已有已完成的付款任务，禁止再次准备付款', 409,
                        data=dict(preparation=preparation_public(active)))
                if active['task_id']:
                    if task_status in TERMINAL_TASK:
                        connection.execute("""UPDATE payments.preparations SET state='cancelled',
                            updated_at=now() WHERE preparation_id=%s""", (active['preparation_id'],))
                        connection.execute("""UPDATE payments.obligations SET state='open',
                            revision=revision+1,updated_at=now() WHERE obligation_id=%s""",
                            (obligation['obligation_id'],))
                    elif task_status is None:
                        raise PaymentError('preparation_unverifiable',
                            '准备记录绑定的任务不可读；未取消未重新准备，请先核实', 503,
                            data=dict(preparation=preparation_public(active), task_id=active['task_id']))
                    else:
                        raise PaymentError('payment_obligation_already_prepared',
                            '同一缴费义务已有进行中的付款任务，禁止重复准备；请先处理原任务', 409,
                            data=dict(preparation=preparation_public(active)))
                else:
                    # Do not cancel/reopen an unverified interrupted preparation.
                    raise _recovery_required(connection, active)
            preparation_id = 'pp_' + sha256_json([body.client_request_id, document_id])[:32]
            connection.execute('''INSERT INTO payments.preparations
                (preparation_id,client_request_id,document_id,document_revision,obligation_id,
                 item_ids,amount_cents,recipient_resolution_id,candidate_id,recipient_id,
                 recipient_fingerprint,from_card_id,remark)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (preparation_id, body.client_request_id, document_id, document['revision'],
                 obligation['obligation_id'], Jsonb(sorted(body.item_ids)), amount_cents,
                 body.recipient_resolution_id, resolution['stored']['candidate_id'],
                 recipient['recipient_id'], recipient_fingerprint(recipient),
                 body.from_card_id, body.remark))
            begin_request(connection, 'document.prepare', body.client_request_id, payload)
            finish_request(connection, 'document.prepare', body.client_request_id, preparation_id)
            audit(connection, 'document.prepare_reserved', document_id=document_id,
                  revision=document['revision'],
                  detail=dict(preparation_id=preparation_id,
                              obligation_id=obligation['obligation_id'],
                              recipient_id=recipient['recipient_id']))
            preparation = _preparation(connection, preparation_id)
    # --- Phase 2: existing draft/task kernel. --------------------------------
    return _resume(principal, session_id, preparation, body.client_request_id)


def read_preparation(principal, preparation_id):
    """Read-only preparation view with the recovery contract summary."""
    with transaction(principal) as connection:
        preparation = _preparation(connection, preparation_id)
        return dict(preparation_public(preparation),
                    recovery=recovery_summary(connection, preparation))
