"""Payment-1.0 service adapter for the formal dialogue tools (C public wiring).

This module is the dependency boundary: it may import ``payments.*`` and is
imported by the application layer, never by ``dialogue.*``. At import time it
registers one executor with the dialogue tool catalog; the dialogue gate then
sees no forbidden dependency edges.

Safety boundary: only read/prepare tools are exposed; no execution/confirm tool
exists. The model never receives full account numbers, phone numbers, passwords,
TOTP/confirmation codes, confirmation phrases or image bytes.
"""
from __future__ import annotations

from foundation.contracts import feedback

from dialogue import payment_tools
from dialogue.projection import bounded_result


def _error(exc):
    status = getattr(exc, 'status', 500)
    code = getattr(exc, 'code', 'payment_service_unavailable')
    data = getattr(exc, 'data', None)
    if code in {'preparation_recovery_required', 'preparation_task_active',
                'preparation_state_mismatch', 'preparation_unverifiable',
                'payment_preparation_recovery_required'}:
        merged = dict(data) if isinstance(data, dict) else {}
        merged.setdefault('recovery', (data or {}).get('recovery')
                          if isinstance(data, dict) else None)
        return feedback('conflict', code, data=merged,
                        next_actions=['payment_preparation_read', 'payment_document_read',
                                      'payment.preparation.recover'])
    if status == 409:
        return feedback('conflict', code, data=data)
    if status == 404:
        return feedback('not_found', code, data=data)
    if status in {401, 403}:
        return feedback('forbidden', code, data=data)
    if status == 503:
        return feedback('unavailable', code, data=data)
    return feedback('service_failure', code, data=data)


def execute(name, args, principal, session_id):
    from payments.errors import PaymentError
    try:
        if name == 'payment_document_list':
            import payments.documents as documents
            parsed = payment_tools.DocumentList.model_validate(args)
            result = documents.list_documents(principal)
            omitted = bool(result.get('has_more')) or len(result['documents']) > parsed.limit
            result['documents'] = result['documents'][:parsed.limit]
            result['truncated'] = omitted
            result['has_more'] = omitted
            return bounded_result(feedback(data=result, next_actions=['payment_document_read']))
        if name == 'payment_document_read':
            import payments.documents as documents
            parsed = payment_tools.DocumentRef.model_validate(args)
            return bounded_result(feedback(
                data=payment_tools._compact(documents.get_document(principal, parsed.document_id)),
                next_actions=['payment_resolve_document_payee', 'payment_preparation_read']))
        if name == 'payment_resolve_document_payee':
            import payments.documents as documents
            from payments.models import DocumentPayeeSelector, ResolutionRequest
            from payments.service import create_resolution
            parsed = payment_tools.ResolvePayee.model_validate(args)
            current = documents.get_document(principal, parsed.document_id)
            if current.get('revision') != parsed.revision:
                raise PaymentError('document_revision_conflict',
                                   '单据版本已变化，请重新读取当前版本后再解析收款方', 409)
            body = ResolutionRequest(
                client_request_id=payment_tools._derived_request_id(name, args),
                selector=DocumentPayeeSelector(kind='document_payee',
                                               document_id=parsed.document_id,
                                               revision=parsed.revision,
                                               payee_index=parsed.payee_index))
            result = create_resolution(principal, body)
            resolution = dict(result.get('resolution') or {})
            resolution['replayed'] = bool(result.get('replayed'))
            resolution['scope'] = ('recipient resolution reference only; candidates are not selected '
                                   'and full account numbers are never returned')
            return bounded_result(feedback(data=resolution,
                                           next_actions=['payment_resolution_read',
                                                         'payment_prepare']))
        if name == 'payment_resolution_read':
            from payments.service import get_resolution
            parsed = payment_tools.ResolutionRef.model_validate(args)
            result = get_resolution(principal, parsed.resolution_id)
            resolution = dict(result.get('resolution') or {})
            resolution['scope'] = ('recipient resolution reference/evidence only; not selected, '
                                   'not payment authorization')
            return bounded_result(feedback(data=resolution))
        if name == 'payment_preparation_read':
            import payments.prepare as prepare
            parsed = payment_tools.PreparationRef.model_validate(args)
            return bounded_result(feedback(
                data=prepare.read_preparation(principal, parsed.preparation_id),
                next_actions=['payment_document_read', 'task_read']))
        if name == 'payment_prepare':
            import payments.documents as documents
            import payments.prepare as prepare
            from payments.models import PreparePaymentRequest
            parsed = payment_tools.Prepare.model_validate(args)
            current = documents.get_document(principal, parsed.document_id)
            if current.get('revision') != parsed.revision or current.get('digest') != parsed.digest:
                raise PaymentError('document_changed',
                                   '单据版本或核对摘要已变化；请重新读取并再次核对，旧授权不得复用', 409)
            active = [item for item in (current.get('preparations') or [])
                      if item.get('state') in payment_tools.ACTIVE_PREPARATION_STATES
                      and item.get('task_status') not in payment_tools.TERMINAL_TASK_STATES]
            if not active and isinstance(current.get('preparation'), dict):
                single = current['preparation']
                if single.get('state') in payment_tools.ACTIVE_PREPARATION_STATES | {'prepared'}:
                    active = [single]
            if active:
                # Original request identities are authoritative: verify the
                # existing preparation first; never silently re-key the payment.
                return bounded_result(feedback(
                    'conflict', 'payment_preparation_recovery_required',
                    data=dict(document_id=parsed.document_id, preparation=active[0],
                              scope='existing preparation must be read/verified; '
                                    'do not create a second one'),
                    next_actions=['payment_preparation_read', 'payment_document_read']))
            body = PreparePaymentRequest(
                client_request_id=payment_tools._derived_request_id(name, args),
                revision=parsed.revision, digest=parsed.digest,
                item_ids=parsed.item_ids,
                recipient_resolution_id=parsed.recipient_resolution_id,
                from_card_id=parsed.from_card_id, remark=parsed.remark)
            result = prepare.prepare(principal, session_id, parsed.document_id, body)
            raw = result.get('data') if isinstance(result, dict) else {}
            task = raw.get('task') if isinstance(raw, dict) else {}
            task = task if isinstance(task, dict) else {}
            task_view = task.get('task') if isinstance(task.get('task'), dict) else task
            nested_task = (task_view.get('task')
                           if isinstance(task_view.get('task'), dict) else {})
            task_id = (nested_task.get('task_id') or (task_view or {}).get('task_id'))
            task_revision = (task_view.get('revision') or task.get('revision'))
            task_status = ((task_view or {}).get('status') or nested_task.get('status'))
            preview = task.get('preview') if isinstance(task.get('preview'), dict) else None
            # Confirmation material stays in the UI; the model only sees
            # references and the required authentication requirement.
            summary = dict(
                document=raw.get('document') or {},
                preparation=raw.get('preparation') or {},
                draft=raw.get('draft') or {},
                replayed=bool(raw.get('replayed')),
                task=dict(task_id=task_id,
                          revision=task_revision,
                          status=task_status,
                          preview=(dict(preview_id=preview.get('preview_id'),
                                        required_authentication=preview.get('required_authentication'),
                                        permission_level=preview.get('permission_level'),
                                        expires_at=preview.get('expires_at'))
                                   if preview else None)),
                scope=('preparation only: no payment executed; confirm in the secure task UI '
                       '(TOTP when required); no confirmation phrase is exposed to the model'))
            return bounded_result(feedback(data=summary,
                                           next_actions=['task_read', 'secure confirmation UI']))
    except PaymentError as exc:
        return _error(exc)
    except PermissionError:
        return feedback('unsupported', 'payment_trusted_turn_required')
    except Exception:
        return feedback('service_failure', 'payment_service_unavailable',
                        next_actions=['payment_document_read', 'payment_preparation_read'])
    return feedback('unsupported', 'unknown_tool')


def _payment_target(principal, payment):
    """Re-read the CURRENT server-side payment target for an unfinished goal."""
    from payments.documents import get_document
    from payments.prepare import read_preparation
    target = dict(scope='server-current payment target; task is authoritative; not authorization')
    document_id = payment.get('document_id')
    preparation_id = payment.get('preparation_id')
    if document_id:
        document = get_document(principal, document_id)
        target['document'] = dict(
            document_id=document['document_id'], revision=document.get('revision'),
            state=document.get('state'), kind=document.get('kind'),
            ready=document.get('ready'), blocking=document.get('blocking'),
            selected_item_ids=document.get('selected_item_ids'),
            selected_payee_index=document.get('selected_payee_index'))
    if preparation_id:
        preparation = read_preparation(principal, preparation_id)
        recovery = preparation.get('recovery') or {}
        target['preparation'] = dict(
            preparation_id=preparation.get('preparation_id'),
            state=preparation.get('state'),
            task_status=recovery.get('task_status') or preparation.get('task_status'),
            required=recovery.get('required'))
        target['amount_cents'] = preparation.get('amount_cents')
        target['document_id'] = preparation.get('document_id')
    return target


payment_tools.register_executor(execute)
payment_tools.register_context_resolver(_payment_target)
