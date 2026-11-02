import type { ReactNode } from 'react'
import type { Json } from '../integrationTypes'
import { formatYuan, formatPct } from '../utils/format'

interface Totals { income: number; expense: number; net_cash_flow: number; transaction_count: number }
interface Range { start: string; end_exclusive: string; card_id: number | null }
interface Entry { transaction_id: number; date: string; merchant: string; category: string; card_last_four: string; direction: 'income' | 'expense'; amount: number }
interface Report {
  kind: 'monthly_cash_flow_report' | 'annual_cash_flow_report'
  explanation: string
  range: Range; totals: Totals
  categories: { name: string; direction: string; amount: number; percentage: number | null }[]
  merchants: { name: string; expense: number; transaction_count: number }[]
  months: { month: string; totals: Totals }[]
  coverage: { evidence: Entry[]; evidence_truncated: boolean; note: string; first_recorded: string | null; last_recorded: string | null }
  comparison: { range: Range | null; totals: Totals; changes: { metric: string; change: number; percentage: number | null }[] }
}
function object(value: Json | undefined): value is { [key: string]: Json } {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}
export function cashFlowReport(data: Json | undefined): Report | null {
  // These reports are validated by the backend analysis contract. Check the
  // essential shape before dispatching; other JSON keeps the generic renderer.
  if (!object(data) || !['monthly_cash_flow_report', 'annual_cash_flow_report'].includes(String(data.kind)) ||
      !object(data.totals) || !object(data.range) || !object(data.coverage) || !object(data.comparison) ||
      !object(data.comparison.totals) || !Array.isArray(data.comparison.changes) ||
      !Array.isArray(data.categories) || !Array.isArray(data.merchants) || !Array.isArray(data.months) || !Array.isArray(data.coverage.evidence) ||
      typeof data.explanation !== 'string' || typeof data.range.start !== 'string' || typeof data.range.end_exclusive !== 'string') return null
  const totals = data.totals
  if (!['income', 'expense', 'net_cash_flow', 'transaction_count'].every(k => typeof totals[k] === 'number' && Number.isFinite(totals[k]))) return null
  return data as unknown as Report
}
function period(range: Range, annual: boolean) {
  return annual ? `${range.start.slice(0, 4)}年` : `${range.start.slice(0, 4)}年${Number(range.start.slice(5, 7))}月`
}
function Disclosure({ title, children }: { title: string; children: ReactNode }) {
  return <details className="border-t border-slate-100 py-3">
    <summary className="cursor-pointer text-sm text-slate-600 font-medium">{title}</summary>
    <div className="mt-3 text-xs text-slate-600 space-y-2">{children}</div>
  </details>
}
export function CashFlowReport({ report: r }: { report: Report }) {
  const annual = r.kind === 'annual_cash_flow_report'
  const expenses = r.categories.filter(c => c.direction === 'expense').slice().sort((a, b) => b.amount - a.amount)
  const top = expenses[0]
  const change = r.comparison.changes.find(c => c.metric === 'expense')
  const conclusion = r.totals.transaction_count === 0 ? '这个期间还没有记录到交易。' :
    r.totals.net_cash_flow > 0 ? '本期已记录的收入高于支出。' : r.totals.net_cash_flow < 0 ? '本期已记录的支出高于收入。' : '本期已记录的收支持平。'
  return <section aria-label="收支概览" className="w-full rounded-2xl bg-white shadow-card px-4 pt-4 pb-1 border border-slate-100">
    <h4 className="text-sm font-semibold text-slate-800">{period(r.range, annual)}收支</h4>
    <p className="text-sm text-slate-700 mt-2">{conclusion}</p>
    <dl className="grid grid-cols-3 gap-3 my-4 tabular-nums">
      {([['收入', r.totals.income], ['支出', r.totals.expense], ['结余', r.totals.net_cash_flow]] as const).map(([label, value]) =>
        <div key={label}><dt className="text-xs text-slate-500 mb-1">{label}</dt><dd className="text-sm sm:text-base font-semibold text-slate-800 break-words">{formatYuan(value)}</dd></div>)}
    </dl>
    {top && top.amount > 0 && <p className="text-sm text-slate-600 mb-3">支出最多的是{top.name}，共 {formatYuan(top.amount)}{top.percentage === null ? '。' : `，占 ${formatPct(top.percentage)}。`}</p>}
    <p className="text-[11px] text-slate-400 mb-3">仅统计已记录流水；结余是收入减支出，不是账户余额。</p>
    <Disclosure title="查看分类和商户">
      {r.categories.length === 0 ? <p>暂无分类记录。</p> : r.categories.map((c, i) => <div key={i} className="flex justify-between gap-3"><span>{c.name} · {c.direction === 'income' ? '收入' : '支出'}</span><span>{formatYuan(c.amount)}{c.percentage === null ? '' : ` · ${formatPct(c.percentage)}`}</span></div>)}
      {r.merchants.length > 0 && <div className="pt-2 border-t border-slate-100"><p className="font-medium mb-2">支出商户（最多 20 项）</p>{r.merchants.map((m, i) => <div key={i} className="flex justify-between gap-3 py-1"><span>{m.name} · {m.transaction_count} 笔</span><span>{formatYuan(m.expense)}</span></div>)}</div>}
    </Disclosure>
    <Disclosure title={annual ? '查看各月走势和上年对比' : '查看上月对比'}>
      {r.months.map(m => <div key={m.month} className="space-y-1 pb-2"><p className="font-medium">{m.month}</p><p>收入 {formatYuan(m.totals.income)} · 支出 {formatYuan(m.totals.expense)} · 结余 {formatYuan(m.totals.net_cash_flow)}</p></div>)}
      {r.comparison.range ? <>
        <p className="font-medium">对比 {period(r.comparison.range, annual)}</p>
        <p>上期收入 {formatYuan(r.comparison.totals.income)} · 支出 {formatYuan(r.comparison.totals.expense)} · 结余 {formatYuan(r.comparison.totals.net_cash_flow)}</p>
        {change && <p>{change.change === 0 ? '已记录支出与上期相同。' : `已记录支出比上期${change.change > 0 ? '增加' : '减少'} ${formatYuan(Math.abs(change.change))}。`}{change.percentage === null ? '上期支出为零，不计算变化比例。' : `变化幅度 ${formatPct(Math.abs(change.percentage))}。`}</p>}
        <p className="text-slate-400">两期均只覆盖已记录的流水。</p>
      </> : <p>暂无可用的上期范围。</p>}
    </Disclosure>
    <Disclosure title={`查看逐笔账单（${r.totals.transaction_count} 笔）`}>
      {r.coverage.evidence.length === 0 ? <p>暂无流水。</p> : r.coverage.evidence.map(t => <div key={t.transaction_id} className="flex items-center justify-between gap-3 border-b border-slate-50 py-2"><div><p className="text-slate-700">{t.merchant}</p><p className="text-[11px] text-slate-400">{t.date} · {t.category} · 尾号{t.card_last_four}</p></div><span className="shrink-0 tabular-nums">{t.direction === 'income' ? '+' : '-'}{formatYuan(t.amount)}</span></div>)}
      {r.coverage.evidence_truncated && <p>明细仅展示部分记录；上方汇总包含全部已记录交易。</p>}
    </Disclosure>
    <Disclosure title="查看统计范围和说明">
      <p>{r.range.start} 至 {r.range.end_exclusive}（不含截止日），按上海时区统计。</p>
      <p>{r.range.card_id === null ? '全部银行卡' : `银行卡 ${r.range.card_id}`} · 共 {r.totals.transaction_count} 笔记录</p>
      <p>数据来源：本地模拟交易流水。</p>
      <p>{r.coverage.note}</p>
    </Disclosure>
  </section>
}
