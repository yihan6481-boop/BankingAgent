import { useEffect, useRef, useState, type ReactNode } from 'react'
import { api } from '../../api'
import { ApiError,request } from '../../api/client'
import { payments } from '../../api/payments'
import type {
  Extraction, FieldValue, PaymentDocument, PaymentPreparation, PaymentNoticeKind, ReconcileCandidate,
  ReconciliationRun, RecipientResolution,
} from '../../api/payments'
import type { CardItem } from '../../types'
import {
  DOCUMENT_MONEY_DISCLOSURE, PREPARE_NOT_PAYMENT, RECONCILE_OUTCOME, UNKNOWN_NOT_PASS,
  VERIFICATION_STATUS, blockingMessages, candidateRows, centsText, duplicateNotice, evidenceText,
  extractAllowed, fieldText, formatCents, kindLabel, parseCents, paymentErrorNeedsRead, paymentErrorText,
  prepareGates, prepareOutcome, reconcileGating, selectedAmountCents, staleLinkNotice, staleVerificationEvidence,
  verificationRows, withItemAmount, withKind, withText,
} from './paymentModel'
import {
  beginPending, canDropPending, clearPending, newRequestId, pendingSummary, readPending, recoveryMode,
  writePending,
} from './preparationIdentity'
import type { PendingPreparation, RecoveryMode } from './preparationIdentity'

type PanelError = { code: string; message: string }
type PreparedView = ReturnType<typeof prepareOutcome>
type ServerPreparation = { preparation_id: string; state: string; draft_reference: string | null; task_id: string | null; amount_cents: number; from_card_id: number }

export function VerificationMatrix({ doc }: { doc: PaymentDocument }) {
  return <section className="payment-verification" aria-label="四项核验">
    <h4>核验（四项分开，不合并成“已验证”）</h4>
    <ul>{verificationRows(doc.verification).map(row => {
      const meta = VERIFICATION_STATUS[row.status] || VERIFICATION_STATUS.unknown
      return <li key={row.key} data-verification={row.key} data-status={row.status}>
        <span>{row.label}</span><span className={`status-layer-state ${meta.tone === 'ok' ? 'bg-emerald-50 text-emerald-700' : meta.tone === 'bad' ? 'bg-rose-50 text-rose-700' : meta.tone === 'idle' ? 'bg-slate-100 text-slate-500' : 'bg-amber-50 text-amber-800'}`}>{meta.label}</span>
        <small>证据 {row.evidence} 条</small>
      </li>
    })}</ul>
    <p className="muted-copy">{DOCUMENT_MONEY_DISCLOSURE} {UNKNOWN_NOT_PASS}模拟目录匹配也不等于现实银行的账户归属认证。</p>
  </section>
}

function FieldRow({ label, field, editable, onEdit }: {
  label: string
  field: FieldValue<string>
  editable?: boolean
  onEdit?: (text: string) => void
}) {
  const value = fieldText(field)
  return <div className="payment-field" data-field-label={label}>
    <dt>{label}<span className="payment-origin">{field?.value == null ? '缺失' : field.origin === 'user' ? '用户修改' : '图片提取'}</span></dt>
    <dd>
      {editable ? <input aria-label={label} className="field w-full" value={value === '未提取' ? '' : value} onChange={e => onEdit?.(e.target.value)} /> : value}
      {(field?.issues || []).map((issue, i) => <p key={i} className="text-xs text-amber-800">疑点：{issue}</p>)}
      {!!field?.evidence?.length && <details><summary>原图证据</summary>{evidenceText(field).map((e, i) => <p key={i} className="text-xs">{e}</p>)}</details>}
      {field?.confidence == null && field?.value != null && <p className="text-xs text-slate-400">置信度未校准：显示为空，不当作高置信。</p>}
    </dd>
  </div>
}

function ItemRow({ item, checked, onCheck, amountText, onAmount }: {
  item: Extraction['items'][number]; checked: boolean; onCheck: (v: boolean) => void; amountText: string; onAmount: (v: string) => void
}) {
  return <article className={`payment-item ${checked ? 'selected' : ''}`} data-item-id={item.item_id}>
    <label className="flex gap-2 items-center"><input type="checkbox" aria-label={`选择缴费项目 ${item.name.value || item.item_id}`} checked={checked} onChange={e => onCheck(e.target.checked)} /><strong>{item.name.value || '未命名项目'}</strong></label>
    <label className="block">应付金额（元）<input aria-label={`项目金额 ${item.name.value || item.item_id}`} className="field w-full" inputMode="decimal" value={amountText} onChange={e => onAmount(e.target.value)} /></label>
    <p className="text-xs">提取值：{formatCents(item.amount_cents.value)}（{item.amount_cents.origin === 'user' ? '用户修改' : '图片提取'}）；币种：{item.currency || '未提取'}；提取项目号 {item.item_id}</p>
    {(item.amount_cents.issues || []).map((issue, i) => <p key={i} className="text-xs text-amber-800">疑点：{issue}</p>)}
  </article>
}

export function PaymentDocumentPanel({ sid, userId, busy, resolution, onOpenTasks, onPrepared,onInvoice,recipientPanel }: {
  sid: string
  /** Authenticated user id; the pending request identity is bound to it. */
  userId: number
  busy: boolean
  resolution: RecipientResolution | null
  onOpenTasks: () => void
  onPrepared?: (taskId: string | null) => void
  onInvoice?: (documentId:string)=>void
  recipientPanel?: ReactNode
}) {
  const [documents, setDocuments] = useState<Pick<PaymentDocument, 'document_id' | 'revision' | 'state' | 'kind' | 'created_at'>[]>([])
  const [doc, setDoc] = useState<PaymentDocument | null>(null)
  const [fields, setFields] = useState<Extraction | null>(null)
  const [dirty, setDirty] = useState(false)
  const [items, setItems] = useState<string[]>([])
  const [payeeIndex, setPayeeIndex] = useState<number | null>(null)
  const [amounts, setAmounts] = useState<Record<string, string>>({})
  const [remark, setRemark] = useState('')
  const [paymentRemark, setPaymentRemark] = useState('')
  const [ownBill, setOwnBill] = useState(false)
  const [image, setImage] = useState('')
  const [working, setWorking] = useState(false)
  const [error, setError] = useState<PanelError | null>(null)
  const [notice, setNotice] = useState('')
  const [cancelledTaskIds, setCancelledTaskIds] = useState<string[]>([])
  const [cards, setCards] = useState<CardItem[]>([])
  const [fromCardId, setFromCardId] = useState('')
  const [preparation, setPreparation] = useState<ServerPreparation | null>(null)
  const [prepared, setPrepared] = useState<PreparedView | null>(null)
  const [pending, setPending] = useState<PendingPreparation | null>(null)
  const [serverRead, setServerRead] = useState<PaymentPreparation | null>(null)
  const [verifyChecked, setVerifyChecked] = useState(false)
  const [run, setRun] = useState<ReconciliationRun | null>(null)
  const [selectedTransaction, setSelectedTransaction] = useState<number | null>(null)
  const [linked, setLinked] = useState<{ link_id: string; transaction_id: number; ledger_unchanged: boolean } | null>(null)
  const lock = useRef(false)
  const imageRef = useRef('')
  const alive = useRef(true)
  const mount = useRef(false)
  const storage = () => (typeof window !== 'undefined' && window.sessionStorage ? window.sessionStorage : null)
  useEffect(() => {
    alive.current = true
    if (!mount.current) { mount.current = true; void refresh() }
    return () => { alive.current = false; if (imageRef.current) URL.revokeObjectURL(imageRef.current) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid])
  useEffect(() => {
    void api.read(sid, 'get_cards', {}).then(out => {
      if (alive.current && out.result?.kind === 'card_list') setCards(out.result.data)
    }).catch(() => { /* card directory optional */ })
  }, [sid])
  useEffect(() => { if (doc) setPending(readPending(storage(), userId, doc.document_id)) }, [doc, userId])
  function fail(e: unknown, fallback: string) {
    if (!alive.current) return
    setError({ code: e instanceof ApiError ? e.code : '', message: e instanceof Error ? e.message : fallback })
    return e
  }
  async function refresh() {
    try {
      const value = await payments.list(sid)
      if (alive.current) setDocuments(value.documents || [])
    } catch (e) { fail(e, '单据列表读取失败。') }
  }
  function apply(value: PaymentDocument) {
    setDoc(value); setFields(value.fields); setDirty(false); setCancelledTaskIds(value.cancelled_task_ids || [])
    setItems([...value.selected_item_ids]); setPayeeIndex(value.selected_payee_index)
    setAmounts(Object.fromEntries((value.fields?.items || []).map(item => [item.item_id, centsText(item.amount_cents.value)])))
    setRemark(value.fields?.remark_requirement?.value || '')
    setOwnBill(!!value.user_confirmed_own_bill)
    setPreparation(value.preparation ? { ...value.preparation } : null)
    setRun(null); setSelectedTransaction(null); setLinked(null); setError(null); setPrepared(null); setServerRead(null); setVerifyChecked(false)
  }
  async function open(value: Pick<PaymentDocument, 'document_id'>) {
    if (lock.current) return
    lock.current = true; setWorking(true); setError(null); setNotice('')
    try {
      const latest = await payments.get(sid, value.document_id)
      if (!alive.current) return
      apply(latest)
      if (imageRef.current) URL.revokeObjectURL(imageRef.current)
      imageRef.current = ''; setImage('')
      if (latest.image?.sha256) {
        const blob = await payments.image(sid, latest.document_id)
        if (!alive.current) return
        imageRef.current = URL.createObjectURL(blob); setImage(imageRef.current)
      }
      setNotice(`已回读单据当前版本（rev ${latest.revision}）${latest.replayed ? '，本次为同键回读' : ''}；核对修改会生成新版本并使旧授权失效。`)
    } catch (e) { fail(e, '单据读取未完成，请重新打开核对。') }
    finally { lock.current = false; if (alive.current) setWorking(false) }
  }
  async function upload(file: File) {
    if (!['image/png', 'image/jpeg'].includes(file.type) || file.size > 10 * 1024 * 1024) { setError({ code: 'image_size_unsupported', message: '请选择 10MiB 以内的 PNG/JPEG 图片。' }); return }
    if (lock.current) return
    lock.current = true; setWorking(true); setError(null); setNotice('正在上传单据…')
    try {
      const created = await payments.create(sid, file)
      if (!alive.current) return
      const latest = await payments.get(sid, created.document_id)
      if (!alive.current) return
      apply(latest)
      if (imageRef.current) URL.revokeObjectURL(imageRef.current)
      imageRef.current = ''; setImage('')
      if (latest.image?.sha256) {
        const blob = await payments.image(sid, latest.document_id)
        if (!alive.current) return
        imageRef.current = URL.createObjectURL(blob); setImage(imageRef.current)
      }
      setNotice(created.repeated_upload
        ? '这张图片已存在，已打开本人已有单据结果；未重复识别，也不会重复付款。'
        : `已上传并回读单据（rev ${latest.revision}）${latest.replayed ? '，本次为同键回读' : ''}。点击“提取票据信息”后逐项核对。`)
      await refresh()
    } catch (e) { fail(e, '上传未完成；结果未知时请刷新本人单据列表核对，不要重复上传。') }
    finally { lock.current = false; if (alive.current) setWorking(false) }
  }
  async function extract() {
    if (!doc || lock.current) return
    lock.current = true; setWorking(true); setError(null); setNotice('正在提取单据信息…')
    try {
      const value = await payments.extract(sid, doc)
      if (!alive.current) return
      apply(value)
      setNotice(value.state === 'extraction_failed' ? '提取失败：可重新提取；不会生成付款草稿。' : '提取完成，请对照原图逐项核对金额、收款账户、期间和疑点。用户修改会保留原始提取证据。')
      await refresh()
    } catch (e) { fail(e, '提取未完成；超时或响应丢失时请重新打开单据核实，不要盲目重发。') }
    finally { lock.current = false; if (alive.current) setWorking(false) }
  }
  function edit(update: (value: Extraction) => Extraction) {
    if (!fields) return
    setFields(update(fields)); setDirty(true)
  }
  async function review() {
    if (!doc || !fields || lock.current) return
    const invalid = Object.entries(amounts).filter(([, text]) => text.trim() !== '' && parseCents(text) == null)
    if (invalid.length) { setError({ code: 'invalid_amount', message: '金额格式错误：请输入最多两位小数的元数。' }); return }
    let next = fields
    for (const [itemId, text] of Object.entries(amounts)) next = withItemAmount(next, itemId, text.trim() === '' ? null : parseCents(text))
    next = withText(next, 'remark_requirement', remark.trim() || null)
    lock.current = true; setWorking(true); setError(null); setNotice('正在核对写回前的最新单据版本…')
    try {
      // A's current review route cancels the linked task BEFORE validating revision/digest
      // (A must fix the ordering). As a mitigation, never send a review whose base version
      // is already stale: pre-read and compare, so an ordinary stale edit cannot trigger
      // the cancel side effect. The server still remains the authority.
      const latest = await payments.get(sid, doc.document_id)
      if (!alive.current) return
      if (latest.revision !== doc.revision || latest.digest !== doc.digest) {
        setError({ code: 'document_revision_conflict', message: '单据已在别处变化（版本或摘要不同）。本次未提交核对，因此不会触发旧任务取消；请重新读取并核对最新版本。' })
        setDoc(latest); setFields(latest.fields); setItems([...latest.selected_item_ids]); setPayeeIndex(latest.selected_payee_index)
        return
      }
      setNotice('正在保存核对内容…')
      const value = await payments.review(sid, doc, {
        kind: next.kind, fields: next, selected_item_ids: items, selected_payee_index: payeeIndex,
        user_notes: null, user_confirmed_own_bill: ownBill,
      })
      if (!alive.current) return
      apply(value)
      const cancelled = value.cancelled_task_ids || []
      setNotice(cancelled.length
        ? `核对已保存为新版本（rev ${value.revision}），并已取消旧付款任务 ${cancelled.join('、')}；旧预览/确认/TOTP 全部失效。${value.ready ? '服务端认为可以重新准备付款。' : '请继续按阻断原因核对。'}`
        : value.ready ? '核对已保存为新版本，服务端认为可以准备付款；请继续核对收款解析，不会自动付款。' : '核对已保存为新版本，但仍有阻断项；请按上方阻断原因继续核对。')
      await refresh()
    } catch (e) { fail(e, '核对保存未完成，请回读单据版本后再试。') }
    finally { lock.current = false; if (alive.current) setWorking(false) }
  }
  /** Send (or replay) one exact preparation request identity. */
  async function sendPrepare(record: PendingPreparation, label: string) {
    if (!doc) return
    setWorking(true); setError(null); setNotice(label)
    try {
      const result = await payments.prepare(sid, { document_id: doc.document_id, revision: record.revision, digest: record.digest }, {
        item_ids: record.item_ids, recipient_resolution_id: record.recipient_resolution_id,
        from_card_id: record.from_card_id, remark: record.remark,
      }, record.client_request_id)
      if (!alive.current) return
      clearPending(storage(), userId, record.document_id)
      setPending(null)
      setPreparation(result.preparation); if (result.document) setDoc(result.document)
      const outcome = prepareOutcome({ preparation: result.preparation, task: result.task || null, replayed: result.replayed })
      setPrepared(outcome)
      setNotice(result.replayed ? '本次为同键回读：未新建草稿或任务。' : '准备请求已由服务端回执确认。')
      await refresh()
    } catch (e) {
      if (!alive.current) return
      const code = e instanceof ApiError ? e.code : null
      const data = e instanceof ApiError ? (e.data as { preparation?: ServerPreparation; draft?: { reference?: string }; task_id?: string } | null) : null
      // Learn server facts from the error body without guessing.
      if (data?.preparation) {
        setPreparation(data.preparation)
        if (data.preparation.preparation_id) {
          const updated = { ...record, preparation_id: data.preparation.preparation_id, state: data.preparation.state, task_id: data.preparation.task_id }
          writePending(storage(), updated); setPending(updated)
        }
      } else if (data?.task_id) {
        const updated = { ...record, task_id: data.task_id }
        writePending(storage(), updated); setPending(updated)
      }
      if (canDropPending(record, code)) {
        clearPending(storage(), userId, record.document_id); setPending(null)
        setError({ code: code || '', message: paymentErrorText(code, e instanceof Error ? e.message : '准备未开始，已清除本地请求身份（服务端未产生准备）。') })
      } else {
        // Unknown/5xx/network: KEEP the identity. Do not mint a new key.
        setError({
          code: code || 'network_error',
          message: `${paymentErrorText(code, e instanceof Error ? e.message : '准备结果未知')} 已保留原请求键，不会换键重提；请先核实服务端准备/任务事实。`,
        })
      }
      await refresh().catch(() => null)
    } finally { if (alive.current) setWorking(false) }
  }
  const mode: RecoveryMode = recoveryMode(doc, pending)
  function startPrepare() {
    if (!doc || !fields || lock.current || !resolution) return
    const gates = prepareGates(doc, { itemIds: items, payeeIndex, resolution })
    if (gates.length) { setError({ code: gates[0].code, message: gates[0].text }); return }
    if (mode !== 'none') { setError({ code: 'preparation_recovery_required', message: '存在未完成/未知的付款准备身份，必须先核实；不会自动换键重新准备。' }); return }
    if (!Number.isSafeInteger(Number(fromCardId)) || Number(fromCardId) <= 0) { setError({ code: 'card_not_found', message: '请选择付款卡。' }); return }
    if (paymentRemark.length > 100) { setError({ code: 'remark_requirement_too_long', message: '转账附言最多 100 字，请缩短后再准备。' }); return }
    // Persist the identity BEFORE the request so refresh/network loss can replay it.
    const record = beginPending(storage(), {
      client_request_id: newRequestId(), user_id: userId, document_id: doc.document_id,
      revision: doc.revision, digest: doc.digest, item_ids: [...items],
      recipient_resolution_id: resolution.resolution_id, from_card_id: Number(fromCardId),
      remark: paymentRemark.trim() || null,
    })
    setPending(record)
    if (lock.current) return
    lock.current = true
    void sendPrepare(record, '正在通过现有转账草稿与任务流程准备付款；不会自动执行…').finally(() => { lock.current = false })
  }
  /** A formal recovery: read server preparation facts; only replay same-key when unknown. */
  async function recover() {
    if (!doc || !pending || lock.current) return
    lock.current = true; setWorking(true); setError(null)
    try {
      // 1) Server-side facts first when we already know the preparation id.
      if (pending.preparation_id) {
        try {
          const value = await payments.readPreparation(sid, pending.preparation_id)
          if (!alive.current) return
          setServerRead(value.preparation)
          const state = value.preparation.state
          if (state === 'prepared' || value.preparation.task_id) {
            setPreparation({
              preparation_id: value.preparation.preparation_id, state, draft_reference: value.preparation.draft_reference,
              task_id: value.preparation.task_id, amount_cents: value.preparation.amount_cents, from_card_id: value.preparation.from_card_id,
            })
            setNotice('服务端已有准备记录，未重复提交。请到任务区核实预览/确认/验证状态。')
            return
          }
          if (state === 'cancelled' || state === 'failed') {
            clearPending(storage(), userId, doc.document_id); setPending(null)
            setError({ code: 'preparation_cancelled', message: '服务端准备记录已结束；本地请求身份已清除，可重新核对后再准备。' })
            return
          }
          setNotice(`服务端准备记录状态 ${state}；未重复创建任务。可继续到任务区核实，或按需同键回读。`)
          return
        } catch (e) {
          if (e instanceof ApiError && e.status === 404) {
            clearPending(storage(), userId, doc.document_id); setPending(null)
            setError({ code: 'preparation_not_found', message: '服务端没有该准备记录；本地请求身份已清除。请核实任务区后重新核对准备。' })
            return
          }
          throw e
        }
      }
      // 2) No preparation id known: re-send the exact same request identity (server replay/resume).
      await sendPrepare(pending, '正在用原请求键核实原付款准备（不会新建第二笔）…')
    } catch (e) {
      fail(e, '核实原付款准备未完成；已保留原请求键，请稍后重试同键核实，不要换键重提。')
    } finally { lock.current = false; if (alive.current) setWorking(false) }
  }
  async function verifyFacts() {
    if (!doc) return
    await open(doc)
    if (pending?.preparation_id) {
      try {
        const value = await payments.readPreparation(sid, pending.preparation_id)
        if (alive.current) setServerRead(value.preparation)
      } catch (e) { fail(e, '准备记录读取未完成。') }
    }
    onOpenTasks()
  }
  function dropIdentity() {
    if (!doc || !pending || !verifyChecked) return
    clearPending(storage(), userId, doc.document_id); setPending(null); setServerRead(null); setVerifyChecked(false)
    setNotice('已清除本地请求身份。注意：这不取消任何服务端准备/任务；如服务端仍有记录，请以任务区为准。')
  }
  async function reconcile() {
    if (!doc || !fields || lock.current) return
    const now = new Date()
    const start = new Date(now.getFullYear(), now.getMonth() - 3, 1)
    const endExclusive = new Date(now.getFullYear(), now.getMonth() + 1, 1)
    const iso = (d: Date) => d.toISOString().slice(0, 10)
    lock.current = true; setWorking(true); setError(null); setNotice('正在核对本人交易流水…')
    try {
      const result = await payments.reconcile(sid, doc, { start: iso(start), end_exclusive: iso(endExclusive) })
      if (!alive.current) return
      setRun(result.reconciliation); setSelectedTransaction(null); setLinked(null)
      if (result.document) setDoc(result.document)
      setNotice(result.reconciliation.match_status === 'not_found' ? '本系统未找到匹配流水；未找到不能证明已付款。'
        : result.reconciliation.match_status === 'conflict' ? '核对存在冲突，需要逐项核对实际比较字段，不能关联。'
          : '核对完成；请核对比较字段与未知字段后再选择流水。')
    } catch (e) { fail(e, '流水核对未完成，请稍后重新核对。') }
    finally { lock.current = false; if (alive.current) setWorking(false) }
  }
  async function link(candidate: ReconcileCandidate) {
    if (!doc || !run || !fields || lock.current) return
    const gates = reconcileGating(run, candidate.transaction_id, doc.kind)
    if (gates.length) { setError({ code: 'reconciliation_not_linkable', message: gates[0] }); return }
    lock.current = true; setWorking(true); setError(null)
    try {
      const result = await payments.link(sid, doc, run, candidate.transaction_id)
      if (!alive.current) return
      setLinked({ link_id: result.link.link_id, transaction_id: result.link.transaction_id, ledger_unchanged: result.ledger_unchanged })
      if (result.document) setDoc(result.document)
      setNotice(result.replayed ? '本次关联为同键回读，未重复关联。' : '已保存凭证与流水关联；关联不新增支出、不改余额（服务端返回 ledger_unchanged）。')
    } catch (e) { fail(e, '关联未完成，请重新对账后再试。') }
    finally { lock.current = false; if (alive.current) setWorking(false) }
  }
  const selection = { itemIds: items, payeeIndex, resolution }
  const gates = doc && fields ? prepareGates(doc, selection) : []
  // A pending/unknown request identity always blocks minting a second key.
  if (mode !== 'none') gates.push({ code: 'preparation_recovery_required', text: '存在未完成/未知的付款准备请求身份；请先核实原准备/任务，不能换键重提。' })
  const blocking = blockingMessages(doc?.blocking)
  const duplicate = doc ? duplicateNotice(doc) : null
  const selectionDirty = !!doc && (items.join(',') !== doc.selected_item_ids.join(',') || payeeIndex !== doc.selected_payee_index)
  const selectedAmount = fields ? selectedAmountCents(fields.items, items) : null
  const linkNotice = doc ? staleLinkNotice(doc.links) : ''
  const staleEvidence = doc ? staleVerificationEvidence(doc) : []
  return <section className="payment-panel" aria-label="缴费单据核对" aria-busy={busy || working}>
    <div className="payment-head"><h3>缴费单据</h3><button type="button" className="secondary" disabled={busy || working} onClick={() => void refresh()}>刷新本人单据</button></div>
    <p className="muted-copy">上传缴费通知、发票或回执图片后，必须对照原图核对。发票/通知本身不能证明已付款；系统只复用现有转账草稿与任务，不提供新的资金执行入口。</p>
    <label className="block">上传缴费单据图片（PNG/JPEG，≤10MiB）<input aria-label="上传缴费单据" type="file" accept="image/png,image/jpeg" disabled={busy || working} onChange={e => { const f = e.target.files?.[0]; e.target.value = ''; if (f) void upload(f) }} /></label>
    {error && <div role="alert" className="mobile-warning" data-error-code={error.code}>
      <p>{error.message || paymentErrorText(error.code)}</p>
      {paymentErrorNeedsRead(error.code) && doc && <button type="button" className="secondary" disabled={busy || working} onClick={() => void open(doc)}>重新读取当前单据</button>}
      {(error.code === 'vision_budget_exhausted' || error.code === 'vision_capacity_exhausted')
        ? <p className="text-xs">已存在的单据和任务仍可读取与核实；不要重复触发识图。</p> : null}
    </div>}
    {notice && <p role="status" className="task-notice">{notice}</p>}
    {!!cancelledTaskIds.length && <p role="alert" className="mobile-warning">核对修改已取消旧付款任务 {cancelledTaskIds.join('、')}；旧预览、确认与 TOTP 全部失效，需重新预览。</p>}
    {!!documents.length && <ul className="payment-list" aria-label="本人缴费单据">{documents.map(d => <li key={d.document_id}><button type="button" className="secondary w-full text-left" disabled={busy || working} onClick={() => void open(d)}>{d.created_at ? new Date(d.created_at).toLocaleString('zh-CN') : d.document_id} · {d.state} · {kindLabel(d.kind)} · rev {d.revision}</button></li>)}</ul>}
    {doc && fields && <div className="payment-detail">
      <h4>当前单据 · {kindLabel(doc.kind)}</h4>
      <details className="muted-copy"><summary>单据来源与版本</summary><p>状态：{doc.state}；版本 {doc.revision}；{doc.ready ? '已具备准备条件' : '仍需核对'}。单据编号 {doc.document_id}；版本摘要 {doc.digest.slice(0, 12)}…；版本变化使旧核对与授权失效。</p></details>
      {image && <img className="payment-image" src={image} alt="当前本人缴费单据原图" />}
      {doc.state === 'extracting' && <p role="status" className="task-notice">服务端正在识别（租期 90 秒）。如超过租期仍未完成，可重新读取单据后再触发识别。</p>}
      <button type="button" className="primary" disabled={busy || working || !extractAllowed(doc.state)} onClick={() => void extract()}>{doc.state === 'extraction_failed' ? '重新提取票据信息' : '提取票据信息'}</button>
      {!extractAllowed(doc.state) && doc.state !== 'extracting' && <p className="text-xs text-slate-500">当前状态不允许重新提取；已人工核对的内容不会被识图覆盖。</p>}
      {duplicate?.text && <p role="alert" className={duplicate.tone === 'bad' ? 'mobile-error' : duplicate.tone === 'warn' ? 'mobile-warning' : 'task-notice'}>{duplicate.text}</p>}
      {!!blocking.length && <div className="payment-issues" aria-label="服务端阻断"><h4>服务端阻断原因（未解决前不能准备付款）</h4>{blocking.map(b => <p key={b.code}>{b.text}</p>)}</div>}
      <dl className="payment-fields">
        <div className="payment-field"><dt>单据类型</dt><dd><select aria-label="单据类型" className="field w-full" value={fields.kind} onChange={e => edit(v => withKind(v, e.target.value as PaymentNoticeKind))}>{(['payment_notice', 'invoice', 'receipt', 'bank_receipt', 'unknown'] as const).map(k => <option key={k} value={k}>{kindLabel(k)}</option>)}</select></dd></div>
        <FieldRow label="缴费人/学生" field={fields.payer_name} editable onEdit={text => edit(v => withText(v, 'payer_name', text || null))} />
        <FieldRow label="机构" field={fields.institution} editable onEdit={text => edit(v => withText(v, 'institution', text || null))} />
        <FieldRow label="单号" field={fields.bill_number} editable onEdit={text => edit(v => withText(v, 'bill_number', text || null))} />
        <FieldRow label="期间" field={fields.period_label} editable onEdit={text => edit(v => withText(v, 'period_label', text || null))} />
        <FieldRow label="期限" field={fields.due_date} editable onEdit={text => edit(v => withText(v, 'due_date', text || null))} />
        <div className="payment-field"><dt>附言要求<span className="payment-origin">{fields.remark_requirement.value == null ? '缺失' : fields.remark_requirement.origin === 'user' ? '用户修改' : '图片提取'}</span></dt><dd><input aria-label="附言要求" className="field w-full" value={remark} onChange={e => { setRemark(e.target.value); setDirty(true) }} disabled={busy || working} />{(fields.remark_requirement.issues || []).map((issue, i) => <p key={i} className="text-xs text-amber-800">疑点：{issue}</p>)}<p className="text-xs text-slate-400">附言超过 100 字时服务端会阻断，不会截断。</p></dd></div>
      </dl>
      {!!(fields.items || []).length && <section aria-label="缴费项目"><h4>缴费项目（必须明确选择）</h4>{(fields.items || []).map(item => <ItemRow key={item.item_id} item={item} checked={items.includes(item.item_id)}
        onCheck={checked => { setItems(prev => checked ? [...new Set([...prev, item.item_id])] : prev.filter(id => id !== item.item_id)) }}
        amountText={amounts[item.item_id] ?? ''} onAmount={text => { setAmounts(v => ({ ...v, [item.item_id]: text })); setDirty(true) }} />)}
        <p className="text-xs">本次选择的应付金额：{formatCents(selectedAmount)}。没有明确选择的项目不参与应付金额；不允许把合计或优惠金额当作应付。</p>
      </section>}
      {!!(fields.payee_accounts || []).length && <section aria-label="收款账户"><h4>收款账户（多个时必须选一个，不得默认首项）</h4>{(fields.payee_accounts || []).map((payee, index) => <label key={index} className="payment-candidate">
        <input type="radio" name="payee-account" checked={payeeIndex === index} onChange={() => setPayeeIndex(index)} />
        <span><strong>{fieldText(payee.account_name)}</strong> · {fieldText(payee.account_number)} · {fieldText(payee.bank_name)}</span>
        {(payee.account_number.issues || []).map((issue, i) => <small key={i} className="text-amber-800">疑点：{issue}</small>)}
      </label>)}</section>}
      <label className="flex gap-2 items-center"><input type="checkbox" checked={ownBill} onChange={e => { setOwnBill(e.target.checked); setDirty(true) }} />我确认这张单据与我本人相关（用户声明会标注来源，不升级为机构认证）</label>
      {(dirty || selectionDirty) && <button type="button" className="secondary" disabled={busy || working} onClick={() => void review()}>保存当前核对（生成新版本，旧授权失效）</button>}
      <VerificationMatrix doc={doc} />
      {!!staleEvidence.length && <p role="alert" className="mobile-warning">当前核验引用了已过期关联（{staleEvidence.join('、')}）：这只能说明“本系统历史流水”，不能作为当前版本已付款证据。服务端应过滤过期关联（A 修复中）。</p>}
      {(mode !== 'none' || serverRead) && <section className="payment-prepare" aria-label="付款准备恢复" data-recovery={mode}>
        <h4>付款准备恢复（原请求身份 / 服务端事实）</h4>
        {preparation && <p className="task-notice">服务端已存在准备记录：{preparation.preparation_id} · {preparation.state} · 草稿 {preparation.draft_reference || '未返回'} · 任务 {preparation.task_id || '未返回'}。不要重复准备；请先核实任务区。</p>}
        {pending && <div className="task-notice" data-pending-key={pending.client_request_id}>
          <p>本地保存了未确认的原请求身份：{pendingSummary(pending)}</p>
          {mode === 'same_key_replay' && <p>单据版本与当时一致。可发送完全相同的请求键与参数，由服务端回读/续跑原准备；不会新建第二笔。</p>}
          {mode === 'stale_identity' && <p>单据版本已变化：旧请求身份不能用于新版本。请先核实服务端准备记录/任务区；确认原准备未产生任务后，才可在下方清除本地身份并重新核对准备。</p>}
        </div>}
        {serverRead && <p className="task-notice">服务端准备记录：{serverRead.preparation_id} · {serverRead.state} · 草稿 {serverRead.draft_reference || '未返回'} · 任务 {serverRead.task_id || '未返回'} · 金额 {formatCents(serverRead.amount_cents)}。</p>}
        <div className="flex gap-2 flex-wrap">
          {pending && mode === 'same_key_replay' && <button type="button" className="primary" disabled={busy || working} onClick={() => void recover()}>同键核实原付款准备</button>}
          {pending && mode === 'stale_identity' && <button type="button" className="secondary" disabled={busy || working} onClick={() => void verifyFacts()}>读取服务端准备/任务事实</button>}
          {pending?.preparation_id && mode !== 'same_key_replay' && <button type="button" className="secondary" disabled={busy || working} onClick={() => void recover()}>读取原准备记录</button>}
          <button type="button" className="secondary" disabled={busy || working} onClick={() => void verifyFacts()}>到任务区核实</button>
        </div>
        {pending && <div className="space-y-2">
          <label className="flex gap-2 items-start text-xs"><input type="checkbox" checked={verifyChecked} onChange={e => setVerifyChecked(e.target.checked)} />我已到任务区/服务端核实：原请求没有产生进行中的准备或任务，且当前单据版本已重新核对。</label>
          <button type="button" className="secondary" disabled={busy || working || !verifyChecked} onClick={dropIdentity}>清除本地请求身份（不取消服务端操作）</button>
          <p className="text-xs text-slate-500">清除只影响本标签页的请求键记录，不会取消服务端准备/任务；如服务端仍有记录，以任务区为准。不会保存密码、TOTP 或确认令牌。</p>
        </div>}
      </section>}
      {doc.kind==='invoice'&&<section className="security-card"><h3>发票记账核对</h3><p>发票不证明付款成功。导入后默认“待支付 / 未核实”，不会扣款，也不会自动计入已支付支出。</p><button className="primary full-width" disabled={busy||working||dirty||!doc.user_confirmed_own_bill} onClick={async()=>{setWorking(true);try{const v=await request<{document:{document_id:string}}>(`/secure/sessions/${sid}/mobile/invoices/${doc.document_id}/bookkeeping`,sid,'POST',{revision:doc.revision,digest:doc.digest});onInvoice?.(v.document.document_id)}catch(e){fail(e,'发票记账核对未完成')}finally{setWorking(false)}}}>进入记账核对</button></section>}
      <section className="payment-prepare" aria-label="准备付款">
        <h4>准备付款（复用现有转账草稿与任务）</h4>
        {recipientPanel}
        <label className="block">付款卡<select aria-label="付款卡" className="field w-full" value={fromCardId} onChange={e => setFromCardId(e.target.value)}><option value="">请选择付款卡</option>{cards.map(c => <option key={c.card_id} value={c.card_id}>{c.bank_name} · {c.masked_card_number} · 余额 {c.balance.toFixed(2)}</option>)}</select></label>
        {!cards.length && <label className="block">付款卡编号<input aria-label="付款卡编号" className="field w-full" inputMode="numeric" value={fromCardId} onChange={e => setFromCardId(e.target.value.replace(/[^0-9]/g, ''))} /></label>}
        <label className="block">转账附言（可选，最多 100 字，不会截断）<input aria-label="转账附言" className="field w-full" maxLength={100} value={paymentRemark} onChange={e => setPaymentRemark(e.target.value)} disabled={busy || working} /></label>
        <p className="text-xs">请先在上方“收款人与账户解析”完成候选选择；准备时服务端会重新校验收款信息指纹，变化返回 recipient_changed。请求键在发送前保存，网络中断/刷新后按原键核实。</p>
        <button type="button" className="primary" disabled={busy || working || gates.length > 0 || !fromCardId || !resolution?.selected_candidate_id} onClick={() => startPrepare()}>生成缴费草稿并准备任务</button>
        {!!gates.length && <ul className="payment-blockers" aria-label="准备付款前的阻断条件">{gates.map(g => <li key={g.code}>{g.text}</li>)}</ul>}
        {prepared && <div className="task-notice" role="status" data-prepared={prepared.taskId || ''}>
          {prepared.lines.map((line, i) => <p key={i}>{line}</p>)}
          {prepared.taskId && <button type="button" className="primary" onClick={() => { if(onPrepared)onPrepared(prepared.taskId);else onOpenTasks() }}>到任务区核对预览/验证/确认</button>}
        </div>}
        {preparation && !prepared && <div className="task-notice" role="status"><p>准备状态：{preparation.state}；草稿 {preparation.draft_reference || '未返回'}；任务 {preparation.task_id || '未返回'}。</p><p>{PREPARE_NOT_PAYMENT}</p></div>}
        {!!doc.obligation && <p className="text-xs">缴费义务：{doc.obligation.obligation_id} · {doc.obligation.state} · rev {doc.obligation.revision}。同一义务换图/换请求键不会生成第二笔。</p>}
        {!!doc.links?.length && <p className="text-xs">{linkNotice} 关联不新增支出、不改余额。</p>}
      </section>
      <section className="payment-reconcile" aria-label="凭证对账">
        <h4>凭证对账（只关联，不记账）</h4>
        <button type="button" className="secondary" disabled={busy || working} onClick={() => void reconcile()}>核对本人流水（近三个月）</button>
        {run && <div data-outcome={run.match_status}>
          <p><strong>核对结果：</strong>{RECONCILE_OUTCOME[run.match_status]?.label || run.match_status}</p>
          <p className="text-xs">已比较字段：{run.compared_fields.join('、') || '无'}；未知字段：{run.unknown_fields.join('、') || '无'}。缺失字段不能当作匹配。</p>
          {run.candidates.map(candidate => <label key={candidate.transaction_id} className="payment-candidate">
            <input type="radio" name="reconcile-match" checked={selectedTransaction === candidate.transaction_id} onChange={() => setSelectedTransaction(candidate.transaction_id)} />
            <span>{candidateRows(candidate).map(row => `${row.label} ${row.value}`).join(' · ')}</span>
          </label>)}
          {reconcileGating(run, selectedTransaction, doc.kind).map((gate, i) => <p key={i} className="text-xs text-amber-800">{gate}</p>)}
          <button type="button" className="primary" disabled={busy || working || selectedTransaction == null} onClick={() => { const c = run.candidates.find(x => x.transaction_id === selectedTransaction); if (c) void link(c) }}>保存凭证关联</button>
        </div>}
        {linked && <p role="status" className="task-notice">已保存关联：单据 {doc.document_id} ↔ 流水 {linked.transaction_id}（关联 {linked.link_id}）。ledger_unchanged={String(linked.ledger_unchanged)}；关联不新增支出、不改余额。</p>}
      </section>
    </div>}
  </section>
}
