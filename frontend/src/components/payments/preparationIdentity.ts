/**
 * Payment-preparation request identity and recovery.
 *
 * Why: `prepare-payment` creates a durable preparation and calls the existing
 * transfer draft/task kernel. If the HTTP response is lost (network/proxy/refresh),
 * re-sending with a NEW `client_request_id` can create a second task for the same
 * obligation, so the UI must persist the original request identity BEFORE the call
 * and recover with the exact same key/params until a server fact (document
 * projection `preparation`, task list, or `GET /payment-preparations/{id}`) proves
 * the original attempt is finished.
 *
 * Stored data only contains request identity and public business references:
 * no account password, no TOTP secret, no confirmation token, no OTP code.
 * The storage key is bound to the authenticated user id, so switching accounts in
 * the same tab cannot reuse another user's request identity.
 */

export interface StorageLike {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

export interface PendingPreparation {
  client_request_id: string
  user_id: number
  document_id: string
  revision: number
  digest: string
  item_ids: string[]
  recipient_resolution_id: string
  from_card_id: number
  remark: string | null
  created_at: string
  /** Server facts learned after a replay/read; never guessed. */
  preparation_id: string | null
  state: string | null
  task_id: string | null
}

const PREFIX = 'banking-payment-prep:'
export const REQUEST_ID_PATTERN = /^[A-Za-z0-9_-]{8,100}$/

export function pendingKey(userId: number, documentId: string): string {
  return `${PREFIX}${userId}:${documentId}`
}

export function newRequestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return `ui-${crypto.randomUUID()}`
  const random = Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2)
  return `ui-${random}${Date.now().toString(36)}`.slice(0, 60)
}

function valid(record: unknown): record is PendingPreparation {
  if (!record || typeof record !== 'object') return false
  const value = record as Record<string, unknown>
  return typeof value.client_request_id === 'string' && REQUEST_ID_PATTERN.test(value.client_request_id) &&
    typeof value.user_id === 'number' && Number.isSafeInteger(value.user_id) &&
    typeof value.document_id === 'string' && value.document_id.length > 0 &&
    typeof value.revision === 'number' && Number.isSafeInteger(value.revision) && value.revision >= 1 &&
    typeof value.digest === 'string' && value.digest.length > 0 &&
    Array.isArray(value.item_ids) && value.item_ids.every(id => typeof id === 'string') &&
    typeof value.recipient_resolution_id === 'string' &&
    typeof value.from_card_id === 'number' && Number.isSafeInteger(value.from_card_id) && value.from_card_id > 0
}

/** Read a pending identity for this exact user + document. Never throws. */
export function readPending(storage: StorageLike | null | undefined, userId: number, documentId: string): PendingPreparation | null {
  if (!storage) return null
  try {
    const raw = storage.getItem(pendingKey(userId, documentId))
    if (!raw) return null
    const parsed = JSON.parse(raw) as unknown
    if (!valid(parsed) || parsed.user_id !== userId || parsed.document_id !== documentId) return null
    return {
      ...parsed,
      remark: typeof parsed.remark === 'string' ? parsed.remark : null,
      preparation_id: typeof parsed.preparation_id === 'string' ? parsed.preparation_id : null,
      state: typeof parsed.state === 'string' ? parsed.state : null,
      task_id: typeof parsed.task_id === 'string' ? parsed.task_id : null,
    }
  } catch {
    return null
  }
}

/** Persist the identity BEFORE the request is sent. Returns the stored record. */
export function beginPending(storage: StorageLike | null | undefined, input: Omit<PendingPreparation, 'created_at' | 'preparation_id' | 'state' | 'task_id'>): PendingPreparation {
  const record: PendingPreparation = {
    ...input,
    created_at: new Date().toISOString(),
    preparation_id: null,
    state: null,
    task_id: null,
  }
  writePending(storage, record)
  return record
}

export function writePending(storage: StorageLike | null | undefined, record: PendingPreparation): void {
  if (!storage) return
  try { storage.setItem(pendingKey(record.user_id, record.document_id), JSON.stringify(record)) } catch { /* storage unavailable: in-memory state still guards the session */ }
}

export function clearPending(storage: StorageLike | null | undefined, userId: number, documentId: string): void {
  if (!storage) return
  try { storage.removeItem(pendingKey(userId, documentId)) } catch { /* ignore */ }
}

/** Identity is only reusable for the same document version + digest. */
export function sameIdentity(record: PendingPreparation, doc: { document_id: string; revision: number; digest: string }): boolean {
  return record.document_id === doc.document_id && record.revision === doc.revision && record.digest === doc.digest
}

export type RecoveryMode =
  /** No local identity and no server-side active preparation. */
  | 'none'
  /** Server document projection already shows an active preparation; read it first. */
  | 'server_active'
  /** Same request identity can be replayed exactly (server resumes/replays). */
  | 'same_key_replay'
  /** Document changed since the request was recorded: verify server facts first. */
  | 'stale_identity'

export function recoveryMode(
  doc: { document_id: string; revision: number; digest: string; preparation?: { state?: string | null } | null } | null,
  pending: PendingPreparation | null,
): RecoveryMode {
  const state = doc?.preparation?.state || ''
  if (state && ['preparing', 'draft_ready', 'prepared'].includes(state)) return 'server_active'
  if (pending && doc && sameIdentity(pending, doc)) return 'same_key_replay'
  if (pending) return 'stale_identity'
  return 'none'
}

/** Errors known to be raised BEFORE the preparation row is reserved (no side effect). */
export const SAFE_NO_SIDE_EFFECT_CODES = new Set([
  'document_revision_conflict', 'document_digest_mismatch', 'document_not_ready',
  'payment_document_blocked', 'selection_changed', 'amount_unresolved', 'amount_conflict',
  'obligation_missing', 'obligation_amount_conflict', 'kind_not_payable', 'recipient_changed',
  'recipient_selection_required', 'recipient_unsupported', 'recipient_not_found',
  'recipient_resolution_not_found', 'resolution_expired', 'candidate_not_found',
  'card_not_found', 'card_unavailable', 'payment_obligation_already_prepared',
  'payment_obligation_already_paid', 'invalid_client_request_id', 'client_request_conflict',
])

/**
 * Only drop the stored identity when the server rejected the request before
 * reserving a preparation. Everything else (network, 5xx, unknown, document
 * changed after reservation) must keep the identity and be verified first.
 */
export function canDropPending(record: PendingPreparation | null, errorCode: string | null | undefined): boolean {
  if (!record) return false
  return !!errorCode && SAFE_NO_SIDE_EFFECT_CODES.has(errorCode)
}

/** Human-readable summary for recovery panels. */
export function pendingSummary(record: PendingPreparation): string {
  return `请求键 ${record.client_request_id} · 单据修订 ${record.revision} · 摘要 ${record.digest.slice(0, 12)}… · 创建于 ${new Date(record.created_at).toLocaleString('zh-CN')}`
}
