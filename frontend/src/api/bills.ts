import { request, ApiError } from './client'
export const BILL_CONFIRM_TEXT = '确认导入已核对账单' as const
export interface BillValue {
  date: string | null; time: string | null; precision: string; merchant: string | null; amount_cents: number | null
  currency: string | null; direction: string; kind: string; status: string; payment_method: string | null
  order_number: string | null; event_number: string | null; evidence: string; issues: string[]; confidence: number | null
}
export interface BillRow { row_id: string; value: BillValue; included: boolean; reviewed: boolean; duplicate_resolution: string }
export interface BillDocument {
  source_current?:boolean
  document_id: string; revision: number; digest: string; state: string; platform: string; rows: BillRow[]
  repeated_upload: boolean; issues: Record<string, string[]>; duplicates: Record<string, {type: string}[]>
  original: unknown; error_code: string | null; confirmation: {imported_count: number; row_ids: string[]; revision: number; digest: string} | null
}
export interface BillSummary { document_id: string; state: string; revision: number; created_at: string }
export interface BillResult {
  coverage: string; records: {document_id: string; row_id: string; revision: number}[]
  totals: {purchase_expense_cents: number; other_expense_cents: number; refund_cents: number; income_cents: number; excluded_count: number}
}
const path = (sid: string) => `/secure/sessions/${encodeURIComponent(sid)}/bills`
async function call<T>(sid: string, suffix = '', method = 'GET', body?: unknown) {
  return (await request<{data: T}>(path(sid) + suffix, sid, method, body)).data
}
export const bills = {
  list: (sid: string) => call<{documents: BillSummary[]; has_more: boolean}>(sid),
  get: (sid: string, id: string) => call<BillDocument>(sid, '/' + id),
  select: (sid: string, id: string) => call<BillDocument>(sid, '/' + id + '/select', 'POST'),
  upload: (sid: string, file: File) => call<BillDocument>(sid, '', 'POST', file),
  recognize: (sid: string, d: BillDocument) => call<BillDocument>(sid, '/' + d.document_id + '/recognize', 'POST', {revision: d.revision}),
  edit: (sid: string, d: BillDocument, rows: BillRow[], platform: string) => call<BillDocument>(sid, '/' + d.document_id + '/rows', 'PUT', {revision: d.revision, rows, platform}),
  confirm: (sid: string, d: BillDocument) => call<BillDocument>(sid, '/' + d.document_id + '/confirm', 'POST', {
    revision: d.revision, digest: d.digest, row_ids: d.rows.filter(r => r.included && r.duplicate_resolution !== 'exclude').map(r => r.row_id), text: BILL_CONFIRM_TEXT}),
  query: (sid: string, id?: string, merchant?: string) => call<BillResult>(sid, '/query', 'POST', {document_id: id || null, merchant: merchant || null}),
  delete: (sid: string, d: BillDocument) => call(sid, '/' + d.document_id + '?revision=' + d.revision, 'DELETE'),
  async image(sid: string, id: string) {
    const base = (import.meta.env.VITE_API_BASE || '/api').replace(/\/$/, '')
    const r = await fetch(base + path(sid) + '/' + id + '/image', {cache: 'no-store', headers: {Authorization: `Bearer ${sid}`}})
    if (!r.ok) throw new ApiError('bill_image_failed', '原图读取失败，请重新登录或重新打开批次。', r.status)
    return r.blob()
  },
}
export function yuan(cents: number) { return `${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, '0')}` }
export function cents(text: string): number {
  if (!/^(?:0|[1-9]\d{0,10})(?:\.\d{1,2})?$/.test(text)) throw Error('金额请输入大于零、最多两位小数的元数，不支持指数或千位分隔符。')
  const [whole, fraction = ''] = text.split('.')
  const n = BigInt(whole) * 100n + BigInt(fraction.padEnd(2, '0'))
  if (n < 1n || n > 1000000000000n) throw Error('金额须大于零且不超过100亿元。')
  return Number(n)
}
export function stable(v: unknown): string {
  if (Array.isArray(v)) return '[' + v.map(stable).join(',') + ']'
  if (v && typeof v === 'object') return '{' + Object.entries(v).sort(([a], [b]) => a.localeCompare(b)).map(([k, x]) => JSON.stringify(k) + ':' + stable(x)).join(',') + '}'
  return JSON.stringify(v)
}
