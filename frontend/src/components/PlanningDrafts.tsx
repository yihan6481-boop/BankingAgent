import { useEffect, useRef, useState } from 'react'
import { request } from '../api/client'

type Draft = { draft_id: string; revision: number; status: string; summary: string; missing_fields: string[]; expires_at: string }
export function PlanningDrafts({ sid, busy, onResume, refreshKey }: { sid: string; busy: boolean; onResume: () => void; refreshKey: number }) {
  const [drafts, setDrafts] = useState<Draft[]>([])
  const [error, setError] = useState('')
  const [working, setWorking] = useState(false)
  const version = useRef(0)
  const base = `/secure/sessions/${encodeURIComponent(sid)}/planning-drafts`
  const load = async () => {
    const revision = ++version.current
    try { const out = await request<{ drafts: Draft[] }>(base, sid); if (revision === version.current) { setDrafts(out.drafts); setError('') } }
    catch (e) { if (revision === version.current) setError(e instanceof Error ? e.message : '草稿列表读取失败') }
  }
  useEffect(() => { setDrafts([]); void load(); const timer = window.setInterval(() => void load(), 15000); return () => { ++version.current; clearInterval(timer) } }, [sid, refreshKey])
  const act = async (d: Draft, action: 'resume' | 'cancel') => {
    if (busy || working) return
    setWorking(true)
    try {
      await request(`${base}/${encodeURIComponent(d.draft_id)}/${action}`, sid, 'POST', { revision: d.revision })
      if (action === 'resume') onResume()
      await load()
    } catch (e) { setError(e instanceof Error ? e.message : '草稿操作失败'); await load() }
    finally { setWorking(false) }
  }
  return <section aria-label="规划草稿" className="border rounded-xl p-3 text-xs space-y-2 bg-white">
    <div className="flex justify-between"><strong>规划草稿 · 未建立正式任务</strong><button disabled={busy || working} onClick={() => void load()}>刷新草稿</button></div>
    <p className="text-slate-500">草稿仅保留需求，有效期5分钟。重新登录后请明确选择；恢复不授予转账权限。</p>
    {error && <p role="alert">{error}</p>}
    {drafts.length === 0 && <p>暂无有效草稿</p>}
    {drafts.map(d => <div key={d.draft_id} className="border-t pt-2 space-y-1">
      <p>{d.summary}</p><p>待补充：{d.missing_fields.map(field => field.replace(/goal_(\d+)\./, '目标$1：').replace('from_card_id', '付款卡').replace('recipient_name', '收款人').replace('amount', '金额').replace('depends_on', '步骤顺序').replace('complete_plan', '完整目标及条件')).join('、')}</p>
      <p className="text-slate-500">{d.draft_id.slice(-8)} · 修订{d.revision} · {new Date(d.expires_at).toLocaleTimeString()} 到期</p>
      <button className="secondary mr-2" disabled={busy || working} onClick={() => void act(d, 'resume')}>选择并恢复草稿</button>
      <button className="secondary" disabled={busy || working} onClick={() => void act(d, 'cancel')}>取消草稿</button>
    </div>)}
  </section>
}
