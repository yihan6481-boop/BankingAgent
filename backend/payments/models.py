"""Strict request contracts for the payment pipeline (payment-1.0).

Model output is untrusted input: every extracted value keeps its origin and
evidence, and no field can authorize a payment. The transfer draft/task kernel
still performs preview, confirmation, strong authentication and execution.
"""
from __future__ import annotations

import re
from datetime import date as calendar_date
from typing import Annotated, Generic, Literal, TypeVar, Union

from pydantic import Field, model_validator

from agent.schemas import StrictModel

CLIENT_REQUEST_ID = r'^[A-Za-z0-9_-]{8,100}$'
DOCUMENT_ID = r'^pd_[0-9a-f]{32}$'
RESOLUTION_ID = r'^rr_[0-9a-f]{32}$'
CONTACT_ID = r'^ct_[0-9a-f]{32}$'
DIGEST = r'^[0-9a-f]{64}$'
KINDS = Literal['payment_notice', 'invoice', 'receipt', 'bank_receipt', 'unknown']
PHONE_INPUT = re.compile(r'^(?:\+?86)?1[3-9][0-9]{9}$')


def normalize_phone(value):
    """Normalize a PRC mobile number to E.164; anything else is rejected."""
    if not isinstance(value, str):
        return None
    compact = re.sub(r'[\s\-()]', '', value.strip())
    if not PHONE_INPUT.fullmatch(compact):
        return None
    digits = compact[3:] if compact.startswith('+86') else compact[2:] if compact.startswith('86') else compact
    if not re.fullmatch(r'1[3-9][0-9]{9}', digits):
        return None
    return '+86' + digits


def mask_phone(phone):
    if not phone or len(phone) < 8:
        return None
    return phone[:3] + '****' + phone[-4:]


class Evidence(StrictModel):
    asset_id: str = Field(min_length=1, max_length=64)
    page: int = Field(ge=1, le=20)
    quote: str = Field(min_length=1, max_length=500)
    bbox: list[float] | None = Field(default=None, max_length=4)

    @model_validator(mode='after')
    def box(self):
        if self.bbox is not None and (len(self.bbox) != 4 or any(x < 0 for x in self.bbox)):
            raise ValueError('invalid_bbox')
        return self


T = TypeVar('T')


class FieldValue(StrictModel, Generic[T]):
    value: T | None = None
    origin: Literal['image', 'user'] = 'image'
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    issues: list[str] = Field(default_factory=list, max_length=20)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode='after')
    def bounded(self):
        if any(len(item) > 240 for item in self.issues):
            raise ValueError('issue_too_long')
        return self


class FieldStr(FieldValue[str]):
    @model_validator(mode='after')
    def bounded_text(self):
        if self.value is not None and len(self.value) > 200:
            raise ValueError('value_too_long')
        return self


class FieldCents(FieldValue[int]):
    @model_validator(mode='after')
    def cents(self):
        if self.value is not None and not 1 <= self.value <= 10 ** 12:
            raise ValueError('invalid_amount_cents')
        return self


class FieldDate(FieldValue[str]):
    @model_validator(mode='after')
    def calendar(self):
        if self.value is not None:
            if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', self.value):
                raise ValueError('explicit_year_required')
            calendar_date.fromisoformat(self.value)
        return self


class PayeeAccount(StrictModel):
    account_name: FieldStr
    account_number: FieldStr
    bank_name: FieldStr


class DocumentItem(StrictModel):
    item_id: str = Field(pattern=r'^i[0-9]{1,2}$')
    name: FieldStr
    amount_cents: FieldCents
    currency: str | None = Field(default=None, pattern=r'^[A-Z]{3}$')


class ReconciliationInfo(StrictModel):
    amount_cents: FieldCents = Field(default_factory=FieldCents)
    paid_on: FieldDate = Field(default_factory=FieldDate)
    time: FieldStr = Field(default_factory=FieldStr)
    time_precision: Literal['day', 'minute', 'second', 'unknown'] = 'unknown'
    merchant: FieldStr = Field(default_factory=FieldStr)
    payment_method: FieldStr = Field(default_factory=FieldStr)
    card_last4: FieldStr = Field(default_factory=FieldStr)
    reference_number: FieldStr = Field(default_factory=FieldStr)

    @model_validator(mode='after')
    def precision(self):
        if self.time.value and self.time_precision not in {'minute', 'second'}:
            raise ValueError('time_precision_mismatch')
        return self


class Extraction(StrictModel):
    """Provider output. Never confer authorization; always requires user review."""

    kind: KINDS = 'unknown'
    supported: bool = True
    items: list[DocumentItem] = Field(default_factory=list, max_length=20)
    payer_name: FieldStr = Field(default_factory=FieldStr)
    institution: FieldStr = Field(default_factory=FieldStr)
    bill_number: FieldStr = Field(default_factory=FieldStr)
    period_label: FieldStr = Field(default_factory=FieldStr)
    period_start: FieldDate = Field(default_factory=FieldDate)
    period_end: FieldDate = Field(default_factory=FieldDate)
    due_date: FieldStr = Field(default_factory=FieldStr)
    due_precision: Literal['day', 'month', 'year', 'unknown'] = 'unknown'
    payee_accounts: list[PayeeAccount] = Field(default_factory=list, max_length=5)
    remark_requirement: FieldStr = Field(default_factory=FieldStr)
    reconciliation: ReconciliationInfo = Field(default_factory=ReconciliationInfo)
    issues: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode='after')
    def sane(self):
        if any(len(item) > 240 for item in self.issues):
            raise ValueError('issue_too_long')
        known = {item.item_id for item in self.items}
        if len(known) != len(self.items):
            raise ValueError('duplicate_item_id')
        return self


def unresolved(field):
    """Reasons a single field is not fit for preparation."""
    if field.value is None:
        return ['missing']
    return list(field.issues)


def field_reasons(extraction):
    """Blocking reasons for a stored/reviewed extraction tree."""
    reasons = []
    if extraction.kind == 'unknown':
        reasons.append('kind_unknown')
    if not extraction.items:
        reasons.append('missing_items')
    for item in extraction.items:
        for reason in unresolved(item.amount_cents):
            reasons.append(item.item_id + '.amount_cents:' + reason)
        for reason in unresolved(item.name):
            reasons.append(item.item_id + '.name:' + reason)
        if item.currency not in {None, 'CNY'}:
            reasons.append(item.item_id + '.currency:unsupported')
    for name in ('institution', 'bill_number'):
        for reason in unresolved(getattr(extraction, name)):
            reasons.append(name + ':' + reason)
    if extraction.bill_number.value is None:
        reasons.append('bill_number_required_for_duplicate_check')
    if not extraction.payee_accounts:
        reasons.append('payee:qr_only')
    for index, account in enumerate(extraction.payee_accounts):
        for name in ('account_name', 'account_number'):
            for reason in unresolved(getattr(account, name)):
                reasons.append('payee_accounts[%d].%s:%s' % (index, name, reason))
    for reason in extraction.issues:
        reasons.append('document:' + reason)
    return sorted(set(reasons))


def selected_amount_cents(extraction, selected_item_ids):
    if not selected_item_ids:
        return None
    by_id = {item.item_id: item for item in extraction.items}
    total = 0
    for item_id in selected_item_ids:
        item = by_id.get(item_id)
        if item is None or item.amount_cents.value is None:
            return None
        total += item.amount_cents.value
    return total if total > 0 else None


def digest_payload(document_id, revision, kind, fields, selected_item_ids, selected_payee_index):
    """Canonical review identity; account numbers stay inside the server."""
    return dict(document_id=document_id, revision=revision, kind=kind,
                fields=fields.model_dump(mode='json') if isinstance(fields, Extraction) else fields,
                selected_item_ids=list(selected_item_ids),
                selected_payee_index=selected_payee_index)


# ---------------------------------------------------------------------------
# Request bodies.
# ---------------------------------------------------------------------------

class ContactCreateRequest(StrictModel):
    client_request_id: str = Field(pattern=CLIENT_REQUEST_ID)
    recipient_id: int = Field(gt=0)
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=32)


class ContactUpdateRequest(StrictModel):
    client_request_id: str = Field(pattern=CLIENT_REQUEST_ID)
    revision: int = Field(ge=1)
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=32)
    archive: bool = False


class ContactSelector(StrictModel):
    kind: Literal['contact']
    recipient_id: int = Field(gt=0)


class PhoneSelector(StrictModel):
    kind: Literal['phone']
    phone: str = Field(min_length=5, max_length=32)


class BankAccountSelector(StrictModel):
    kind: Literal['bank_account']
    account_number: str = Field(min_length=6, max_length=40)
    account_name: str = Field(min_length=1, max_length=100)
    bank_name: str | None = Field(default=None, max_length=100)


class DocumentPayeeSelector(StrictModel):
    kind: Literal['document_payee']
    document_id: str = Field(pattern=DOCUMENT_ID)
    revision: int = Field(ge=1)
    payee_index: int = Field(ge=0, le=4)


Selector = Annotated[
    Union[ContactSelector, PhoneSelector, BankAccountSelector, DocumentPayeeSelector],
    Field(discriminator='kind')]


class ResolutionRequest(StrictModel):
    client_request_id: str = Field(pattern=CLIENT_REQUEST_ID)
    selector: Selector


class ResolutionSelectRequest(StrictModel):
    client_request_id: str = Field(pattern=CLIENT_REQUEST_ID)
    revision: int = Field(ge=1)
    candidate_id: str = Field(min_length=1, max_length=120)


class DocumentRevisionRequest(StrictModel):
    client_request_id: str = Field(pattern=CLIENT_REQUEST_ID)
    revision: int = Field(ge=1)


class PreparationRecoverRequest(StrictModel):
    """Owner-scoped recovery of an interrupted preparation (idempotent)."""

    client_request_id: str = Field(pattern=CLIENT_REQUEST_ID)


class ReviewRequest(StrictModel):
    client_request_id: str = Field(pattern=CLIENT_REQUEST_ID)
    revision: int = Field(ge=1)
    digest: str = Field(pattern=DIGEST)
    kind: KINDS | None = None
    fields: Extraction
    selected_item_ids: list[str] = Field(default_factory=list, max_length=20)
    selected_payee_index: int | None = Field(default=None, ge=0, le=4)
    user_notes: str | None = Field(default=None, max_length=1000)
    user_confirmed_own_bill: bool = False

    @model_validator(mode='after')
    def unique_items(self):
        if len(set(self.selected_item_ids)) != len(self.selected_item_ids):
            raise ValueError('duplicate_item_selection')
        return self


class PreparePaymentRequest(StrictModel):
    client_request_id: str = Field(pattern=CLIENT_REQUEST_ID)
    revision: int = Field(ge=1)
    digest: str = Field(pattern=DIGEST)
    item_ids: list[str] = Field(min_length=1, max_length=20)
    recipient_resolution_id: str = Field(pattern=RESOLUTION_ID)
    from_card_id: int = Field(gt=0)
    remark: str | None = Field(default=None, max_length=100)

    @model_validator(mode='after')
    def unique_items(self):
        if len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError('duplicate_item_selection')
        return self


class ReconcileRequest(StrictModel):
    client_request_id: str = Field(pattern=CLIENT_REQUEST_ID)
    revision: int = Field(ge=1)
    digest: str = Field(pattern=DIGEST)
    start: str = Field(pattern=r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
    end_exclusive: str = Field(pattern=r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$')

    @model_validator(mode='after')
    def range(self):
        start = calendar_date.fromisoformat(self.start)
        end = calendar_date.fromisoformat(self.end_exclusive)
        if start >= end:
            raise ValueError('invalid_range')
        if (end - start).days > 366:
            raise ValueError('range_too_large')
        return self


class TransactionLinkRequest(StrictModel):
    client_request_id: str = Field(pattern=CLIENT_REQUEST_ID)
    revision: int = Field(ge=1)
    digest: str = Field(pattern=DIGEST)
    reconciliation_id: str = Field(pattern=r'^rc_[0-9a-f]{32}$')
    reconciliation_revision: int = Field(ge=1)
    transaction_id: int = Field(gt=0)
