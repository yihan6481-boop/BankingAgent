"""Formal dialogue tool catalog for the payment pipeline (C public wiring).

This module is part of the dialogue dependency gate: it must NOT import
``payments.*``, ``api.*`` or ``agent.*``. The actual payment service adapter is
registered at application startup from ``api.payment_dialogue`` (dependency
inversion), so the dialogue layer only knows tool names/schemas and a handler
registry.

Boundary:

* no fund execution tool exists; ``payment_prepare`` only reuses the existing
  draft/task kernel and returns a preview that must go through the UI
  confirmation/TOTP chain;
* the model may only pass document/resolution/preparation references plus a
  card id and selected item ids. Full account numbers, phone numbers, passwords,
  TOTP/confirmation codes and image bytes are never accepted;
* ``client_request_id`` is derived server-side from the trusted turn/call id and
  canonical arguments, so a retried identical call is idempotent while the model
  never persists nor invents request keys;
* an unresolved/interrupted preparation is never silently re-keyed: the tool
  returns the recovery reference and requires the original preparation to be
  read/verified first.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from foundation.contracts import feedback
from .projection import bounded_result


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


PAYMENT_READ_NAMES = {
    'payment_document_list', 'payment_document_read',
    'payment_resolution_read', 'payment_preparation_read',
}
PAYMENT_WRITE_NAMES = {'payment_resolve_document_payee', 'payment_prepare'}
PAYMENT_TOOL_NAMES = PAYMENT_READ_NAMES | PAYMENT_WRITE_NAMES
ACTIVE_PREPARATION_STATES = {'preparing', 'draft_ready'}
TERMINAL_TASK_STATES = {'completed', 'cancelled', 'failed'}

# Populated once by api.payment_dialogue at application startup (or by tests).
_HANDLERS = {}

_CONTEXT_RESOLVER = {}


def register_context_resolver(resolver):
    """Register the payment service read-through used by dialogue state.

    Kept behind the same inversion boundary as the executor: dialogue must not
    import ``payments.*`` directly.
    """
    _CONTEXT_RESOLVER['resolve'] = resolver


def register_executor(executor):
    """Register the payment-service adapter without importing it here."""
    _HANDLERS['execute'] = executor


def registered() -> bool:
    return 'execute' in _HANDLERS


def _derived_request_id(name, args):
    from .links import binding
    scope = binding.get()
    if not scope:
        raise PermissionError('payment_trusted_turn_required')
    _cid, tid, call_id = scope
    payload = json.dumps([name, args], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return 'pi_' + hashlib.sha256((tid + '|' + call_id + '|' + payload).encode()).hexdigest()[:40]


class DocumentList(StrictModel):
    limit: int = Field(8, ge=1, le=30, strict=True)


class DocumentRef(StrictModel):
    document_id: str = Field(pattern=r'^pd_[0-9a-f]{32}$')


class ResolvePayee(DocumentRef):
    revision: int = Field(ge=1, strict=True)
    payee_index: int = Field(0, ge=0, le=4, strict=True)


class ResolutionRef(StrictModel):
    resolution_id: str


class PreparationRef(StrictModel):
    preparation_id: str


class Prepare(DocumentRef):
    revision: int = Field(ge=1, strict=True)
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    item_ids: list[str] = Field(min_length=1, max_length=20)
    recipient_resolution_id: str = Field(pattern=r'^rr_[0-9a-f]{32}$')
    from_card_id: int = Field(gt=0, strict=True)
    remark: str | None = Field(default=None, max_length=100)


DESCRIPTIONS = {
    'payment_document_list': (
        DocumentList,
        '列出本人单据；不代表已付款。'),
    'payment_document_read': (
        DocumentRef,
        '读取单据当前版本；不执行付款。'),
    'payment_resolve_document_payee': (
        ResolvePayee,
        '解析单据收款方；不付款。'),
    'payment_resolution_read': (
        ResolutionRef,
        '读取解析状态与候选；不付款。'),
    'payment_preparation_read': (
        PreparationRef,
        '读取准备/草稿/任务；不执行付款。'),
    'payment_prepare': (
        Prepare,
        '只建草稿与任务预览，不执行。'),
}


def _compact(document):
    if not isinstance(document, dict):
        return document
    keep = ['document_id', 'revision', 'state', 'kind', 'digest', 'created_at', 'updated_at',
            'fields', 'selected_item_ids', 'selected_payee_index', 'blocking', 'ready',
            'verification', 'preparation', 'preparations', 'links', 'repeated_upload']
    value = {key: document[key] for key in keep if key in document}
    value['scope'] = ('current payment document state; masked references only; '
                      'confirmed bank receipt/task is the only payment-success evidence')
    return value


def _task_reference(task):
    """Return (task_id, revision) from the raw task response or projected summary."""
    if not isinstance(task, dict):
        return None, None
    if task.get('task_id'):
        return task['task_id'], task.get('revision')
    view = task.get('task') if isinstance(task.get('task'), dict) else None
    if view is None:
        return None, task.get('revision')
    if view.get('task_id'):
        return view['task_id'], view.get('revision') or task.get('revision')
    nested = view.get('task') if isinstance(view.get('task'), dict) else None
    if nested and nested.get('task_id'):
        return nested['task_id'], view.get('revision') or task.get('revision')
    return None, view.get('revision') or task.get('revision')


def references(name, args, result):
    """Only the controlled task of a completed preparation is a dialogue link."""
    if name != 'payment_prepare' or not isinstance(result, dict):
        return []
    if result.get('status') != 'completed':
        return []
    data = result.get('data')
    if not isinstance(data, dict):
        return []
    task_id, _revision = _task_reference(data.get('task'))
    return [task_id] if task_id else []


def context_for(name, args, result):
    """Durable payment-target context for an unfinished payment goal.

    Contains references only. Current document/preparation state is re-read from
    the server when dialogue state is assembled; this value is not authorization.
    """
    refs = references(name, args, result)
    if not refs:
        return None
    data = result.get('data') if isinstance(result, dict) else None
    data = data if isinstance(data, dict) else {}
    document = data.get('document') if isinstance(data.get('document'), dict) else {}
    preparation = data.get('preparation') if isinstance(data.get('preparation'), dict) else {}
    payment = {key: value for key, value in dict(
        document_id=document.get('document_id'),
        preparation_id=preparation.get('preparation_id')).items() if value}
    return dict(task_id=refs[0], payment=payment)


def payment_target(principal, payment):
    resolver = _CONTEXT_RESOLVER.get('resolve')
    if resolver is None or not isinstance(payment, dict):
        return None
    try:
        return resolver(principal, payment)
    except Exception:
        # A target that cannot be re-read is simply omitted; never fabricated.
        return None


def execute(name, args, principal, session_id):
    if name not in DESCRIPTIONS:
        return feedback('unsupported', 'unknown_tool')
    executor = _HANDLERS.get('execute')
    if executor is None:
        return feedback('service_failure', 'payment_service_unavailable',
                        next_actions=['payment_document_read', 'payment_preparation_read'])
    return executor(name, args, principal, session_id)
