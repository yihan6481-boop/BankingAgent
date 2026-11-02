import type { ReactNode } from 'react'
import type {
  UiResult, MonthlySpendingData, CategorySpendingData, TransactionItem, CardItem, SubscriptionItem,
  SubscriptionCostData, RecipientItem, WealthRiskProfileData, WealthProductItem, WealthHoldingItem,
  TransferPreviewData, TransferCompletedData, WealthPurchasePreviewData, WealthOrderCompletedData,
  WealthRedemptionPreviewData,
} from '../../types'
import { formatYuan, formatPct, formatDate, riskName, riskColor, transferRiskColor } from '../../utils/format'

function Card({ title, badge, children }: { title: string; badge?: string; children: ReactNode }) {
  return (
    <div className="rounded-2xl bg-white shadow-card px-4 py-3 border border-slate-100">
      <div className="flex items-center justify-between mb-2">
        <h4 className="text-sm font-semibold text-slate-800">{title}</h4>
        {badge && <span className="text-[11px] px-2 py-0.5 rounded-full bg-webank-50 text-webank-600">{badge}</span>}
      </div>
      {children}
    </div>
  )
}

function Row({ label, value, accent }: { label: string; value: ReactNode; accent?: string }) {
  return (
    <div className="flex items-start justify-between gap-3 py-1 text-sm break-words">
      <span className="text-slate-500">{label}</span>
      <span className={accent ?? 'text-slate-800 font-medium'}>{value}</span>
    </div>
  )
}

function Notice({ text }: { text: string }) {
  return <p className="text-[11px] text-slate-400 mt-2 leading-snug">{text}</p>
}

function MonthlySpending({ d }: { d: MonthlySpendingData }) {
  return (
    <Card title="月度支出统计" badge={d.month}>
      <div className="text-2xl font-bold text-webank-600 mb-1">{formatYuan(d.total_spending)}</div>
      <Row label="交易笔数" value={d.transaction_count} />
      <Row label="统计范围" value={d.card_specified ? `卡 ${d.card_id}` : '全部银行卡'} />
    </Card>
  )
}

function CategorySpending({ d }: { d: CategorySpendingData }) {
  const entries = Object.entries(d.categories).sort((a, b) => b[1].amount - a[1].amount)
  return (
    <Card title="消费分类统计" badge={d.month}>
      <div className="text-2xl font-bold text-webank-600 mb-2">{formatYuan(d.total_spending)}</div>
      <div className="space-y-1.5">
        {entries.map(([name, c]) => (
          <div key={name}>
            <div className="flex justify-between text-xs mb-0.5">
              <span className="text-slate-600">{name}</span>
              <span className="text-slate-400">{formatYuan(c.amount)} · {formatPct(c.percentage)}</span>
            </div>
            <div className="h-1.5 rounded-full bg-slate-100 overflow-hidden">
              <div className="h-full bg-webank-400 rounded-full" style={{ width: `${c.percentage}%` }} />
            </div>
          </div>
        ))}
      </div>
    </Card>
  )
}

function TransactionList({ items,onSelect }: { items: TransactionItem[];onSelect?:(id:number)=>void }) {
  return (
    <Card title="交易流水" badge={`${items.length} 笔`}>
      <details>
      <summary className="cursor-pointer text-sm text-slate-600">查看逐笔账单</summary>
      <div className="divide-y divide-slate-50">
        {items.map((t) => (
          <div key={t.transaction_id} className="flex items-center justify-between gap-2 py-2">{onSelect&&<button type="button" className="shrink-0 text-xs" aria-label={`查看交易${t.transaction_id}`} onClick={()=>onSelect(t.transaction_id)}>详情</button>}
            <div className="min-w-0 flex-1">
              <div className="text-sm text-slate-800">{t.merchant}</div>
              <div className="text-[11px] text-slate-400">{t.event_at?new Date(t.event_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):formatDate(t.date)+'（仅日期）'} · {t.time_source==='demo_rule_v2'?'演示规则时间 · ':''}{t.category} · 尾号{t.card_last_four} · 已入账 · 编号{t.transaction_id}</div>
            </div>
            <div className={'shrink-0 whitespace-nowrap text-right '+(t.direction === 'income' ? 'text-emerald-600 font-semibold' : 'text-slate-700 font-semibold')}>
              {t.direction === 'income' ? '+' : '-'}{formatYuan(t.amount)}
            </div>
          </div>
        ))}
      </div>
      </details>
    </Card>
  )
}

function CardList({ items }: { items: CardItem[] }) {
  return (
    <Card title="我的银行卡" badge={`${items.length} 张`}>
      <div className="space-y-2">
        {items.map((c) => (
          <div key={c.card_id} className="rounded-xl bg-gradient-to-br from-webank-500 to-webank-700 text-white px-3 py-2.5">
            <div className="flex justify-between items-center">
              <span className="text-sm font-medium">{c.bank_name}</span>
              <span className="text-xs opacity-80">{c.status === 'normal' ? '正常' : c.status === 'lost' ? '已挂失' : '冻结'}</span>
            </div>
            <div className="text-lg tracking-widest mt-1">{c.masked_card_number}</div>
            <div className="flex justify-between text-xs mt-1 opacity-90">
              <span>余额 {formatYuan(c.balance)}</span>
              <span>单日限额 {formatYuan(c.daily_limit)}</span>
            </div>
          </div>
        ))}
      </div>
    </Card>
  )
}

function SubscriptionList({ items }: { items: SubscriptionItem[] }) {
  return (
    <Card title="订阅与代扣" badge={`${items.length} 项`}>
      <div className="space-y-2">
        {items.map((s) => (
          <div key={s.subscription_id} className="flex items-center justify-between">
            <div>
              <div className="text-sm text-slate-800">{s.name}</div>
              <div className="text-[11px] text-slate-400">下次扣款 {s.next_charge_date} · 尾号{s.card_last_four}</div>
            </div>
            <div className="text-right">
              <div className="text-sm font-semibold text-slate-700">{formatYuan(s.amount)}/{({monthly:'月',yearly:'年',annual:'年',weekly:'周',daily:'天',quarterly:'季'} as Record<string,string>)[s.cycle]||`周期待核实（${s.cycle}）`}</div>
              <div className={`text-[11px] ${s.status === 'active' ? 'text-emerald-600' : 'text-gray-400'}`}>{s.status === 'active' ? '生效中' : '已取消'}</div>
            </div>
          </div>
        ))}
      </div>
    </Card>
  )
}

function SubscriptionCost({ d }: { d: SubscriptionCostData }) {
  return (
    <Card title="订阅费用合计">
        <p className="text-xs text-slate-500 mb-2">按有效订阅周期折算，非本月实际扣款；月均按分四舍五入。</p>
        {d.complete === false && <p role="status" className="text-xs text-amber-700">存在未知周期，当前仅为已知周期小计，无法完整统计。</p>}
      <div className="flex gap-4">
        <div>
            <div className="text-[11px] text-slate-400">月均折算</div>
          <div className="text-xl font-bold text-webank-600">{formatYuan(d.monthly_cost)}</div>
        </div>
        <div>
            <div className="text-[11px] text-slate-400">年度折算</div>
          <div className="text-xl font-bold text-slate-700">{formatYuan(d.annual_cost)}</div>
        </div>
        <div>
          <div className="text-[11px] text-slate-400">有效项</div>
          <div className="text-xl font-bold text-slate-700">{d.active_count}</div>
        </div>
      </div>
    </Card>
  )
}

function RecipientList({ items }: { items: RecipientItem[] }) {
  return (
    <Card title="常用收款人" badge={`${items.length} 位`}>
      <div className="space-y-2">
        {items.map((r) => (
          <div key={r.recipient_id} className="flex items-center justify-between">
            <div>
              <div className="text-sm text-slate-800">{r.name}</div>
              <div className="text-[11px] text-slate-400">{r.bank_name} · {r.masked_account}</div>
            </div>
            {r.trusted ? <span className="text-[11px] px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-700">可信</span>
              : <span className="text-[11px] px-2 py-0.5 rounded-full bg-rose-100 text-rose-700">未核验</span>}
          </div>
        ))}
      </div>
    </Card>
  )
}

function RiskProfile({ d }: { d: WealthRiskProfileData }) {
  return (
    <Card title="风险等级评估">
      <div className="flex items-center gap-3">
        <span className={`text-xs px-2 py-1 rounded-full ${riskColor(d.risk_level)}`}>{d.risk_level}</span>
        <span className="text-lg font-bold text-slate-800">{riskName(d.risk_level)}</span>
      </div>
      <Notice text={d.notice} />
    </Card>
  )
}

function WealthProductList({ items }: { items: WealthProductItem[] }) {
  return (
    <Card title="理财产品" badge={`${items.length} 款`}>
      <div className="space-y-2">
        {items.map((p) => (
          <div key={p.product_id} className="rounded-xl border border-slate-100 px-3 py-2">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-slate-800">{p.name}</span>
              <span className={`text-[11px] px-2 py-0.5 rounded-full ${riskColor(p.risk_level)}`}>{p.risk_level}</span>
            </div>
            <div className="flex justify-between text-xs text-slate-500 mt-1">
              <span>参考年化 {p.reference_annual_rate}%</span>
              <span>{p.duration_days>0?`期限 ${p.duration_days} 天`:'期限/赎回规则待核实'}</span>
              <span>起购 {formatYuan(p.min_purchase)}</span>
            </div>
          </div>
        ))}
      </div>
    </Card>
  )
}

function WealthHoldingList({ items }: { items: WealthHoldingItem[] }) {
  return (
    <Card title="我的持仓" badge={`${items.length} 笔`}>
      <div className="space-y-2">
        {items.map((h) => (
          <div key={h.holding_id} className="flex items-center justify-between">
            <div>
              <div className="text-sm text-slate-800">{h.product_name}</div>
              <div className="text-[11px] text-slate-400">买入 {formatDate(h.purchased_at.slice(0, 10))}</div>
            </div>
            <div className="text-right">
              <div className="text-sm font-semibold text-slate-700">{formatYuan(h.principal)}</div>
              <div className={`text-[11px] ${h.status === 'active' ? 'text-emerald-600' : 'text-gray-400'}`}>{h.status === 'active' ? '持有中' : '已赎回'}</div>
            </div>
          </div>
        ))}
      </div>
    </Card>
  )
}

function TransferPreview({ d }: { d: TransferPreviewData }) {
  return (
    <Card title="转账确认" badge="待确认">
      <Row label="付款卡" value={`${d.from_bank_name} ${d.from_masked_card_number}`} />
      <Row label="收款人" value={`${d.recipient_name} · ${d.recipient_bank_name.replace(/模拟目标[:：]?/g,'').trim()}`} />
      <Row label="收款账号" value={d.recipient_masked_account} />
      <Row label="金额" value={formatYuan(d.amount)} accent="text-webank-600 font-bold" />
      <Row label="风险等级" value={<span className={`text-[11px] px-2 py-0.5 rounded-full ${transferRiskColor(d.risk_level)}`}>{d.risk_level}</span>} />
    </Card>
  )
}

function TransferCompleted({ d }: { d: TransferCompletedData }) {
  return (
    <Card title="转账成功" badge="已完成">
      <Row label="收款人" value={`${d.recipient_name} · ${d.recipient_masked_account}`} />
      <Row label="金额" value={formatYuan(d.amount)} accent="text-emerald-600 font-bold" />
      <Row label="付款前余额" value={formatYuan(d.balance_before)} />
      <Row label="付款后余额" value={formatYuan(d.balance_after)} />
      {d.idempotent_replay && <Notice text="该操作为幂等重放，未重复扣款。" />}
    </Card>
  )
}

function WealthPurchasePreview({ d }: { d: WealthPurchasePreviewData }) {
  return (
    <Card title="购买确认" badge="待确认">
      <Row label="产品" value={d.product_name} />
      <Row label="产品风险" value={<span className={`text-[11px] px-2 py-0.5 rounded-full ${riskColor(d.product_risk_level)}`}>{d.product_risk_level}</span>} />
      <Row label="我的风险" value={<span className={`text-[11px] px-2 py-0.5 rounded-full ${riskColor(d.user_risk_level)}`}>{d.user_risk_level}</span>} />
      <Row label="购买金额" value={formatYuan(d.amount)} accent="text-webank-600 font-bold" />
      <Row label="参考年化" value={`${d.reference_annual_rate}%`} />
      <Row label="期限" value={`${d.duration_days} 天`} />
      <Notice text={d.notice} />
    </Card>
  )
}

function WealthRedemptionPreview({ d }: { d: WealthRedemptionPreviewData }) {
  return (
    <Card title="赎回确认" badge="待确认">
      <Row label="产品" value={d.product_name} />
      <Row label="当前本金" value={formatYuan(d.current_principal)} />
      <Row label="赎回金额" value={formatYuan(d.redemption_amount)} accent="text-webank-600 font-bold" />
      <Row label="剩余本金" value={formatYuan(d.remaining_principal)} />
      <Notice text={d.notice} />
    </Card>
  )
}

function WealthOrderCompleted({ d }: { d: WealthOrderCompletedData }) {
  return (
    <Card title={d.order_type === 'BUY' ? '购买成功' : '赎回成功'} badge="已完成">
      <Row label="产品" value={d.product_name} />
      <Row label="金额" value={formatYuan(d.amount)} accent="text-emerald-600 font-bold" />
      <Row label="本金变动后" value={formatYuan(d.principal_after)} />
      <Row label="卡余额变动后" value={formatYuan(d.card_balance_after)} />
      {d.idempotent_replay && <Notice text="该操作为幂等重放，未重复处理。" />}
      <Notice text={d.notice} />
    </Card>
  )
}

function DailyLimitPreview({ d, completed }: { d: { old_limit: number; new_limit: number; masked_card_number: string }; completed?: boolean }) {
  return (
    <Card title={completed ? '限额修改成功' : '修改限额确认'} badge={completed ? '已完成' : '待确认'}>
      <Row label="银行卡" value={d.masked_card_number} />
      <Row label="原限额" value={formatYuan(d.old_limit)} />
      <Row label="新限额" value={formatYuan(d.new_limit)} accent="text-webank-600 font-bold" />
    </Card>
  )
}

function CardLostPreview({ d, completed }: { d: { masked_card_number: string; bank_name?: string; status?: string }; completed?: boolean }) {
  return (
    <Card title={completed ? '挂失成功' : '挂失确认'} badge={completed ? '已完成' : '待确认'}>
      <Row label="银行" value={d.bank_name ?? '—'} />
      <Row label="卡号" value={d.masked_card_number} />
      {completed && <Row label="状态" value={<span className="text-rose-600">已挂失</span>} />}
    </Card>
  )
}

function CancelSubPreview({ d, completed }: { d: SubscriptionItem; completed?: boolean }) {
  return (
    <Card title={completed ? '已停止订阅' : '停止订阅确认'} badge={completed ? '已完成' : '待确认'}>
      <Row label="订阅" value={d.name} />
      <Row label="金额" value={`${formatYuan(d.amount)}/月`} />
      <Row label="状态" value={<span className={completed ? 'text-gray-500' : 'text-amber-600'}>{completed ? '已取消' : '生效中'}</span>} />
    </Card>
  )
}

export function ResultCardRenderer({ result,onTransaction }: { result: UiResult;onTransaction?:(id:number)=>void }) {
  switch (result.kind) {
    case 'monthly_spending': return <MonthlySpending d={result.data} />
    case 'category_spending': return <CategorySpending d={result.data} />
    case 'transaction_list': return <TransactionList items={result.data} onSelect={onTransaction} />
    case 'card_list': return <CardList items={result.data} />
    case 'subscription_list': return <SubscriptionList items={result.data} />
    case 'subscription_cost': return <SubscriptionCost d={result.data} />
    case 'recipient_list': return <RecipientList items={result.data} />
    case 'wealth_risk_profile': return <RiskProfile d={result.data} />
    case 'wealth_product_list': return <WealthProductList items={result.data} />
    case 'wealth_holding_list': return <WealthHoldingList items={result.data} />
    case 'transfer_preview': return <TransferPreview d={result.data} />
    case 'transfer_completed': return <TransferCompleted d={result.data} />
    case 'wealth_purchase_preview': return <WealthPurchasePreview d={result.data} />
    case 'wealth_redemption_preview': return <WealthRedemptionPreview d={result.data} />
    case 'wealth_purchase_completed':
    case 'wealth_redemption_completed': return <WealthOrderCompleted d={result.data} />
    case 'daily_limit_preview': return <DailyLimitPreview d={result.data} />
    case 'daily_limit_completed': return <DailyLimitPreview d={result.data} completed />
    case 'card_lost_preview': return <CardLostPreview d={result.data} />
    case 'card_lost_completed': return <CardLostPreview d={result.data} completed />
    case 'cancel_subscription_preview': return <CancelSubPreview d={result.data} />
    case 'cancel_subscription_completed': return <CancelSubPreview d={result.data} completed />
    default: return null
  }
}
