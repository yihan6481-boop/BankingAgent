import { useEffect, useState } from 'react'
import { payments } from '../../api/payments'
import type { Contact, RecipientResolution, RecipientSelector } from '../../api/payments'

const STATUS_TEXT: Record<RecipientResolution['status'], string> = {
  resolved: '已解析（仍需明确选择候选）',
  ambiguous: '存在多个候选，必须选择',
  not_found: '未找到匹配收款人',
  unsupported: '模拟系统不支持该收款方式',
  needs_information: '需要补充信息或该收款人不可用',
}

/**
 * Unified recipient resolution entry: contact / phone / manual account.
 *
 * Phone numbers are only a trusted-mapping lookup; the UI never derives an
 * account from a phone number and never marks a user-typed account "trusted".
 * Candidate selection is a separate server call (`/recipient-resolutions/{id}/select`)
 * because `prepare-payment` accepts only the resolution id.
 */
export function RecipientResolutionPanel({ sid, busy, resolution, onResolution }: {
  sid: string
  busy: boolean
  resolution: RecipientResolution | null
  onResolution: (value: RecipientResolution | null) => void
}) {
  const [kind, setKind] = useState<RecipientSelector['kind']>('contact')
  const [contactId, setContactId] = useState('')
  const [phone, setPhone] = useState('')
  const [accountNumber, setAccountNumber] = useState('')
  const [accountName, setAccountName] = useState('')
  const [bankName, setBankName] = useState('')
  const [contacts, setContacts] = useState<Contact[] | null>(null)
  const [contactsError, setContactsError] = useState('')
  const [note, setNote] = useState('')
  const [working, setWorking] = useState(false)
  const [error, setError] = useState('')
  const [candidateId, setCandidateId] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    void payments.contacts(sid).then(value => {
      if (live) setContacts(value.contacts || [])
    }).catch(() => {
      if (live) { setContacts([]); setContactsError('联系人目录暂不可用（可手工输入收款人编号，或稍后重试）。') }
    })
    return () => { live = false }
  }, [sid])

  const selector = (): RecipientSelector | null => {
    if (kind === 'contact') {
      const id = Number(contactId)
      return Number.isSafeInteger(id) && id > 0 ? { kind: 'contact', recipient_id: id } : null
    }
    if (kind === 'phone') return phone.trim() ? { kind: 'phone', phone: phone.trim() } : null
    if (kind === 'bank_account') {
      if (!accountNumber.trim() || !accountName.trim()) return null
      return { kind: 'bank_account', account_number: accountNumber.trim(), account_name: accountName.trim(), bank_name: bankName.trim() || null }
    }
    return null
  }
  const valid = !!selector()
  const run = async (fn: () => Promise<void>) => {
    if (working || busy) return
    setWorking(true); setError('')
    try { await fn() } catch (e) { setError(e instanceof Error ? e.message : '收款人操作未完成，请回读状态后再试。') }
    finally { setWorking(false) }
  }
  const submit = () => run(async () => {
    const value = selector()
    if (!value) return
    setCandidateId(null)
    const result = await payments.resolve(sid, value)
    onResolution(result)
    setNote(result.candidates.length > 1 ? '存在多个候选：请逐项核对后明确选择，系统不会默认选择第一项。' : '解析完成：仍需明确选择候选，之后才能准备付款。')
  })
  const select = () => run(async () => {
    if (!resolution || !candidateId) return
    const result = await payments.selectCandidate(sid, resolution, candidateId)
    onResolution(result)
    setNote('已选择该收款候选；准备付款时服务端会重新校验收款信息指纹。')
  })
  const selected = resolution?.selected_candidate_id || null
  return <section className="payment-panel" aria-label="收款人解析">
    <h3>收款人与账户解析</h3>
    <p className="muted-copy">联系人、手机号和手工账户共用同一解析入口。手工输入的账户不会被标为“可信”；手机号只用于已绑定联系人的可信映射（未知号码返回未找到），不能据此推算账户。候选的“模拟目录”证据不等于现实银行账户归属认证。</p>
    <div className="payment-kind-switch" role="radiogroup" aria-label="收款人类型">
      {([['contact', '联系人'], ['phone', '手机号'], ['bank_account', '银行卡号']] as const).map(([value, label]) =>
        <label key={value} className={kind === value ? 'current' : ''}><input type="radio" name="recipient-kind" checked={kind === value} onChange={() => { setKind(value); onResolution(null); setCandidateId(null); setNote('') }} />{label}</label>)}
    </div>
    {kind === 'contact' && <div className="space-y-2">
      {contacts === null && <p className="text-xs text-slate-500">正在读取本人联系人目录…</p>}
      {!!contacts?.length && <label className="block">从本人联系人选择<select aria-label="本人联系人" className="field w-full" value={contactId} onChange={e => setContactId(e.target.value)}>
        <option value="">请选择联系人</option>
        {contacts.map(contact => <option key={`${contact.recipient_id}-${contact.contact_id || 'directory'}`} value={contact.recipient_id}>{contact.display_name}{contact.directory ? ` · ${contact.directory.bank_name || '银行未知'} · ${contact.directory.masked_account}` : ''}{contact.phone_masked ? ` · ${contact.phone_masked}` : ''}{contact.phone_bound ? ' · 已绑定手机' : ''}</option>)}
      </select></label>}
      {contactsError && <p className="text-xs text-amber-800">{contactsError}</p>}
      <label className="block">联系人编号（来自服务器目录）<input aria-label="联系人编号" className="field w-full" inputMode="numeric" value={contactId} onChange={e => setContactId(e.target.value.replace(/[^0-9]/g, ''))} disabled={busy || working} /></label>
    </div>}
    {kind === 'phone' && <div className="space-y-2">
      <label className="block">手机号（仅已绑定联系人可解析）<input aria-label="手机号" className="field w-full" inputMode="tel" value={phone} onChange={e => setPhone(e.target.value)} disabled={busy || working} /></label>
      <p className="text-xs text-slate-500">未绑定手机号会返回“未找到”；手机号不会用于推算账户，也不能创建可信收款人。</p>
    </div>}
    {kind === 'bank_account' && <div className="space-y-2">
      <label className="block">账户名称<input aria-label="账户名称" className="field w-full" value={accountName} onChange={e => setAccountName(e.target.value)} disabled={busy || working} /></label>
      <label className="block">完整账号（仅本核对请求使用，不进入聊天/模型）<input aria-label="完整账号" className="field w-full" inputMode="numeric" autoComplete="off" value={accountNumber} onChange={e => setAccountNumber(e.target.value.replace(/\s+/g, ''))} disabled={busy || working} /></label>
      <label className="block">开户行（可选）<input aria-label="开户行" className="field w-full" value={bankName} onChange={e => setBankName(e.target.value)} disabled={busy || working} /></label>
    </div>}
    <button type="button" className="primary" disabled={!valid || busy || working} onClick={() => void submit()}>{working ? '正在解析…' : '解析收款人'}</button>
    {error && <p role="alert" className="mobile-warning">{error} 结果未知时请重新读取收款解析状态，不要重复提交。</p>}
    {note && <p className="task-notice" role="status">{note}</p>}
    {resolution && <div className="payment-resolution" data-status={resolution.status}>
      <p><strong>解析状态：</strong>{STATUS_TEXT[resolution.status] || resolution.status}</p>
      <p className="muted-copy">引用 {resolution.resolution_id} · rev {resolution.revision} · 有效至 {new Date(resolution.expires_at).toLocaleString('zh-CN')}</p>
      {!!resolution.limitations.length && <p className="text-xs text-amber-800">{resolution.limitations.join('；')}</p>}
      {!!resolution.candidates.length && <div role="radiogroup" aria-label="收款候选">
        {resolution.candidates.map(c => <label key={c.candidate_id} className={`payment-candidate ${c.candidate_id === candidateId ? 'selected' : ''} ${c.candidate_id === selected ? 'chosen' : ''}`}>
          <input type="radio" name="recipient-candidate" checked={c.candidate_id === candidateId} onChange={() => setCandidateId(c.candidate_id)} disabled={busy || working} />
          <span><strong>{c.account_name}</strong> · {c.bank_name || '开户行未知'} · {c.masked_account} · {c.beneficiary_kind === 'person' ? '个人' : '机构'} · {c.trusted ? '目录标记可信' : '目录未标记可信'}</span>
          <small>支持范围：{c.supports.join('、') || '未说明'}；{c.reasons.length ? `限制：${c.reasons.join('、')}；` : ''}证据：{c.evidence.map(e => `${e.source}${e.reference ? ' ' + e.reference : ''}${e.note ? '（' + e.note + '）' : ''}`).join('；') || '无'}</small>
        </label>)}
      </div>}
      {resolution.candidates.length > 0 && !selected && <button type="button" className="primary" disabled={!candidateId || busy || working} onClick={() => void select()}>{working ? '正在确认选择…' : '确认选择该收款人'}</button>}
      {selected && <p role="status" className="text-xs text-emerald-700">已选择候选 {selected}。准备付款时仍会重新校验收款信息；旧解析可能过期失效。</p>}
      {resolution.status === 'resolved' && !selected && !candidateId && <p role="status" className="task-notice">请明确选择上面的候选；系统不会默认选择第一项。</p>}
      {resolution.status === 'not_found' && <p role="status" className="task-notice">未找到匹配收款人：不能继续准备付款，也不会生成扣款。</p>}
      {resolution.status === 'unsupported' && <p role="alert" className="mobile-warning">模拟系统不支持该收款方式，不会生成成功回执，也不会计入已付款。</p>}
      {resolution.status === 'needs_information' && <p role="status" className="task-notice">该收款人当前不可用或需要补充信息；系统不会猜测账户归属。</p>}
    </div>}
  </section>
}
