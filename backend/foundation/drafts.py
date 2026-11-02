"""Structured, non-authorizing goal tools. No natural language routing."""
from copy import deepcopy
import json
import logging
import sqlite3
from uuid import uuid4
from typing import Literal

from pydantic import Field, ValidationError
from agent.schemas import StrictModel, TransferMoneyArguments
from data_query import QueryPlan, QueryBatch, verified_execute
from lifecycle.store import db, digest, TaskError

log = logging.getLogger(__name__)


class TransferDraft(StrictModel):
    from_card_id: int | None = Field(None, gt=0, strict=True)
    recipient_label: str | None = Field(None, min_length=1, max_length=100)
    recipient_id: int | None = Field(None, gt=0, strict=True)
    amount: str | None = Field(None, max_length=32)
    remark: str | None = Field(None, max_length=100)


class GoalStore:
    def load(self, principal, reference):
        with db() as c:
            row = c.execute('SELECT * FROM tool_goals WHERE reference=? AND owner_id=? AND principal_id=?',
                (reference, principal.user_id, principal.principal_id)).fetchone()
        if not row:
            raise TaskError('reference_not_found', status=404)
        value = {k: row[k] for k in ['reference','owner_id','principal_id','kind','status','revision']}
        value['fields'] = json.loads(row['payload'])
        if digest(value) != row['checksum']:
            raise TaskError('invalid_storage')
        return value

    def save(self, principal, kind, fields, *, previous=None, status='open'):
        value = dict(reference=previous['reference'] if previous else 'tg_'+uuid4().hex,
            owner_id=principal.user_id, principal_id=principal.principal_id, kind=kind,
            status=status, revision=previous['revision']+1 if previous else 1, fields=fields)
        with db() as c:
            if previous:
                n = c.execute('UPDATE tool_goals SET status=?,revision=?,payload=?,checksum=? '
                    'WHERE reference=? AND revision=? AND owner_id=? AND principal_id=?',
                    (status,value['revision'],json.dumps(fields),digest(value),value['reference'],
                     previous['revision'],principal.user_id,principal.principal_id)).rowcount
                if n != 1: raise TaskError('stale_revision')
            else:
                c.execute('INSERT INTO tool_goals '
                    '(reference,owner_id,principal_id,kind,status,revision,payload,checksum) VALUES(?,?,?,?,?,?,?,?)',
                    (value['reference'],principal.user_id,principal.principal_id,kind,status,1,json.dumps(fields),digest(value)))
        return value


def public(value):
    return {k:v for k,v in value.items() if k not in {'owner_id','principal_id'}}


def merge(old, patch):
    """Object merge; omitted = retain, null = clear, arrays = replace."""
    value = deepcopy(old)
    for key,item in patch.items():
        if item is None: value.pop(key,None)
        elif isinstance(item,dict): value[key] = merge(value.get(key,{}) if isinstance(value.get(key),dict) else {},item)
        else: value[key] = item
    return value


def normalized(kind, fields):
    from security.redaction import redact
    if len(json.dumps(fields,ensure_ascii=False)) > 12000 or redact(fields) != fields:
        raise TaskError('unsafe_fields', status=422)
    if kind == 'transfer':
        value=TransferDraft.model_validate(fields).model_dump(exclude_none=True)
        if 'amount' in value:
            from pydantic import TypeAdapter
            try: TypeAdapter(TransferMoneyArguments.model_fields['amount'].rebuild_annotation()).validate_python(value['amount'])
            except ValidationError: raise TaskError('invalid_amount',status=422) from None
        return value
    return QueryPlan.model_validate(dict(selection={},**fields) if 'selection' not in fields else fields).model_dump(mode='json',exclude_none=True)


def inspect(principal, value):
    try:
        return _inspect(principal,value)
    except Exception as error:
        diagnostic=uuid4().hex
        log.exception('draft validation dependency failure trace=%s',diagnostic)
        from storage.errors import StorageError
        code='service_unavailable' if isinstance(error,(sqlite3.Error,StorageError)) else 'internal_error'
        return dict(missing_fields=[],field_errors=[dict(field='validation',code=code)],
            candidates=[],resolved_recipient=None,ready=False,validation_trace=diagnostic,
            validation_scope='dependency failed; saved fields retained, use draft.validate later')


def _inspect(principal, value):
    fields=value['fields']; missing=[]; errors=[]; candidates=[]; resolved=None
    if value['kind']=='transfer':
        for key in ['from_card_id','amount']:
            if fields.get(key) is None: missing.append(key)
        if not fields.get('recipient_id') and not fields.get('recipient_label'):
            missing.append('recipient_label_or_id')
        from transfer_service import get_transfer_recipients
        result=get_transfer_recipients(principal)
        if not result['success']:
            return dict(missing_fields=missing,field_errors=[dict(field='recipient_id',code='service_unavailable')],
                candidates=[],resolved_recipient=None,ready=False,validation_scope='recipient lookup unavailable; saved fields retained')
        label=fields.get('recipient_label')
        label=label.strip() if label is not None else None
        candidates=[r for r in result['data'] if
            (r['recipient_id']==fields['recipient_id'] if fields.get('recipient_id') else r['name']==label)]
        if fields.get('recipient_id') and fields.get('recipient_label'):
            # Directory resolution, including ambiguity, must agree with the selected ID.
            matches=[r for r in result['data'] if r['name']==label]
            if not any(r['recipient_id']==fields['recipient_id'] for r in matches):
                errors.append(dict(field='recipient_label',code='recipient_conflict',conflicts_with='recipient_id'))
                candidates=[]
        if len(candidates)==1:
            resolved=candidates[0]
            # The original label is only an unresolved user reference, never an ID.
        elif fields.get('recipient_id') or fields.get('recipient_label'):
            errors.append(dict(field='recipient_id',code='recipient_ambiguous' if candidates else 'recipient_not_found'))
        if fields.get('amount') is not None:
            try:
                # Reuse the existing money field constraints, with no invented selector.
                from pydantic import TypeAdapter
                TypeAdapter(TransferMoneyArguments.model_fields['amount'].rebuild_annotation()).validate_python(fields['amount'])
            except ValidationError:
                errors.append(dict(field='amount',code='invalid_amount',allowed='positive CNY, max 2 decimals, max 10 digits'))
    else:
        q=QueryPlan.model_validate(fields)
        for prefix,s in [('selection',q.selection),('comparison',q.comparison)]:
            if s and q.time_mode=='period' and not s.period: missing.append(prefix+'.period')
            if s and s.current_document: errors.append(dict(field=prefix+'.document_id',code='explicit_reference_required'))
    return dict(missing_fields=missing,field_errors=errors,candidates=candidates if len(candidates)>1 else [],
        resolved_recipient=resolved,ready=not missing and not errors,
        validation_scope='structure and recipient lookup; ownership, availability and business conditions rechecked during execution')


