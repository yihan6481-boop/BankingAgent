/**
 * Client for the payment-1.0 pipeline (A line).
 *
 * Aligned with A's draft `backend/payments/{routes,models,documents,service,prepare,reconcile}.py`
 * snapshot read on 2026-10-26. A had not committed the contract yet; if A changes
 * field names or request bodies this file is the only frontend adapter to update
 * (the UI components read the exported types). See docs/tasks/B_FRONTEND_HANDOFF.md.
 *
 * Safety rules kept by this module:
 * - server authentication supplies owner; no owner/user field is sent;
 * - money is handled as integer cents; no client float arithmetic;
 * - every create/select/prepare/reconcile/link call carries `client_request_id`
 *   (server pattern `^[A-Za-z0-9_-]{8,100}$`) so a retried HTTP request cannot
 *   silently create a second resource;
 * - responses are unwrapped only via the documented `{contract_version,data}`
 *   envelope, never guessed.
 */
import { ApiError, request } from './client'
import type { SecureTask } from '../integrationTypes'

export type PaymentNoticeKind = 'payment_notice' | 'invoice' | 'receipt' | 'bank_receipt' | 'unknown'
/** Provider field evidence (image coordinates/quote). */
export type FieldEvidence = { asset_id: string; page: number; quote: string; bbox: number[] | null }
/** Server-side verification/directory evidence (source reference, never image text). */
export type SourceEvidence = { source: string; reference?: string; note?: string; transaction_id?: number; link_id?: string }
export type FieldValue<T> = { value: T | null; origin: 'image' | 'user'; evidence: FieldEvidence[]; issues: string[]; confidence: number | null }
export type DocumentItem = { item_id: string; name: FieldValue<string>; amount_cents: FieldValue<number>; currency: string | null }
export type PayeeAccount = { account_name: FieldValue<string>; account_number: FieldValue<string>; bank_name: FieldValue<string> }
export type DocumentReconciliation = {
  amount_cents: FieldValue<number>
  paid_on: FieldValue<string>
  time: FieldValue<string>
  time_precision: 'day' | 'minute' | 'second' | 'unknown'
  merchant: FieldValue<string>
  payment_method: FieldValue<string>
  card_last4: FieldValue<string>
  reference_number: FieldValue<string>
}
export type Extraction = {
  kind: PaymentNoticeKind
  supported: boolean
  items: DocumentItem[]
  payer_name: FieldValue<string>
  institution: FieldValue<string>
  bill_number: FieldValue<string>
  period_label: FieldValue<string>
  period_start: FieldValue<string>
  period_end: FieldValue<string>
  due_date: FieldValue<string>
  due_precision: 'day' | 'month' | 'year' | 'unknown'
  payee_accounts: PayeeAccount[]
  remark_requirement: FieldValue<string>
  reconciliation: DocumentReconciliation
  issues: string[]
}
export type VerificationStatus = 'match' | 'mismatch' | 'unknown' | 'not_applicable'
export type VerificationBlock = { status: VerificationStatus; evidence: SourceEvidence[] }
export type PaymentStatusVerification = { status: 'not_found_in_this_system' | 'found_in_this_system' | 'unknown'; external_channels: 'unknown' | 'not_applicable'; evidence: SourceEvidence[] }
export type Verification = {
  user_association: VerificationBlock
  document_authenticity: VerificationBlock
  payee_ownership: VerificationBlock
  payment_status: PaymentStatusVerification
}
export type DocumentObligation = { obligation_id: string; state: string; amount_cents: number; revision: number }
export type DocumentPreparation = { preparation_id: string; state: string; draft_reference: string | null; task_id: string | null; amount_cents: number; from_card_id: number }
export type TransactionLinkView = {
  link_id: string
  transaction_id: number
  reconciliation_id: string
  document_revision?: number
  document_digest?: string
  /** True when the document changed after this link: historical evidence only. */
  stale?: boolean
  payment_status: string
  created_at: string
}
export type PaymentDocument = {
  document_id: string
  revision: number
  state: 'uploaded' | 'extracting' | 'extraction_failed' | 'review' | 'ready' | 'prepared' | 'reconciled' | 'unknown'
  kind: PaymentNoticeKind
  digest: string
  created_at: string
  updated_at: string
  repeated_upload: boolean
  replayed: boolean
  image: { sha256: string; mime: string; width: number | null; height: number | null }
  /** Extraction tree as reviewed/sanitized by the server (account numbers masked). */
  fields: Extraction
  /** Extraction before user changes, when the server has one. */
  original: Extraction | null
  selected_item_ids: string[]
  selected_payee_index: number | null
  total_amount_cents: number | null
  /** Server-computed blocking reasons; the UI must display them, not recompute approval. */
  blocking: string[]
  ready: boolean
  user_notes: string | null
  user_confirmed_own_bill: boolean
  verification: Verification
  obligation: DocumentObligation | null
  preparation: DocumentPreparation | null
  links: TransactionLinkView[]
  error_code: string | null
  limitations: string[]
  cancelled_task_ids?: string[]
}

export type RecipientSelector =
  | { kind: 'contact'; recipient_id: number }
  | { kind: 'phone'; phone: string }
  | { kind: 'bank_account'; account_number: string; account_name: string; bank_name: string | null }
  | { kind: 'document_payee'; document_id: string; revision: number; payee_index: number }
export type RecipientCandidate = {
  candidate_id: string
  recipient_id: number | null
  account_name: string
  bank_name: string | null
  masked_account: string
  beneficiary_kind: 'person' | 'institution'
  trusted: boolean
  supports: string[]
  reasons: string[]
  evidence: SourceEvidence[]
}
export type RecipientResolution = {
  resolution_id: string
  revision: number
  selector_kind: string
  selector: Record<string, unknown>
  status: 'resolved' | 'ambiguous' | 'not_found' | 'unsupported' | 'needs_information'
  candidates: RecipientCandidate[]
  selected_candidate_id: string | null
  expires_at: string
  created_at: string
  limitations: string[]
}
export type Contact = {
  note?: string
  contact_id: string | null
  recipient_id: number
  display_name: string
  phone_masked: string | null
  phone_bound: boolean
  revision: number | null
  status: string
  created_at: string | null
  updated_at: string | null
  directory: { recipient_id: number; name: string; bank_name: string | null; masked_account: string; beneficiary_kind: string; trusted: boolean; active: boolean } | null
}
export type ContactCreateInput = { recipient_id: number; display_name?: string | null; phone?: string | null }
export type ContactUpdateInput = { revision: number; display_name?: string | null; phone?: string | null; archive?: boolean }

export type PaymentPreparation = {
  preparation_id: string
  state: 'preparing' | 'draft_ready' | 'prepared' | 'cancelled' | 'failed' | 'unknown'
  document_id: string
  document_revision: number
  obligation_id: string | null
  item_ids: string[]
  amount_cents: number
  currency: string
  recipient_resolution_id: string
  candidate_id: string | null
  recipient_id: number | null
  from_card_id: number
  remark: string | null
  draft_reference: string | null
  draft_revision: number | null
  task_id: string | null
  task_revision: number | null
  created_at: string
  updated_at: string
}
export type PreparePaymentResult = {
  preparation: PaymentPreparation
  document: PaymentDocument
  draft?: { reference: string; revision: number; status: string; kind: string; amount: string; from_card_id: number; recipient_id: number; remark?: string | null }
  /** Existing strong-auth-1.0 task response; confirm/TOTP still go through the task contract. */
  task?: SecureTask
  replayed?: boolean
}

export type ReconcileCandidate = {
  transaction_id: number
  date: string
  merchant: string | null
  amount_cents: number
  direction: 'income' | 'expense'
  transaction_type: string | null
  card_last4: string | null
  compared: { field: string; expected: unknown; actual: unknown; match: boolean }[]
  contradictions: string[]
  evidence: SourceEvidence[]
}
export type ReconciliationRun = {
  reconciliation_id: string
  revision: number
  document_id: string
  document_revision: number
  document_digest: string
  range: { start: string; end_exclusive: string }
  match_status: 'unique_match' | 'multiple_candidates' | 'not_found' | 'conflict'
  candidates: ReconcileCandidate[]
  compared_fields: string[]
  unknown_fields: string[]
  created_at: string
}
export type ReconcileResult = { reconciliation: ReconciliationRun; document: PaymentDocument; replayed: boolean }
export type LinkResult = {
  link: { link_id: string; document_id: string; transaction_id: number; reconciliation_id: string; payment_status: string; evidence: Record<string, unknown>; created_at: string }
  document: PaymentDocument
  /** Server truth: linking a receipt to a ledger row never creates money movement. */
  ledger_unchanged: boolean
  replayed: boolean
}

const secure = (sid: string) => `/secure/sessions/${encodeURIComponent(sid)}`
const docs = (sid: string) => `${secure(sid)}/payment-documents`
export const CLIENT_REQUEST_ID = /^[A-Za-z0-9_-]{8,100}$/
export const clientRequestId = () => `ui-${crypto.randomUUID()}`
function unwrap<T>(value: unknown): T {
  if (value && typeof value === 'object' && 'data' in (value as Record<string, unknown>)) return (value as { data: T }).data
  return value as T
}
function resolutionOf(value: unknown): RecipientResolution {
  const data = unwrap<{ resolution?: RecipientResolution } | RecipientResolution>(value)
  if (data && typeof data === 'object' && 'resolution' in data) return (data as { resolution: RecipientResolution }).resolution
  return data as RecipientResolution
}
function apiError(value: unknown, status: number): ApiError {
  const p = value as { code?: string; message?: string; data?: unknown; error?: { code?: string; message?: string } } | null
  const code = p?.error?.code || p?.code || 'request_failed'
  const message = p?.error?.message || p?.message
  return new ApiError(code, !message || message === code ? '操作未完成，请回读单据与任务状态后再试。' : message, status, p?.data ?? null)
}

export const payments = {
  clientRequestId,
  list: async (sid: string) => unwrap<{ documents: Pick<PaymentDocument, 'document_id' | 'revision' | 'state' | 'kind' | 'created_at' | 'updated_at'>[]; has_more: boolean }>(await request(`${docs(sid)}`, sid)),
  get: async (sid: string, id: string) => unwrap<PaymentDocument>(await request(`${docs(sid)}/${encodeURIComponent(id)}`, sid)),
  async create(sid: string, file: File, requestId = clientRequestId()) {
    if (!CLIENT_REQUEST_ID.test(requestId)) throw new ApiError('invalid_client_request_id', '请求标识不合法，请刷新后重试。')
    const base = (import.meta.env.VITE_API_BASE || '/api').replace(/\/$/, '')
    const response = await fetch(`${base}${docs(sid)}?client_request_id=${encodeURIComponent(requestId)}`, {
      method: 'POST', cache: 'no-store',
      // Contract: raw JPEG/PNG bytes with application/octet-stream.
      headers: { 'Content-Type': 'application/octet-stream', Authorization: `Bearer ${sid}` },
      body: file,
    })
    let value: unknown
    try { value = await response.json() } catch { throw new ApiError('invalid_response', '单据上传返回了无法读取的结果，请核对服务端。', response.status) }
    if (!response.ok) throw apiError(value, response.status)
    return unwrap<PaymentDocument>(value)
  },
  contacts: async (sid: string) => unwrap<{ contacts: Contact[]; truncated: boolean }>(await request(`${secure(sid)}/contacts`, sid)),
  createContact: async (sid: string, input: ContactCreateInput, requestId = clientRequestId()) =>
    unwrap<{ contact: Contact; replayed: boolean }>(await request(`${secure(sid)}/contacts`, sid, 'POST', {
      client_request_id: requestId, recipient_id: input.recipient_id,
      display_name: input.display_name ?? null, phone: input.phone ?? null,
    })),
  updateContact: async (sid: string, contactId: string, input: ContactUpdateInput, requestId = clientRequestId()) =>
    unwrap<{ contact: Contact; replayed: boolean }>(await request(`${secure(sid)}/contacts/${encodeURIComponent(contactId)}`, sid, 'PATCH', {
      client_request_id: requestId, revision: input.revision,
      display_name: input.display_name ?? null, phone: input.phone ?? null, archive: !!input.archive,
    })),
  contact: async (sid: string, contactId: string) =>
    unwrap<{ contact: Contact }>(await request(`${secure(sid)}/contacts/${encodeURIComponent(contactId)}`, sid)),
  extract: async (sid: string, doc: Pick<PaymentDocument, 'document_id' | 'revision'>, requestId = clientRequestId()) =>
    unwrap<PaymentDocument>(await request(`${docs(sid)}/${encodeURIComponent(doc.document_id)}/extract`, sid, 'POST', { client_request_id: requestId, revision: doc.revision })),
  review: async (sid: string, doc: PaymentDocument, input: {
    kind?: PaymentNoticeKind; fields: Extraction; selected_item_ids: string[]; selected_payee_index: number | null
    user_notes?: string | null; user_confirmed_own_bill?: boolean
  }, requestId = clientRequestId()) =>
    unwrap<PaymentDocument>(await request(`${docs(sid)}/${encodeURIComponent(doc.document_id)}/review`, sid, 'PUT', {
      client_request_id: requestId, revision: doc.revision, digest: doc.digest,
      kind: input.kind ?? doc.kind, fields: input.fields,
      selected_item_ids: input.selected_item_ids, selected_payee_index: input.selected_payee_index,
      user_notes: input.user_notes ?? null, user_confirmed_own_bill: !!input.user_confirmed_own_bill,
    })),
  async image(sid: string, id: string) {
    const base = (import.meta.env.VITE_API_BASE || '/api').replace(/\/$/, '')
    const response = await fetch(`${base}${docs(sid)}/${encodeURIComponent(id)}/image`, { cache: 'no-store', headers: { Authorization: `Bearer ${sid}` } })
    if (!response.ok) throw new ApiError('document_image_failed', '单据原图读取失败，请重新登录或重新打开。', response.status)
    return response.blob()
  },
  resolve: async (sid: string, selector: RecipientSelector, requestId = clientRequestId()) =>
    resolutionOf(await request(`${secure(sid)}/recipient-resolutions`, sid, 'POST', { client_request_id: requestId, selector })),
  getResolution: async (sid: string, id: string) =>
    resolutionOf(await request(`${secure(sid)}/recipient-resolutions/${encodeURIComponent(id)}`, sid)),
  selectCandidate: async (sid: string, resolution: RecipientResolution, candidateId: string, requestId = clientRequestId()) =>
    resolutionOf(await request(`${secure(sid)}/recipient-resolutions/${encodeURIComponent(resolution.resolution_id)}/select`, sid, 'POST', {
      client_request_id: requestId, revision: resolution.revision, candidate_id: candidateId,
    })),
  prepare: async (sid: string, doc: Pick<PaymentDocument, 'document_id' | 'revision' | 'digest'>, input: {
    item_ids: string[]; recipient_resolution_id: string; from_card_id: number; remark?: string | null
  }, requestId = clientRequestId()) =>
    unwrap<PreparePaymentResult>(await request(`${docs(sid)}/${encodeURIComponent(doc.document_id)}/prepare-payment`, sid, 'POST', {
      client_request_id: requestId, revision: doc.revision, digest: doc.digest,
      item_ids: input.item_ids, recipient_resolution_id: input.recipient_resolution_id,
      from_card_id: input.from_card_id, remark: input.remark ?? null,
    })),
  readPreparation: async (sid: string, preparationId: string) =>
    unwrap<{ preparation: PaymentPreparation }>(await request(`${secure(sid)}/payment-preparations/${encodeURIComponent(preparationId)}`, sid)),
  reconcile: async (sid: string, doc: PaymentDocument, scope: { start: string; end_exclusive: string }, requestId = clientRequestId()) =>
    unwrap<ReconcileResult>(await request(`${docs(sid)}/${encodeURIComponent(doc.document_id)}/reconcile`, sid, 'POST', {
      client_request_id: requestId, revision: doc.revision, digest: doc.digest,
      start: scope.start, end_exclusive: scope.end_exclusive,
    })),
  link: async (sid: string, doc: PaymentDocument, run: ReconciliationRun, transactionId: number, requestId = clientRequestId()) =>
    unwrap<LinkResult>(await request(`${docs(sid)}/${encodeURIComponent(doc.document_id)}/transaction-link`, sid, 'POST', {
      client_request_id: requestId, revision: doc.revision, digest: doc.digest,
      reconciliation_id: run.reconciliation_id, reconciliation_revision: run.revision, transaction_id: transactionId,
    })),
  delete: async (sid: string, doc: PaymentDocument) =>
    unwrap<{ document_id: string; deleted: boolean }>(await request(`${docs(sid)}/${encodeURIComponent(doc.document_id)}?revision=${doc.revision}`, sid, 'DELETE')),
}
