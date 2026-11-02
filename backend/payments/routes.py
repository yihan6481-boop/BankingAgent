"""payment-1.0 HTTP surface on the existing authenticated secure router.

Registration currently happens from ``billvision.routes`` (an A-owned module
already imported by ``api/app.py``) so this slice needs no edit to the shared
application entry point. C can move the import into ``api/app.py`` later.
"""
from __future__ import annotations

import functools

from fastapi import Depends, Query as HttpQuery, Request, Response
from fastapi.responses import JSONResponse

from api.runtime import runtime
from api.secure import router
from billvision.provider import MAX_BYTES
from lifecycle.routes import require_task_principal, session_for
from security.schemas import Principal

from . import documents, prepare, reconcile, service
from .errors import PaymentError
from .models import (CLIENT_REQUEST_ID, ContactCreateRequest, ContactUpdateRequest,
                     DocumentRevisionRequest, PreparationRecoverRequest,
                     PreparePaymentRequest, ReconcileRequest,
                     ResolutionRequest, ResolutionSelectRequest, ReviewRequest,
                     TransactionLinkRequest)
from .service import transaction


def ok(data):
    return {'contract_version': 'payment-1.0', 'data': data}


def _problem(exc):
    content = {'code': exc.code, 'message': exc.message}
    if exc.data:
        content['data'] = exc.data
    return JSONResponse(status_code=exc.status, content=content,
                        headers={'Cache-Control': 'no-store'})


def payment_route(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except PaymentError as exc:
            return _problem(exc)
        except Exception:
            # Unexpected failures are reported without leaking private values.
            import logging
            logging.getLogger('payments').exception('payment endpoint failure')
            return _problem(PaymentError('payment_internal_error',
                                         '缴费单据服务内部错误；未完成的操作可读取当前状态核实', 500))
    return wrapper


# ---------------------------------------------------------------------------
# Contacts.
# ---------------------------------------------------------------------------

@router.get('/contacts')
@payment_route
def list_contacts(session_id: str, principal: Principal = Depends(require_task_principal)):
    return ok(service.list_contacts(principal))


@router.post('/contacts')
@payment_route
def create_contact(session_id: str, body: ContactCreateRequest,
                   principal: Principal = Depends(require_task_principal)):
    return ok(service.create_contact(principal, body))


@router.get('/contacts/{contact_id}')
@payment_route
def get_contact(session_id: str, contact_id: str,
                principal: Principal = Depends(require_task_principal)):
    return ok(dict(contact=service.get_contact(principal, contact_id)))


@router.patch('/contacts/{contact_id}')
@payment_route
def update_contact(session_id: str, contact_id: str, body: ContactUpdateRequest,
                   principal: Principal = Depends(require_task_principal)):
    return ok(service.update_contact(principal, contact_id, body))


# ---------------------------------------------------------------------------
# Recipient resolution.
# ---------------------------------------------------------------------------

@router.post('/recipient-resolutions')
@payment_route
def create_resolution(session_id: str, body: ResolutionRequest,
                      principal: Principal = Depends(require_task_principal)):
    return ok(service.create_resolution(principal, body))


@router.get('/recipient-resolutions/{resolution_id}')
@payment_route
def get_resolution(session_id: str, resolution_id: str,
                   principal: Principal = Depends(require_task_principal)):
    return ok(service.get_resolution(principal, resolution_id))


@router.post('/recipient-resolutions/{resolution_id}/select')
@payment_route
def select_candidate(session_id: str, resolution_id: str, body: ResolutionSelectRequest,
                     principal: Principal = Depends(require_task_principal)):
    return ok(service.select_candidate(principal, resolution_id, body))


# ---------------------------------------------------------------------------
# Payment documents.
# ---------------------------------------------------------------------------

@router.post('/payment-documents')
@payment_route
async def upload_payment_document(session_id: str, request: Request,
                                  client_request_id: str = HttpQuery(pattern=CLIENT_REQUEST_ID),
                                  principal: Principal = Depends(require_task_principal)):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BYTES:
            raise PaymentError('image_size_unsupported', '图片须在10MiB以内', 413)
    from starlette.concurrency import run_in_threadpool
    value = await run_in_threadpool(documents.upload, principal, bytes(data), client_request_id)
    return ok(value)


@router.get('/payment-documents')
@payment_route
def list_payment_documents(session_id: str, principal: Principal = Depends(require_task_principal)):
    return ok(documents.list_documents(principal))


@router.get('/payment-documents/{document_id}')
@payment_route
def payment_document(session_id: str, document_id: str,
                     principal: Principal = Depends(require_task_principal)):
    return ok(documents.get_document(principal, document_id))


@router.get('/payment-documents/{document_id}/image')
@payment_route
def payment_document_image(session_id: str, document_id: str,
                           principal: Principal = Depends(require_task_principal)):
    raw, mime = documents.image(principal, document_id)
    return Response(raw, media_type=mime,
                    headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


@router.get('/payment-documents/{document_id}/versions')
@payment_route
def payment_document_versions(session_id: str, document_id: str,
                              principal: Principal = Depends(require_task_principal)):
    return ok(dict(versions=documents.versions(principal, document_id)))


@router.post('/payment-documents/{document_id}/extract')
@payment_route
def extract_payment_document(session_id: str, document_id: str, body: DocumentRevisionRequest,
                             principal: Principal = Depends(require_task_principal)):
    return ok(documents.extract(principal, document_id, body.revision, body.client_request_id))


@router.put('/payment-documents/{document_id}/review')
@payment_route
def review_payment_document(session_id: str, document_id: str, body: ReviewRequest,
                            principal: Principal = Depends(require_task_principal)):
    with runtime.session_lock(session_id):
        # Validation (owner/revision/digest/replay/business) happens before any
        # side effect; invalid or replayed requests never cancel a task.
        plan = documents.review_preflight(principal, document_id, body)
        if plan['replay'] is not None:
            replayed = dict(plan['replay'])
            replayed['replayed'] = True
            replayed['cancelled_task_ids'] = []
            return ok(replayed)
        cancelled = documents.cancel_preparation_task(
            session_id, principal, document_id,
            expected_preparation_id=plan['preparation_id'],
            expected_document_revision=plan['expected_revision'])
        value = documents.review(principal, document_id, body,
                                 expected_preparation_id=plan['preparation_id'])
    value['cancelled_task_ids'] = cancelled
    return ok(value)


@router.post('/payment-documents/{document_id}/prepare-payment')
@payment_route
def prepare_payment(session_id: str, document_id: str, body: PreparePaymentRequest,
                    principal: Principal = Depends(require_task_principal)):
    session_for(session_id)
    with runtime.session_lock(session_id):
        return prepare.prepare(principal, session_id, document_id, body)


@router.get('/payment-preparations/{preparation_id}')
@payment_route
def read_preparation(session_id: str, preparation_id: str,
                     principal: Principal = Depends(require_task_principal)):
    return ok(dict(preparation=prepare.read_preparation(principal, preparation_id)))


@router.post('/payment-preparations/{preparation_id}/recover')
@payment_route
def recover_preparation(session_id: str, preparation_id: str, body: PreparationRecoverRequest,
                        principal: Principal = Depends(require_task_principal)):
    session_for(session_id)
    with runtime.session_lock(session_id):
        return prepare.recover(principal, session_id, preparation_id, body.client_request_id)


@router.post('/payment-documents/{document_id}/reconcile')
@payment_route
def reconcile_document(session_id: str, document_id: str, body: ReconcileRequest,
                       principal: Principal = Depends(require_task_principal)):
    return ok(reconcile.reconcile(principal, document_id, body))


@router.post('/payment-documents/{document_id}/transaction-link')
@payment_route
def link_transaction(session_id: str, document_id: str, body: TransactionLinkRequest,
                     principal: Principal = Depends(require_task_principal)):
    return ok(reconcile.link(principal, document_id, body))


@router.delete('/payment-documents/{document_id}')
@payment_route
def delete_payment_document(session_id: str, document_id: str,
                            revision: int = HttpQuery(ge=1),
                            principal: Principal = Depends(require_task_principal)):
    return ok(documents.delete(principal, document_id, revision))


@router.get('/payment-vision/status')
@payment_route
def payment_vision_status(session_id: str, principal: Principal = Depends(require_task_principal)):
    import vision_gate
    with transaction(principal):
        pass  # migration/identity precheck before reading the gate snapshot
    return ok(vision_gate.snapshot(principal))
