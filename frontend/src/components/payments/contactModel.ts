/**
 * Pure helpers for the contact-management UI (A payment-1.0 `/contacts` API).
 * The server owns uniqueness and the trusted-recipient directory: this module
 * only validates obvious input before a write, never marks a user input trusted.
 */
import type { Contact } from '../../api/payments'

const PHONE = /^(?:\+?86)?1[3-9][0-9]{9}$/

/** Normalize a PRC mobile number to its compact form; null when invalid. */
export function normalizePhone(text: string): string | null {
  const compact = text.replace(/[\s\-()]/g, '')
  if (!PHONE.test(compact)) return null
  return compact
}

export function phoneIsBoundElsewhere(_contacts: Contact[], _recipientId: number, _phone: string): boolean {
  // Masked suffixes cannot establish full-phone equality. Server checks it.
  return false
}

export function contactFormErrors(input: { display_name: string; phone: string; requirePhone?: boolean }): string[] {
  const errors: string[] = []
  if (input.display_name.trim().length > 100) errors.push('备注名最多 100 字。')
  if (input.phone.trim() && !normalizePhone(input.phone)) errors.push('手机号仅支持中国大陆 11 位手机号（可带 +86）。')
  if (input.requirePhone && !input.phone.trim()) errors.push('该操作需要手机号。')
  return errors
}

export function contactLabel(contact: Contact): string {
  const directory = contact.directory
  const parts = [contact.display_name || directory?.name || `收款人 ${contact.recipient_id}`]
  if (directory?.bank_name || directory?.masked_account) parts.push(`${directory.bank_name || '银行未知'} · ${directory.masked_account}`)
  if (contact.phone_masked) parts.push(contact.phone_masked)
  if (contact.note) parts.push(contact.note)
  if (contact.phone_bound) parts.push('已绑定手机')
  if (contact.contact_id === null || contact.status === 'directory_only') parts.push('目录项（未建档案）')
  else if (contact.status === 'archived') parts.push('已归档')
  return parts.join(' · ')
}

export function directoryOnlyContacts(contacts: Contact[]): Contact[] {
  return contacts.filter(contact => !contact.contact_id || contact.status === 'directory_only')
}

export function managedContacts(contacts: Contact[]): Contact[] {
  return contacts.filter(contact => !!contact.contact_id && contact.status !== 'directory_only')
}

/** Contact writes are safe to replay with the same key: server returns the same contact. */
export function contactWriteReplayHint(code: string | null | undefined): string {
  switch (code) {
    case 'network_error': return '连接中断，写入结果未知。先刷新联系人列表核实；重试将复用同一请求键，不会重复建档案。'
    case 'stale_revision': return '联系档案版本已变化。请重新读取列表后再修改。'
    case 'phone_already_bound': return '该手机号已绑定到本人其他有效联系人；同一用户必须唯一，请先解绑或使用其他号码。'
    case 'phone_invalid': return '手机号格式不受支持；仅支持中国大陆 11 位手机号。'
    case 'contact_exists': return '该收款人已有联系档案；请直接编辑，不要重复创建。'
    case 'contact_not_found': return '联系档案不存在或不属于当前用户；请刷新列表。'
    case 'invalid_credentials': return '密码复核失败。'
    default: return code ? `操作未完成（${code}）。请刷新联系人列表核实后再试。` : ''
  }
}
