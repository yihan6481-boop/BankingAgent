/**
 * Pure view-model for the payment-1.0 flow (aligned with A's draft contract).
 *
 * The server owns approval: `PaymentDocument.blocking` and `ready` are authoritative
 * and are displayed verbatim (with labels). This module only adds UI-level gates
 * that the server would reject anyway (missing explicit item/payee/candidate
 * selection) so the user is told before sending a request. Unknown data is never
 * coerced into "fine".
 */
import type {
  DocumentItem, Extraction, FieldValue, PaymentDocument, PaymentNoticeKind,
  ReconcileCandidate, ReconciliationRun, RecipientResolution, Verification, VerificationStatus,
} from '../../api/payments'

export const VERIFICATION_STATUS: Record<VerificationStatus, { label: string; tone: 'ok' | 'bad' | 'warn' | 'idle' }> = {
  match: { label: '匹配', tone: 'ok' },
  mismatch: { label: '不匹配', tone: 'bad' },
  unknown: { label: '未知（不能视为通过）', tone: 'warn' },
  not_applicable: { label: '不适用', tone: 'idle' },
}

export function verificationRows(verification: Verification | null | undefined) {
  if (!verification) return [
    { key: 'user_association', label: '用户与缴费义务关联', status: 'unknown' as VerificationStatus, evidence: 0 },
    { key: 'document_authenticity', label: '单据真实性', status: 'unknown' as VerificationStatus, evidence: 0 },
    { key: 'payee_ownership', label: '收款方归属', status: 'unknown' as VerificationStatus, evidence: 0 },
    { key: 'payment_status', label: '在本系统是否已支付', status: 'unknown' as VerificationStatus, evidence: 0 },
  ]
  const paymentStatus: VerificationStatus = verification.payment_status.status === 'found_in_this_system' ? 'match' : 'unknown'
  return [
    { key: 'user_association', label: '用户与缴费义务关联', status: verification.user_association.status, evidence: verification.user_association.evidence.length },
    { key: 'document_authenticity', label: '单据真实性', status: verification.document_authenticity.status, evidence: verification.document_authenticity.evidence.length },
    { key: 'payee_ownership', label: '收款方归属', status: verification.payee_ownership.status, evidence: verification.payee_ownership.evidence.length },
    {
      key: 'payment_status', evidence: verification.payment_status.evidence.length,
      // A ledger link proves a matching local row, never external settlement.
      label: verification.payment_status.status === 'found_in_this_system' ? '本系统匹配到流水（外部渠道清算未知）' : '在本系统是否已支付（未找到不代表未付款）',
      status: paymentStatus,
    },
  ]
}

export function formatCents(cents: number | null | undefined): string {
  if (cents == null || !Number.isSafeInteger(cents)) return '未知'
  const sign = cents < 0 ? '-' : ''
  const n = Math.abs(cents)
  return `${sign}${Math.floor(n / 100)}.${String(n % 100).padStart(2, '0')} 元`
}

export function parseCents(text: string): number | null {
  const value = text.trim()
  if (!/^(?:0|[1-9]\d{0,10})(?:\.\d{1,2})?$/.test(value)) return null
  const [whole, fraction = ''] = value.split('.')
  const n = BigInt(whole) * 100n + BigInt(fraction.padEnd(2, '0'))
  if (n < 0n || n > 1000000000000n) return null
  return Number(n)
}

export function centsText(cents: number | null | undefined): string {
  return cents == null ? '' : (cents / 100).toFixed(2)
}

export function fieldText<T>(field: FieldValue<T> | null | undefined, render: (value: T) => string = v => String(v)): string {
  if (!field || field.value == null) return '未提取'
  return render(field.value)
}

export function fieldIssues(field: FieldValue<unknown> | null | undefined): string[] {
  return (field?.issues || []).filter(Boolean)
}

export function evidenceText(field: FieldValue<unknown> | null | undefined, max = 2): string[] {
  return (field?.evidence || []).slice(0, max).map(e => `${e.asset_id} 第${e.page}页：“${e.quote}”`)
}

export function kindLabel(kind: PaymentNoticeKind | null | undefined): string {
  return ({ payment_notice: '缴费通知', invoice: '发票', receipt: '收据', bank_receipt: '银行回执', unknown: '未知' } as Record<string, string>)[kind || 'unknown'] || '未知'
}

/** Known server blocking codes rendered in Chinese; unknown codes stay visible. */
const BLOCKING_LABEL: Record<string, string> = {
  kind_unknown: '单据类型未确认（缴费通知/发票/回执/未知），不能准备付款。',
  kind_not_payable: '该单据类型不能准备付款（发票不能作为付款依据）。',
  missing_items: '没有可核对的缴费项目，不能准备付款。',
  missing_institution: '机构名称未确认，无法识别缴费义务。',
  missing_bill_number: '缴费单号未确认，无法可靠识别重复缴费。',
  'payee:qr_only': '单据只提供二维码/缴费编号，没有可核对的收款账户，不能据此转账。',
  qr_only: '单据只提供二维码/缴费编号，没有可核对的收款账户，不能据此转账。',
  payee_selection_required: '单据包含多个收款账户，必须明确选择一个，不能默认第一项。',
  payee_selection_invalid: '所选收款账户不属于当前单据，请重新选择。',
  payee_account_incomplete: '所选收款账户信息不完整，不能准备付款。',
  item_selection_required: '至少明确选择一个缴费项目，不能把合计或优惠金额当作应付。',
  item_selection_invalid: '所选缴费项目不属于当前单据，请重新选择。',
  amount_unresolved: '所选缴费项目金额未确认，不能准备付款。',
  amount_invalid: '所选缴费项目金额无效，不能准备付款。',
  amount_required: '单据没有可对账的金额；请先核对金额。',
  amount_conflict: '单据申报金额与所选项目合计不一致，不能对账或付款。',
  remark_requirement_too_long: '单据要求的附言超出转账备注容量（100 字），不能截断处理；请人工核对。',
  bill_number_required_for_duplicate_check: '缺少缴费单号，无法可靠识别重复缴费；请人工核对单号。',
  'institution:missing': '机构名称未确认，无法识别缴费义务。',
  'bill_number:missing': '缴费单号未确认，无法识别重复缴费。',
  obligation_missing: '未识别到稳定的缴费义务，不能准备付款。',
  selection_required: '缺少必要的项目/收款账户选择，不能准备付款。',
  selection_changed: '选择与已核对内容不一致，请重新核对。',
  recipient_selection_required: '收款人存在多个候选，必须明确选择。',
  recipient_selection_invalid: '所选收款候选不属于当前解析，请重新解析。',
  recipient_unsupported: '模拟系统不支持该收款方式，不会生成成功回执。',
}
function blockingText(code: string): string {
  if (BLOCKING_LABEL[code]) return BLOCKING_LABEL[code]
  const amount = /^([^:]+)\.amount_cents:(.*)$/.exec(code)
  if (amount) return `项目 ${amount[1]} 的金额未确认（${amount[2]}），不能准备付款。`
  const name = /^([^:]+)\.name:(.*)$/.exec(code)
  if (name) return `项目 ${name[1]} 的名称未确认（${name[2]}）。`
  const currency = /^([^:]+)\.currency:(.*)$/.exec(code)
  if (currency) return `项目 ${currency[1]} 的币种不受支持（${currency[2]}）；仅支持 CNY。`
  const payee = /^payee_accounts\[(\d+)\]\.([^:]+):(.*)$/.exec(code)
  if (payee) return `收款账户 ${Number(payee[1]) + 1} 的 ${payee[2] === 'account_number' ? '账号' : '户名'}未确认（${payee[3]}）。`
  if (code.startsWith('document:')) return `单据疑点：${code.slice('document:'.length)}。请对照原图核实或排除该单据。`
  return `服务端阻断：${code}（未识别的阻断原因也不能视为通过）。`
}
export function blockingMessages(blocking: string[] | null | undefined): { code: string; text: string }[] {
  return (blocking || []).map(code => ({ code, text: blockingText(code) }))
}

/** Selection gates that the server would enforce with a request-level error. */
export function selectionGates(doc: PaymentDocument, selection: { itemIds: string[]; payeeIndex: number | null; resolution: RecipientResolution | null }): { code: string; text: string }[] {
  const gates: { code: string; text: string }[] = []
  const items = doc.fields?.items || []
  if (!doc.ready) gates.push({ code: 'document_not_ready', text: '服务端尚未把该单据标记为可准备（ready=false）；请先按阻断原因核对并保存。' })
  if (!selection.itemIds.length) gates.push({ code: 'item_selection_required', text: BLOCKING_LABEL.item_selection_required })
  else {
    const tokens = new Set(items.map(item => item.item_id))
    if (selection.itemIds.some(id => !tokens.has(id))) gates.push({ code: 'item_selection_invalid', text: BLOCKING_LABEL.item_selection_invalid })
  }
  if (items.length > 1 && selection.payeeIndex == null && (doc.fields?.payee_accounts || []).length > 1) gates.push({ code: 'payee_selection_required', text: BLOCKING_LABEL.payee_selection_required })
  if (!selection.resolution) gates.push({ code: 'recipient_unresolved', text: '尚未取得有效的收款人解析，不能准备付款。' })
  else if (selection.resolution.status !== 'resolved') gates.push({ code: 'recipient_not_resolved', text: `收款人解析状态为 ${selection.resolution.status}，不能准备付款。` })
  else if (!selection.resolution.selected_candidate_id) gates.push({ code: 'recipient_not_selected', text: '收款人存在候选，必须在收款解析中明确选择，不能默认第一项。' })
  return gates
}

export function prepareGates(doc: PaymentDocument, selection: { itemIds: string[]; payeeIndex: number | null; resolution: RecipientResolution | null }): { code: string; text: string }[] {
  const server = blockingMessages(doc.blocking)
  const local = selectionGates(doc, selection)
  const seen = new Set(server.map(g => g.code))
  return [...server, ...local.filter(g => !seen.has(g.code))]
}

export function duplicateNotice(doc: PaymentDocument): { tone: 'warn' | 'bad' | 'idle'; text: string } {
  if (doc.repeated_upload) return { tone: 'warn', text: '这张图片已存在，已打开本人已有单据结果；未重复识别，也不能因换图或换请求键生成第二笔付款。' }
  if (doc.links?.length) return { tone: 'idle', text: `本系统内已有 ${doc.links.length} 条关联流水记录；关联不生成扣款，余额以服务端回读为准。` }
  if (doc.preparation && ['preparing', 'draft_ready', 'prepared'].includes(doc.preparation.state)) return {
    tone: 'bad',
    text: `该缴费义务已有付款准备记录（${doc.preparation.state}${doc.preparation.task_id ? `，任务 ${doc.preparation.task_id}` : ''}）。请先核实原任务，不能重复准备第二笔。`,
  }
  if (doc.obligation) return {
    tone: 'warn',
    text: `系统已按机构/单号/缴费人/期间识别到同一缴费义务（${doc.obligation.obligation_id}，${doc.obligation.state}）。换一张图片或换请求键都不会生成新的义务；金额不同时服务端会阻断。`,
  }
  return { tone: 'idle', text: '' }
}

/** Recompute the payable amount only from items the user explicitly selected. */
export function selectedAmountCents(items: DocumentItem[] | null | undefined, selectedIds: string[]): number | null {
  const chosen = (items || []).filter(item => selectedIds.includes(item.item_id))
  if (!chosen.length) return null
  let total = 0
  for (const item of chosen) {
    const value = item.amount_cents?.value
    if (value == null || !Number.isSafeInteger(value)) return null
    total += value
  }
  return total
}

export const RECONCILE_OUTCOME: Record<ReconciliationRun['match_status'], { label: string; tone: 'ok' | 'warn' | 'bad' }> = {
  unique_match: { label: '唯一匹配', tone: 'ok' },
  multiple_candidates: { label: '多个候选（必须选择）', tone: 'warn' },
  not_found: { label: '未找到（不能当作已支付）', tone: 'warn' },
  conflict: { label: '冲突（不能关联）', tone: 'bad' },
}

export function reconcileGating(run: ReconciliationRun | null, selectedTransactionId: number | null, kind: PaymentNoticeKind): string[] {
  if (!run) return ['尚未执行流水核对。']
  if (kind === 'invoice') return ['发票不能作为已付款凭证，不能关联流水；也不会生成扣款。']
  if (run.match_status === 'conflict') return ['核对结果存在冲突，不能关联；请逐项核对实际比较字段。']
  if (run.match_status === 'not_found') return ['本系统未找到匹配流水。未找到不能证明已支付，也不能生成扣款。']
  if (selectedTransactionId == null) {
    return [run.match_status === 'multiple_candidates' ? '存在多个候选流水，必须明确选择一笔，不能默认第一项。' : '请确认所选流水后再关联。']
  }
  const candidate = run.candidates.find(c => c.transaction_id === selectedTransactionId)
  if (!candidate) return ['所选流水不在本次对账候选中，未关联。']
  if (candidate.contradictions.length) return [`所选流水存在矛盾字段：${candidate.contradictions.join('、')}；不能关联。`]
  return []
}

export function candidateRows(match: ReconcileCandidate): { label: string; value: string }[] {
  return [
    { label: '流水编号', value: String(match.transaction_id) },
    { label: '金额', value: `${formatCents(match.amount_cents)}（${match.direction === 'expense' ? '支出' : '收入'}）` },
    { label: '日期', value: match.date },
    { label: '商户/收款方', value: match.merchant || '缺失' },
    { label: '卡尾号', value: match.card_last4 || '缺失' },
    { label: '已比较字段', value: match.compared.map(c => `${c.field}${c.match ? '✓' : '✗'}`).join('、') || '无' },
    { label: '矛盾字段', value: match.contradictions.join('、') || '无' },
  ]
}

/** The document itself must never be used as proof that money moved. */
export const DOCUMENT_MONEY_DISCLOSURE = '发票、缴费通知或用户确认都不能证明已经付款；只有服务端核验的交易流水/回执才是付款事实。'
export const UNKNOWN_NOT_PASS = '未知不等于通过。'
export const PREPARE_NOT_PAYMENT = '准备付款只复用现有转账草稿与任务，不等于付款；付款仍须预览、验证和明确确认。'

/** Contract error codes that make the current document/resolution evidence stale. */
export const RE_READ_PAYMENT_ERRORS = new Set([
  'document_revision_conflict', 'document_digest_mismatch', 'document_changed', 'document_state_conflict',
  'document_not_ready', 'document_payment_completed', 'document_preparation_active', 'stale_revision',
  'recipient_changed', 'resolution_expired', 'reconciliation_stale', 'transaction_changed',
  'payment_obligation_already_prepared', 'payment_obligation_already_paid', 'obligation_amount_conflict',
  'selection_changed', 'client_request_conflict', 'extraction_in_progress',
])

export const PAYMENT_ERROR_HINT: Record<string, string> = {
  document_revision_conflict: '单据版本已变化。请重新读取当前单据并重新核对；旧核对、旧预览与旧确认均已失效。',
  document_digest_mismatch: '核对摘要已失效。请重新读取当前单据后重新核对与准备。',
  document_changed: '单据内容已变化，旧证据失效；请重新读取并核对。',
  document_state_conflict: '单据当前状态不允许该操作；请重新读取单据状态后再处理。',
  document_not_ready: '服务端尚未把该单据标记为可准备付款（ready=false）；请按阻断原因继续核对。',
  document_payment_completed: '该单据关联的付款任务已完成，不能修改已执行付款的核对内容。',
  document_preparation_active: '该单据仍有进行中的付款任务，需先处理原任务才能修改核对内容。',
  stale_revision: '版本已变化，请重新读取后再操作。',
  recipient_changed: '收款人信息已变化，旧解析与旧选择失效；请重新解析并重新选择。',
  resolution_expired: '收款解析已过期；请重新解析，不能复用旧选择。',
  reconciliation_stale: '对账记录或单据已变化，旧对账结果失效；请重新对账。',
  transaction_changed: '流水在核对后已变化，旧证据失效；请重新对账后再选择。',
  payment_obligation_already_prepared: '同一缴费义务已有进行中的付款准备/任务，禁止重复准备；请先处理原任务。',
  payment_obligation_already_paid: '该缴费义务已有已完成的付款任务，禁止再次准备付款。',
  obligation_amount_conflict: '同一缴费义务已存在但金额不同；未生成新义务，请核对单据或联系管理员。',
  client_request_conflict: '同一个请求键被用于不同内容，服务端拒绝重复执行；请重新读取状态后再继续。',
  payment_preparation_unknown: '准备任务创建结果未知。请用原草稿/准备引用核实（GET /payment-preparations/{id}、任务区），不要换请求键重提。',
  vision_budget_exhausted: '今日识图真实调用预算已用完，未发起新的模型请求；可稍后处理或先核实已有单据/任务。',
  vision_capacity_exhausted: '识图并发已满并等待超时，未发起新的模型请求；请稍后重试，不影响已存在单据的读取与核实。',
  vision_not_configured: '识图模型未配置，未发起模型请求；可先核对已有单据或使用业务表单。',
  extraction_in_progress: '单据正在识别中（租期 90 秒）；请先读取当前状态，不要重复触发识别。',
  invoice_not_payment_evidence: '发票不能作为已付款凭证；未关联流水，也未生成扣款。',
  document_kind_not_payment_evidence: '该单据类型不能关联付款流水；未关联流水，也未生成扣款。',
  reconciliation_not_linkable: '对账结果不是可关联状态，不能保存关联；请重新核对。',
  transaction_not_in_candidates: '所选流水不在本次对账候选中，未关联。',
  payee_account_masked_unknown: '回传的收款账号掩码与当前单据不一致，服务端未猜配；请重新读取再核对。',
  remark_requirement_too_long: '单据附言要求超过 100 字，不能截断；请人工核对后再决定。',
  image_size_unsupported: '图片超过 10MiB，未上传。',
  image_format_unsupported: '仅支持 JPEG/PNG 图片。',
  image_invalid: '图片无法解码，未上传。',
  payment_internal_error: '缴费单据服务内部错误；未完成的操作可读取当前状态核实，不要盲目重试。',
  service_unavailable: '服务暂不可用；请稍后读取当前状态核实，不要重复提交写操作。',
}
export function paymentErrorText(code: string | null | undefined, message?: string | null): string {
  if (!code) return message || '操作未完成，请回读当前状态后再试。'
  return PAYMENT_ERROR_HINT[code] || message || `操作未完成（${code}）；请回读当前单据/任务状态后再试。`
}
export function paymentErrorNeedsRead(code: string | null | undefined): boolean {
  return !!code && RE_READ_PAYMENT_ERRORS.has(code)
}

/** Server says extract is allowed only for these states (or a stale `extracting` lease). */
export function extractAllowed(state: PaymentDocument['state']): boolean {
  return state === 'uploaded' || state === 'extraction_failed'
}

export interface PrepareOutcome {
  tone: 'info' | 'ok' | 'warn' | 'bad'
  lines: string[]
  hasPreview: boolean
  taskId: string | null
}
/**
 * Summarise a prepare-payment response. The return of this endpoint is a task
 * response, never a payment result: the UI must direct the user to the existing
 * preview/confirm/TOTP flow and must not render "已付款".
 */
export function prepareOutcome(result: { preparation: { state: string; draft_reference: string | null; task_id: string | null; amount_cents: number }; task?: { preview?: { permission_level?: string; expires_at?: string; confirmation_message?: string } | null } | null; replayed?: boolean }): PrepareOutcome {
  const prep = result.preparation
  const hasPreview = !!result.task?.preview
  const lines: string[] = []
  lines.push(`准备状态：${prep.state}；草稿 ${prep.draft_reference || '未返回'}；任务 ${prep.task_id || '未返回'}；金额 ${formatCents(prep.amount_cents)}。`)
  if (prep.state === 'preparing' || prep.state === 'draft_ready') {
    lines.push('任务尚未创建完成。请保留原草稿/准备引用核实，不要重复准备或换请求键重提。')
  }
  if (result.replayed) lines.push('本次响应对应同键回读（replayed），未新建草稿或任务。')
  if (hasPreview) {
    const preview = result.task!.preview!
    lines.push(`已取得任务预览（${preview.permission_level === 'red' ? '需要强验证' : '需要明确确认'}，${preview.expires_at ? `有效至 ${new Date(preview.expires_at).toLocaleString('zh-CN')}` : '有效期未知'}）。${PREPARE_NOT_PAYMENT}`)
  } else if (prep.task_id) {
    lines.push('任务已建立但本响应未携带预览；请到任务区重新读取最新预览，旧预览/旧验证码不能复用。')
  }
  return {
    tone: prep.state === 'prepared' && hasPreview ? 'info' : 'warn',
    lines, hasPreview, taskId: prep.task_id,
  }
}

export function staleLinkNotice(links: PaymentDocument['links'] | null | undefined): string {
  const stale = (links || []).filter(link => link.stale)
  const current = (links || []).filter(link => !link.stale)
  const parts = []
  if (current.length) parts.push(`${current.length} 条当前版本有效关联：${current.map(l => l.transaction_id).join('、')}。`)
  if (stale.length) parts.push(`${stale.length} 条旧版本关联（stale）只作历史证据，不随新版本自动成立。`)
  return parts.join(' ')
}

/**
 * A document's `payment_status=found_in_this_system` can currently be built from
 * stale links (A backend fix pending). Surface which stale links the verification
 * evidence references so the UI never presents them as current payment proof.
 */
export function staleVerificationEvidence(doc: PaymentDocument | null | undefined): string[] {
  if (!doc) return []
  const stale = new Map((doc.links || []).filter(link => link.stale).map(link => [link.link_id, link]))
  const evidence = doc.verification?.payment_status?.evidence || []
  const found = new Set<string>()
  for (const item of evidence) {
    const reference = typeof item.reference === 'string' ? item.reference : ''
    if (stale.has(reference)) found.add(reference)
  }
  // If the server says "found" but its evidence references only stale links (or the
  // evidence list cannot be tied to a current link), still flag the uncertainty.
  if (!found.size && doc.verification?.payment_status?.status === 'found_in_this_system' && stale.size) {
    for (const id of stale.keys()) found.add(id)
  }
  return [...found]
}

/** Fields the user may edit in review; the UI submits the whole Extraction tree back. */
export function withItemAmount(fields: Extraction, itemId: string, cents: number | null): Extraction {
  return {
    ...fields,
    items: fields.items.map(item => item.item_id === itemId
      ? { ...item, amount_cents: { ...item.amount_cents, value: cents, origin: 'user' as const, issues: [] } }
      : item),
  }
}
export function withItemName(fields: Extraction, itemId: string, name: string | null): Extraction {
  return {
    ...fields,
    items: fields.items.map(item => item.item_id === itemId
      ? { ...item, name: { ...item.name, value: name, origin: 'user' as const } }
      : item),
  }
}
export function withKind(fields: Extraction, kind: PaymentNoticeKind): Extraction {
  return { ...fields, kind }
}
export function withText(fields: Extraction, key: 'payer_name' | 'institution' | 'bill_number' | 'period_label' | 'due_date' | 'remark_requirement', value: string | null): Extraction {
  return { ...fields, [key]: { ...fields[key], value, origin: 'user' as const } }
}
export function withPayeeAccount(fields: Extraction, index: number, key: 'account_name' | 'bank_name', value: string | null): Extraction {
  return {
    ...fields,
    payee_accounts: fields.payee_accounts.map((payee, i) => i === index
      ? { ...payee, [key]: { ...payee[key], value, origin: 'user' as const } }
      : payee),
  }
}
