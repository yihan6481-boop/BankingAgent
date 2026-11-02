import type { Json } from '../integrationTypes'
import { CashFlowReport, cashFlowReport } from './CashFlowReport'
import { QueryResult, queryResult } from './QueryResult'
const LABELS: Record<string, string> = {
  from_card_id:'付款卡编号',recipient_id:'收款人编号',recipient_label:'收款人称呼',recipient_name:'收款人',
  daily_limit:'每日限额（元）',holding_id:'持仓编号',principal:'本金（元）',queried_at:'查询时间',
  summary:'汇总',details:'明细',groups:'分组',results:'查询结果',plan:'查询条件',scope:'统计范围',
  selection:'筛选条件',period:'期间',kinds:'账务类型',metrics:'统计指标',current:'当前期间',
  amount_cents:'金额（分）',purchase_cents:'购买支出（分）',refund_cents:'退款（分）',net_cents:'购买减退款（分）',
  count:'笔数',recorded_count:'记录数',matched_count:'匹配数',masked_card_number:'银行卡',bank_name:'银行',balance:'可用余额（元）',
  status:'状态',card_id:'银行卡编号',currency:'币种',truncated:'尚有未展示的明细',
  kind: '报告类型', month: '月份', year: '年份', totals: '收支汇总', income: '收入（元）', expense: '支出（元）',
  net_cash_flow: '净现金流（元）', transaction_count: '交易笔数', income_count: '收入笔数', expense_count: '支出笔数',
  categories: '分类', merchants: '商户', months: '各月分布', coverage: '数据覆盖', note: '说明', notice: '提示',
  evidence: '相关交易', date: '日期', merchant: '商户', category: '分类', amount: '金额（元）', percentage: '占比',
  direction: '收支方向', card_last_four: '银行卡尾号', first_recorded: '最早记录', last_recorded: '最新记录',
  recorded_months: '有记录的月份', source: '数据来源', completeness: '完整性', name: '名称', message: '说明',
  clues: '核查线索', products: '产品', product_name: '产品名称', product_id: '产品编号', risk_level: '风险等级',
  reasons: '原因', conclusion: '结论', eligible: '是否适合', risk_match: '风险匹配', history_only: '仅历史回顾',
  sources: '历史来源', content: '内容', role: '角色', created_at: '记录时间', task_refs: '关联任务', range: '统计范围',
  start: '起始日期', end_exclusive: '截止日期（不含）', timezone: '时区', total_spending: '总支出（元）',
  data: '结果', matched: '匹配结果', claims: '分析结论', limitations: '局限', reference_annual_rate: '参考年化',
  duration_days: '期限（天）', min_purchase: '起购金额（元）', suitable: '适当性', warnings: '注意事项',
}
const VALUES: Record<string, string> = {
  income: '收入', expense: '支出', recorded_only: '仅覆盖已记录交易', local_simulated_transactions: '本地模拟交易',
  monthly_cash_flow_report: '月度收支报告', annual_cash_flow_report: '年度收支报告',
  expense_clues: '支出核查线索', product_comparison: '理财产品对比', wealth_suitability: '理财适当性',
  user: '用户', assistant: '助理',
}
const HIDDEN = new Set(['ledger_sha256', 'execution_digest', 'arguments_hash', 'owner_id', 'principal_id', 'session_id',
  'authorization_id', 'secret', 'password', 'otpauth_uri', 'preview_id', 'operation_id'])
function Tree({ data, depth = 0 }: { data: Json; depth?: number }) {
  if (data === null) return <span className="text-slate-400">暂无</span>
  if (typeof data === 'boolean') return <span>{data ? '是' : '否'}</span>
  if (typeof data !== 'object') return <span className="whitespace-pre-wrap break-words">{typeof data === 'string' ? VALUES[data] ?? data : data.toLocaleString('zh-CN', { maximumFractionDigits: 8 })}</span>
  if (depth > 6) return <span className="text-slate-400">详细数据层级较多，请查看报告说明。</span>
  if (Array.isArray(data)) return <div className="space-y-2">{data.length ? data.slice(0, 30).map((d, i) => <div key={i} className="border-l-2 border-webank-100 pl-2"><Tree data={d} depth={depth + 1} /></div>) : <span className="text-slate-400">暂无记录</span>}{data.length > 30 && <div>展示前 30 项。</div>}</div>
  return <dl className="space-y-1.5">{Object.entries(data).filter(([k]) => !HIDDEN.has(k)).map(([k, v]) => <div key={k} className={typeof v === 'object' && v !== null ? 'py-1' : 'flex gap-3 justify-between'}>
    <dt className="text-slate-500 shrink-0">{LABELS[k] ?? k}</dt><dd className="text-slate-800 min-w-0"><Tree data={v} depth={depth + 1} /></dd></div>)}</dl>
}
export function StructuredData({ title, data, onNext }: { title: string; data: Json; onNext?: () => void }) {
  if (data === null) return null
  const query = queryResult(data)
  if (query) return <QueryResult data={query} onNext={onNext} />
  if (typeof data === 'object' && !Array.isArray(data) && data.kind === 'query_result_v1') return <p role="alert">查询卡片格式无效，请重新查询。</p>
  const report = cashFlowReport(data)
  if (report) return <CashFlowReport report={report} />
  return <details className="w-full bg-white border border-slate-100 rounded-2xl p-3 text-xs">
    <summary className="text-sm font-semibold text-slate-700 cursor-pointer mb-2">{title}</summary><Tree data={data} />
  </details>
}
