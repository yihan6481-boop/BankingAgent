import type { Json } from '../integrationTypes'

type Value = { label: string; value: string }
type Detail = { day: string; merchant: string; amount: string; kind: string; direction: string; card_tail: string | null; source: string }
type Ranked = { rank: number; name: string; value: string; values?: Value[] }
type Comparison = { label: string; current_label: string; base_label: string; current: string; base: string; delta: string; signed_delta: string; direction: '多' | '少' | '相同' | '未知'; percent: string | null }
type Card = { kind: 'summary' | 'details' | 'ranking' | 'comparison'; title: string; source: string; values: Value[]; rows: (Detail | Ranked)[]; comparisons: Comparison[]; sort: string | null; empty: boolean; next_page: boolean; page: string | null; warnings: string[] }
export type QueryResultData = { kind: 'query_result_v1'; turn_id: string; cards: Card[]; coverage: string; warnings: string[]; accounting: string[] }
const obj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)
const str = (v: unknown): v is string => typeof v === 'string'
const strs = (v: unknown): v is string[] => Array.isArray(v) && v.every(str)
const nullable = (v: unknown) => v === null || str(v)
export function queryResult(data: Json): QueryResultData | null {
  if (!obj(data) || data.kind !== 'query_result_v1' || !str(data.turn_id) || !/^[a-f0-9]{32}$/.test(data.turn_id) ||
    !str(data.coverage) || !strs(data.warnings) || !strs(data.accounting) || !Array.isArray(data.cards) || data.cards.length > 4) return null
  for (const c of data.cards) {
    if (!obj(c) || !['summary', 'details', 'ranking', 'comparison'].includes(String(c.kind)) || !str(c.title) || !str(c.source) ||
      !strs(c.warnings) || !nullable(c.sort) || !nullable(c.page) || typeof c.empty !== 'boolean' || typeof c.next_page !== 'boolean' ||
      !Array.isArray(c.values) || !c.values.every(v => obj(v) && str(v.label) && str(v.value)) ||
      !Array.isArray(c.rows) || c.rows.length > 100 || !Array.isArray(c.comparisons)) return null
    for (const r of c.rows) {
      if (!obj(r)) return null
      if (c.kind === 'details') {
        if (!['day', 'merchant', 'amount', 'kind', 'direction', 'source'].every(k => str(r[k])) ||
          !(r.card_tail === null || (str(r.card_tail) && /^\d{4}$/.test(r.card_tail)))) return null
      } else if (c.kind === 'ranking') {
        if (!Number.isSafeInteger(r.rank) || Number(r.rank) < 1 || !str(r.name) || !str(r.value)) return null
        if (r.values !== undefined && (!Array.isArray(r.values) || !r.values.every(v => obj(v) && str(v.label) && str(v.value)))) return null
      } else return null
    }
    for (const r of c.comparisons) {
      if (!obj(r) || !['label', 'current_label', 'base_label', 'current', 'base', 'delta', 'signed_delta'].every(k => str(r[k])) ||
        !['多', '少', '相同', '未知'].includes(String(r.direction)) || !nullable(r.percent)) return null
    }
  }
  return data as unknown as QueryResultData
}

export function QueryResult({ data, onNext, historical = false }: { data: QueryResultData; onNext?: () => void; historical?: boolean }) {
  return <section aria-label="金融查询结果" data-query-turn={data.turn_id} className="w-full min-w-0 space-y-2 break-words">
    <p className="text-[11px] text-slate-500">{data.coverage}</p>
    {data.warnings.map((w, i) => <p key={i} className="text-xs text-amber-800 bg-amber-50 p-2 rounded-lg">{w}</p>)}
    {data.cards.map((c, i) => <article key={i} data-query-kind={c.kind} className="w-full min-w-0 rounded-2xl bg-white border border-slate-100 p-3 space-y-2 shadow-card">
      <h4 className="text-sm font-semibold text-slate-800">{c.title}</h4>
      <p className="text-[11px] text-slate-500">{c.source}</p>
      {c.empty && <p className="text-sm text-slate-600">未查到已记录的匹配项；不能据此判断没有实际消费。</p>}
      {c.kind !== 'comparison' && c.values.length > 0 && <dl className="space-y-1">{c.values.map((v, n) => <div key={n} className="flex justify-between gap-3 text-sm"><dt className="text-slate-500">{v.label}</dt><dd className="font-semibold text-webank-700 text-right">{v.value}</dd></div>)}</dl>}
      {c.kind === 'details' && <details><summary className="cursor-pointer text-sm text-webank-700">查看逐笔明细（{c.rows.length}笔）</summary>
        <ul className="divide-y divide-slate-100">{(c.rows as Detail[]).map((r, n) => <li key={n} className="py-2 text-xs space-y-1">
          <div className="flex justify-between gap-3"><span className="min-w-0 break-all">{r.merchant}</span><strong className="shrink-0">{r.amount}</strong></div>
          <div className="text-slate-500">{r.day} · {r.kind} · {r.direction}{r.card_tail ? ` · 尾号${r.card_tail}` : ''} · {r.source}</div>
        </li>)}</ul><p className="text-[11px] text-slate-500">{c.page}</p>
      </details>}
      {c.kind === 'ranking' && <><p className="text-xs text-slate-500">{c.sort} · 当前展示{c.rows.length}项</p>
        <ol className="space-y-2">{(c.rows as Ranked[]).map((r, n) => <li key={n} className="text-sm space-y-1"><div className="flex gap-2"><span className="text-slate-400">{r.rank}</span><span className="flex-1 min-w-0 break-all">{r.name}</span><strong className="shrink-0">{r.value}</strong></div>{r.values && r.values.length > 1 && <dl className="text-xs text-slate-500 pl-4 space-y-1">{r.values.map((v, k) => <div key={k} className="flex justify-between gap-2"><dt>{v.label}</dt><dd>{v.value}</dd></div>)}</dl>}</li>)}</ol></>}
      {c.kind === 'comparison' && c.comparisons.map((r, n) => <div key={n} className="text-sm space-y-1 border-t pt-2">
        <p className="font-medium">{r.label}</p><p>{r.current_label}：<strong>{r.current}</strong></p><p>{r.base_label}：<strong>{r.base}</strong></p>
        <p className="text-webank-700">{r.direction === '未知' ? '差额无法确认' : r.direction === '相同' ? '两者相同' : `前者比后者${r.direction}${r.delta}`}{r.percent !== null ? ` · 变化率${r.percent}` : ' · 无可用变化率'}</p>
      </div>)}
      {c.warnings.map((w, n) => <p key={n} className="text-[11px] text-amber-800">{w}</p>)}
      {c.next_page && (onNext && !historical ? <button className="text-sm text-webank-700 underline" onClick={onNext}>查看更多</button> : <p className="text-xs text-slate-500">这是历史分页快照；请重新查询后继续翻页。</p>)}
    </article>)}
    <details className="text-xs text-slate-500 bg-white rounded-xl border p-3"><summary className="cursor-pointer">数据来源与核算口径</summary>
      {data.accounting.map((a, i) => <p className="mt-2" key={i}>{a}</p>)}
    </details>
  </section>
}
