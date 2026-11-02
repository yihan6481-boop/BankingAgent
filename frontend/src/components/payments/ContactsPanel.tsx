import { useEffect, useRef, useState } from 'react'
import { ApiError } from '../../api/client'
import { payments } from '../../api/payments'
import type { Contact, ContactCreateInput, ContactUpdateInput } from '../../api/payments'
import {
  contactFormErrors, contactLabel, contactWriteReplayHint, directoryOnlyContacts, managedContacts, normalizePhone,
} from './contactModel'

type ErrorState = { code: string; message: string } | null
type PendingWrite = { key: string; kind: 'create' | 'update'; payload: string; contactId: string | null }

/**
 * Contact management for the existing A payment-1.0 `/contacts` API:
 * create a profile for a directory recipient, edit display name / bound phone,
 * archive (which also clears the phone) and reactivate.
 *
 * Write identity: every create/update mints `client_request_id` BEFORE the call and
 * keeps the exact payload for same-key replay after a network/unknown failure, so a
 * retry cannot create a second contact profile. No password/TOTP/token is stored.
 */
export function ContactsPanel({ sid, busy }: { sid: string; busy: boolean }) {
  const [contacts, setContacts] = useState<Contact[] | null>(null)
  const [truncated, setTruncated] = useState(false)
  const [error, setError] = useState<ErrorState>(null)
  const [notice, setNotice] = useState('')
  const [working, setWorking] = useState(false)
  const [recipientId, setRecipientId] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [phone, setPhone] = useState('')
  const [clearPhone,setClearPhone]=useState(false)
  const [editing, setEditing] = useState<Contact | null>(null)
  const [archiveConfirm, setArchiveConfirm] = useState(false)
  const [pendingWrite, setPendingWrite] = useState<PendingWrite | null>(null)
  const alive = useRef(true)
  const storage = () => (typeof window !== 'undefined' && window.sessionStorage ? window.sessionStorage : null)
  useEffect(() => {
    alive.current = true
    void load()
    return () => { alive.current = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid])
  async function load() {
    setWorking(true); setError(null)
    try {
      const value = await payments.contacts(sid)
      if (!alive.current) return
      setContacts(value.contacts || []); setTruncated(!!value.truncated)
    } catch (e) {
      if (alive.current) setError({ code: e instanceof ApiError ? e.code : '', message: contactWriteReplayHint(e instanceof ApiError ? e.code : '') || '联系人列表读取失败。' })
    } finally { if (alive.current) setWorking(false) }
  }
  function mintKey(prefix: string): string {
    const key = `ui-${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
    try { storage()?.setItem('banking-contact-write-last', JSON.stringify({ key, at: new Date().toISOString() })) } catch { /* ignore */ }
    return key
  }
  function resetForm() {
    setRecipientId(''); setDisplayName(''); setPhone(''); setEditing(null); setArchiveConfirm(false);setClearPhone(false)
  }
  function chooseDirectory(id: string) {
    setRecipientId(id)
    const contact = (contacts || []).find(item => String(item.recipient_id) === id)
    setDisplayName(contact?.display_name || contact?.directory?.name || '')
  }
  function chooseEdit(contact: Contact) {
    setEditing(contact)
    setDisplayName(contact.display_name)
    setPhone('')
    setClearPhone(false)
    setArchiveConfirm(false)
    setNotice('手机号留空会保留原号码；需要移除时请勾选清空。')
  }
  async function send(kind: 'create' | 'update', key: string, payload: ContactCreateInput | ContactUpdateInput, contactId: string | null = null) {
    setWorking(true); setError(null); setNotice(kind === 'create' ? '正在创建联系档案…' : '正在保存联系档案…')
    try {
      const result = kind === 'create'
        ? await payments.createContact(sid, payload as ContactCreateInput, key)
        : await payments.updateContact(sid, contactId!, payload as ContactUpdateInput, key)
      if (!alive.current) return
      setPendingWrite(null)
      setNotice(result.replayed ? '本次为同键回读：服务端返回原联系档案，未重复写入。' : '联系档案已保存（服务端回读结果）。')
      resetForm()
      await load()
    } catch (e) {
      if (!alive.current) return
      const code = e instanceof ApiError ? e.code : null
      const safeCodes = ['validation_failed', 'invalid_credentials', 'phone_invalid', 'phone_already_bound', 'contact_exists', 'stale_revision', 'contact_not_found']
      if (code && safeCodes.includes(code)) {
        // Validation/conflict before a durable write: drop the identity after surfacing.
        setPendingWrite(null)
        setError({ code, message: contactWriteReplayHint(code) || (e instanceof Error ? e.message : '联系人写入未完成。') })
      } else {
        // Network/5xx/unknown: keep the exact key+payload; retry replays it.
        setError({ code: code || 'network_error', message: `${contactWriteReplayHint(code || 'network_error')} 原请求键与参数已保留，可同键重试核实，不会重复建档案。` })
      }
    } finally { if (alive.current) setWorking(false) }
  }
  async function submitCreate(replay = false) {
    if (busy || working) return
    const id = Number(recipientId)
    if (!Number.isSafeInteger(id) || id <= 0) { setError({ code: 'recipient_required', message: '请先从本人目录选择收款人。' }); return }
    const errors = contactFormErrors({ display_name: displayName, phone })
    if (errors.length) { setError({ code: 'validation_failed', message: errors.join(' ') }); return }
    const payload: ContactCreateInput = {
      recipient_id: id,
      display_name: displayName.trim() || null,
      phone: phone.trim() ? normalizePhone(phone) : null,
    }
    const serialized = JSON.stringify(payload)
    let current = pendingWrite
    if (replay && current && current.kind === 'create' && current.payload === serialized) {
      // exact same payload -> reuse the original key
    } else {
      if (current && current.kind === 'create' && current.payload !== serialized) {
        setError({ code: 'client_request_conflict', message: '上一次联系人写入尚未核实且参数已变化；请先刷新联系人列表核实结果，再重新创建。' })
        return
      }
      current = { key: mintKey('contact'), kind: 'create', payload: serialized, contactId: null }
      setPendingWrite(current)
    }
    await send('create', current.key, payload)
  }
  async function submitUpdate(replay = false) {
    if (!editing || !editing.contact_id || editing.revision == null || busy || working) return
    const errors = contactFormErrors({ display_name: displayName, phone })
    if (errors.length) { setError({ code: 'validation_failed', message: errors.join(' ') }); return }
    const payload: ContactUpdateInput = {
      revision: editing.revision,
      display_name: displayName.trim() || null,
      ...(clearPhone?{phone:null}:phone.trim()?{phone:normalizePhone(phone)}:{}),
      archive: false,
    }
    const serialized = JSON.stringify(payload)
    let current = pendingWrite
    if (replay && current && current.kind === 'update' && current.payload === serialized && current.contactId === editing.contact_id) {
      // reuse
    } else {
      if (current && current.kind === 'update' && current.payload !== serialized) {
        setError({ code: 'client_request_conflict', message: '上一次联系档案写入尚未核实且参数已变化；请先刷新列表核实结果。' })
        return
      }
      current = { key: mintKey('contact-update'), kind: 'update', payload: serialized, contactId: editing.contact_id }
      setPendingWrite(current)
    }
    await send('update', current.key, payload, editing.contact_id)
  }
  async function archive(contact: Contact, restore = false) {
    if (!contact.contact_id || contact.revision == null || busy || working) return
    if (!restore && !archiveConfirm) { setArchiveConfirm(true); return }
    const payload: ContactUpdateInput = { revision: contact.revision, display_name: contact.display_name, phone: null, archive: !restore }
    const current = { key: mintKey('contact-archive'), kind: 'update' as const, payload: JSON.stringify(payload), contactId: contact.contact_id }
    setPendingWrite(current)
    await send('update', current.key, payload, contact.contact_id)
    if (!restore) setNotice('联系档案已归档并清空手机号；可重新启用，但手机号需重新绑定。归档不是删除收款人。')
  }
  const directory = directoryOnlyContacts(contacts || [])
  const managed = managedContacts(contacts || [])
  return <section className="payment-panel" aria-label="联系人管理">
    <div className="payment-head"><h3>联系人管理</h3><button type="button" className="secondary" disabled={busy || working} onClick={() => void load()}>刷新联系人</button></div>
    <p className="muted-copy">选择已有联系人可编辑备注名和手机号；同一人的多个收款账户请分别核对。演示号码仅用于目录查询，不发送真实短信。</p>
    {error && <p role="alert" className="mobile-warning" data-error-code={error.code}>{error.message}</p>}
    {notice && <p role="status" className="task-notice">{notice}</p>}
    {truncated && <p className="text-xs text-amber-800">联系人目录已截断，仅显示部分条目。</p>}
    {contacts === null && <p className="muted-copy">正在读取本人联系人目录…</p>}
    {contacts !== null && <>
      <section aria-label="新建联系档案">
        <h4>新增联系人档案</h4>
        {directory.length === 0
          ? <p className="text-xs text-slate-500">本人目录中没有未建档案的收款人。</p>
          : <div className="space-y-2">
            <label className="block">目录收款人<select aria-label="目录收款人" className="field w-full" value={recipientId} onChange={e => chooseDirectory(e.target.value)} disabled={busy || working}>
              <option value="">请选择收款人</option>
              {directory.map(contact => <option key={contact.recipient_id} value={contact.recipient_id}>{contactLabel(contact)}</option>)}
            </select></label>
            <label className="block">备注名（可选，≤100 字）<input aria-label="联系人备注名" className="field w-full" maxLength={100} value={displayName} onChange={e => setDisplayName(e.target.value)} disabled={busy || working} /></label>
            <label className="block">手机号（可选；用于手机号解析，不用于推算账户）<input aria-label="联系人手机号" className="field w-full" inputMode="tel" value={phone} onChange={e => setPhone(e.target.value)} disabled={busy || working} /></label>
            <button type="button" className="primary" disabled={busy || working || !recipientId} onClick={() => void submitCreate()}>创建联系档案</button>
            {pendingWrite?.kind === 'create' && <button type="button" className="secondary" disabled={busy || working} onClick={() => void submitCreate(true)}>同键重试并核实原写入</button>}
          </div>}
      </section>
      <section aria-label="本人联系档案">
        <h4>本人联系档案</h4>
        {managed.length === 0 && <p className="text-xs text-slate-500">暂无已建档案的联系人。</p>}
        {managed.map(contact => <article key={contact.contact_id || contact.recipient_id} className="payment-item" data-contact-id={contact.contact_id || ''}>
          <p className="text-xs"><strong>{contactLabel(contact)}</strong>{contact.status === 'archived' ? '（已归档）' : ''}</p>
          {editing?.contact_id === contact.contact_id ? <div className="space-y-2">
            <label className="block">备注名<input aria-label="编辑联系人备注名" className="field w-full" maxLength={100} value={displayName} onChange={e => setDisplayName(e.target.value)} disabled={busy || working} /></label>
            <label className="block">手机号（留空保留）<input aria-label="编辑联系人手机号" className="field w-full" inputMode="tel" value={phone} onChange={e => {setPhone(e.target.value);setClearPhone(false)}} disabled={busy || working} /></label>
            <label><input type="checkbox" aria-label="清空联系人手机号" checked={clearPhone} onChange={e=>{setClearPhone(e.target.checked);if(e.target.checked)setPhone('')}} disabled={busy||working}/> 清空手机号</label>
            <div className="flex gap-2 flex-wrap">
              <button type="button" className="primary" disabled={busy || working} onClick={() => void submitUpdate()}>保存联系人修改</button>
              {pendingWrite?.kind === 'update' && pendingWrite.contactId === contact.contact_id && <button type="button" className="secondary" disabled={busy || working} onClick={() => void submitUpdate(true)}>同键重试并核实</button>}
              <button type="button" className="secondary" onClick={resetForm}>返回</button>
            </div>
          </div> : <div className="flex gap-2 flex-wrap">
            <button type="button" className="secondary" disabled={busy || working} onClick={() => chooseEdit(contact)}>编辑</button>
            {contact.status !== 'archived'
              ? <button type="button" className="secondary text-rose-600" disabled={busy || working} onClick={() => void archive(contact)}>{archiveConfirm ? '确认归档并清空手机号' : '归档'}</button>
              : <button type="button" className="secondary" disabled={busy || working} onClick={() => void archive(contact, true)}>重新启用</button>}
          </div>}
        </article>)}
      </section>
    </>}
    <p className="mobile-footnote">联系人写入使用请求键：网络中断/刷新后先刷新列表核实，重试会复用原键；跨会话以服务端联系人列表为准，不依赖本标签页缓存。</p>
  </section>
}
