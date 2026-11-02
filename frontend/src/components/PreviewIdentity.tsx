import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Preview, ReadEnvelope } from '../integrationTypes'

// Only authenticated existing reads; never opt into additional query permission.
export function PreviewIdentity({ sid, taskId, preview }: { sid: string; taskId: string; preview: Preview }) {
  const args = preview.requested_arguments
  const key = JSON.stringify([sid, taskId, preview.preview_id, args])
  const [loaded, setLoaded] = useState<{ key: string; rows: [string, string][] } | null>(null)
  useEffect(() => {
    let live = true
    setLoaded(null)
    const specs = [
      { id: args.from_card_id ?? args.card_id, tool: 'get_cards', kind: 'card_list', field: 'card_id', label: '付款 / 操作账户', format: (v: any) => `${v.bank_name} · ${v.masked_card_number}` },
      { id: args.recipient_id, tool: 'get_transfer_recipients', kind: 'recipient_list', field: 'recipient_id', label: '收款人', format: (v: any) => `${v.name} · ${v.bank_name} · ${v.masked_account}` },
      { id: args.product_id, tool: 'get_wealth_products', kind: 'wealth_product_list', field: 'product_id', label: '产品', format: (v: any) => v.name },
      { id: args.holding_id, tool: 'get_wealth_holdings', kind: 'wealth_holding_list', field: 'holding_id', label: '持仓产品', format: (v: any) => v.product_name },
    ].filter(s => s.id !== undefined && s.id !== null)
    void Promise.all(specs.map(async s => {
      let out: ReadEnvelope | null = null
      try { out = await api.read(sid, s.tool, {}) } catch { /* Preview stays usable. */ }
      const result = out?.result
      const item = result?.kind === s.kind && Array.isArray(result.data)
        ? result.data.find(v => (v as unknown as Record<string, unknown>)[s.field] === s.id) : null
      return [s.label, item ? s.format(item) : `账户信息暂不可用（${s.label}编号 ${s.id}）`] as [string, string]
    })).then(rows => { if (live) setLoaded({ key, rows }) })
    return () => { live = false }
  }, [key])
  return <dl className="secure-parameters text-sm space-y-2" aria-label="操作对象信息">
    {loaded?.key === key ? loaded.rows.map(([label, text]) => <div key={label}><dt className="text-slate-500">{label}</dt><dd className="font-medium break-all">{text}</dd></div>) : <div>正在读取账户信息，编号可在下方核对。</div>}
  </dl>
}
