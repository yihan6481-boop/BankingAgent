import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { Json, Plan, ReadEnvelope } from '../integrationTypes'
import type { CardItem, RecipientItem, WealthProductItem, WealthHoldingItem, SubscriptionItem } from '../types'
import type { useChat } from '../store/useChat'
type Controller = ReturnType<typeof useChat>
const TITLES: Record<string, string> = { transfer: '智能转账', card: '银行卡管理', subscription: '订阅与代扣', wealth: '模拟理财', bill: '账单分析', workflow: '跨场景任务' }
function previousMonth() { const date = new Date(); date.setDate(1); date.setMonth(date.getMonth() - 1); return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}` }
export function BusinessPanel({ sid, initialDomain, ctrl, onChat }: { sid: string; initialDomain: string; ctrl: Controller; onChat: () => void }) {
  const [domain, setDomain] = useState(initialDomain)
  useEffect(()=>setDomain(initialDomain),[initialDomain])
  const [month, setMonth] = useState(previousMonth)
  const [cards, setCards] = useState<CardItem[]>([])
  const [recipients, setRecipients] = useState<RecipientItem[]>([])
  const [products, setProducts] = useState<WealthProductItem[]>([])
  const [holdings, setHoldings] = useState<WealthHoldingItem[]>([])
  const [subscriptions, setSubscriptions] = useState<SubscriptionItem[]>([])
  const [card, setCard] = useState('')
  const [recipient, setRecipient] = useState('')
  const [product, setProduct] = useState('')
  const [product2, setProduct2] = useState('')
  const [holding, setHolding] = useState('')
  const [subscription, setSubscription] = useState('')
  const [amount, setAmount] = useState('')
  const [limit, setLimit] = useState('')
  const [cardAction, setCardAction] = useState('limit')
  const [wealthAction, setWealthAction] = useState('purchase')
  const [condition, setCondition] = useState(false)
  const [threshold, setThreshold] = useState('')
  const [pendingQuery, setPendingQuery] = useState<{ tool: string; args: Record<string, Json> } | null>(null)
  const [optionsError,setOptionsError]=useState('')
  const initial = useRef(false)
  const load = async () => {
    const out = await ctrl.run(sid, async () => {
      const values = await Promise.allSettled(['get_cards', 'get_transfer_recipients', 'get_wealth_products', 'get_wealth_holdings'].map(tool => api.read(sid, tool, {})))
      // Independent option lists must not disappear when a protected list needs
      // additional authorization. Expired credentials still use the controller.
      for(const v of values)if(v.status==='rejected'&&v.reason?.status===401)throw v.reason
      setOptionsError(values.some(v=>v.status==='rejected'||v.value.status==='failed')?'部分选项未读取，可能需要授权或连接重试。已读取的选项仍可使用；未读取的信息不会推测。':'')
      return values.map(v=>v.status==='fulfilled'?v.value:null)
    }, false)
    if (out) {
      const c = out[0]?.result; if (c?.kind === 'card_list') setCards(c.data)
      const r = out[1]?.result; if (r?.kind === 'recipient_list') setRecipients(r.data)
      const p = out[2]?.result; if (p?.kind === 'wealth_product_list') setProducts(p.data)
      const h = out[3]?.result; if (h?.kind === 'wealth_holding_list') setHoldings(h.data)
    }
    return !!out
  }
  useEffect(() => { if (!initial.current && !ctrl.loading && !ctrl.isBusy()) { initial.current = true; void load() } })
  const consume = (out: ReadEnvelope | null) => {
    if (out?.result?.kind === 'subscription_list') setSubscriptions(out.result.data)
  }
  const read = async (tool: string, args: Record<string, Json> = {}) => {
    const out = await ctrl.read(tool, args)
    if (out?.error?.code === 'query_authorization_required') setPendingQuery({ tool, args })
    else { consume(out); onChat() }
  }
  const submit = async () => {
    let tool = ''; let args: Record<string, Json> = {}; let title = ''; let domains = [domain]
    if (domain === 'transfer' || domain === 'workflow') {
      const selected = recipients.find(r => r.recipient_id === Number(recipient))
      tool = 'transfer_money'; args = { from_card_id: Number(card), recipient_id: Number(recipient), amount }
      title = `从所选银行卡向${selected?.name || '所选收款人'}转账 ${amount} 元`
    } else if (domain === 'card') {
      tool = cardAction === 'lost' ? 'report_card_lost' : 'set_daily_limit'
      args = cardAction === 'lost' ? { card_id: Number(card) } : { card_id: Number(card), daily_limit: limit }
      title = cardAction === 'lost' ? '挂失所选银行卡' : `将所选银行卡每日限额调整为 ${limit} 元`
    } else if (domain === 'subscription') {
      tool = 'cancel_subscription'; args = { subscription_id: Number(subscription) }
      title = `取消${subscriptions.find(s => s.subscription_id === Number(subscription))?.name || '所选订阅'}代扣`
    } else if (domain === 'wealth') {
      tool = wealthAction === 'purchase' ? 'buy_wealth_product' : 'redeem_wealth_product'
      args = wealthAction === 'purchase' ? { card_id: Number(card), product_id: Number(product), amount } : { holding_id: Number(holding), amount }
      title = `${wealthAction === 'purchase' ? '购买所选模拟理财' : '赎回所选模拟持仓'} ${amount} 元`
    }
    const plan: Plan = { contract: { contract_version: '1.0', goal: title, domains, requested_tools: [tool], constraints: [] },
      steps: [{ step_id: 'operation', purpose: title, tool_name: tool, arguments: args, depends_on: [] }] }
    if (domain === 'workflow') {
      domains = ['bill', 'transfer']
      plan.contract = { ...plan.contract, goal: `查询 ${month} 支出后，${condition ? `仅在支出小于 ${threshold} 元时，` : ''}${title}`, domains, requested_tools: ['get_monthly_spending', tool] }
      plan.steps.unshift({ step_id: 'report', purpose: `查询 ${month} 支出`, tool_name: 'get_monthly_spending', arguments: { month }, depends_on: [] })
      plan.steps[1].depends_on = ['report']
      if (condition) plan.steps[1].condition = { source_step_id: 'report', field_path: 'data.total_spending', operator: 'lt', value: Number(threshold) }
    }
    if (await ctrl.createTask(plan)) onChat()
  }
  const cardSelect = <label className="block">银行卡<select aria-label="选择银行卡" className="field mt-1 w-full" value={card} onChange={e => setCard(e.target.value)} disabled={ctrl.loading}>
    <option value="">请选择</option>{cards.map(c => <option value={c.card_id} key={c.card_id}>{c.bank_name} · {c.masked_card_number} · 余额 {c.balance.toFixed(2)} 元</option>)}</select></label>
  const money = <label className="block">金额（元）<input aria-label="业务金额" type="text" inputMode="decimal" value={amount} onChange={e => setAmount(e.target.value)} placeholder="例如 100.00" className="field mt-1 w-full" disabled={ctrl.loading} /></label>
  const validMoney = /^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$/.test(amount) && Number(amount) > 0
  const valid = (domain === 'transfer' || domain === 'workflow') ? !!card && !!recipient && validMoney && (domain !== 'workflow' || !!month && (!condition || threshold.trim() !== '' && Number.isFinite(Number(threshold)) && Number(threshold) >= 0)) :
    domain === 'card' ? !!card && (cardAction === 'lost' || /^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$/.test(limit) && Number(limit) > 0) :
    domain === 'subscription' ? !!subscription : domain === 'wealth' ? validMoney && (wealthAction === 'purchase' ? !!card && !!product : !!holding) : false
  return <div className="mobile-page business-page space-y-4">
    <div className="flex justify-between items-center"><h2 className="font-semibold">{TITLES[domain]}</h2><button className="secondary" disabled={ctrl.loading} onClick={() => void load()}>刷新选项</button></div>
    {optionsError&&<p className="task-notice" role="status">{optionsError}</p>}
    <select aria-label="业务类别" className="field w-full" value={domain} onChange={e => { setDomain(e.target.value); setPendingQuery(null) }} disabled={ctrl.loading}>{Object.entries(TITLES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
    {pendingQuery && <section className="rounded-xl bg-amber-50 p-3 space-y-2"><p className="text-xs">订阅与代扣查询需要明确授权；授权前不会展示数据。</p><button className="primary" disabled={ctrl.loading} onClick={async () => {
      const q = pendingQuery; setPendingQuery(null); const out = await ctrl.read(q.tool, q.args, true); consume(out)
    }}>授权本次订阅查询</button><button className="secondary ml-2" onClick={() => setPendingQuery(null)} disabled={ctrl.loading}>取消查询</button></section>}
    {(domain === 'bill' || domain === 'workflow') && <label className="block">查询月份<input aria-label="查询月份" type="month" value={month} onChange={e => setMonth(e.target.value)} className="field mt-1 w-full" disabled={ctrl.loading} /></label>}
    {domain === 'bill' && <div className="grid grid-cols-2 gap-2">
      <button className="secondary" disabled={ctrl.loading || !month} onClick={() => void read('get_monthly_spending', { month })}>月度支出</button>
      <button className="secondary" disabled={ctrl.loading || !month} onClick={() => void read('get_category_spending', { month })}>消费分类</button>
      <button className="secondary" disabled={ctrl.loading || !month} onClick={() => void read('get_transactions_by_month', { month })}>交易流水</button>
      <button className="primary" disabled={ctrl.loading || !month} onClick={async () => { await ctrl.analysis('get_monthly_cash_flow_report', { month }); onChat() }}>月度收支报告</button>
      <button className="secondary" disabled={ctrl.loading || !month} onClick={async () => { await ctrl.analysis('get_expense_clues', { month }); onChat() }}>支出核查线索</button>
      <button className="secondary" disabled={ctrl.loading || !month} onClick={async () => { await ctrl.analysis('get_annual_cash_flow_report', { year: Number(month.slice(0, 4)) }); onChat() }}>该年度收支报告</button>
    </div>}
    {domain === 'card' && <><button className="secondary" disabled={ctrl.loading} onClick={() => void read('get_cards')}>查看银行卡</button>
      <select aria-label="银行卡操作" value={cardAction} onChange={e => setCardAction(e.target.value)} className="field w-full"><option value="limit">修改每日限额</option><option value="lost">挂失银行卡</option></select>
      {cardSelect}{cardAction === 'limit' && <label className="block">新每日限额（元）<input aria-label="新每日限额" value={limit} inputMode="decimal" onChange={e => setLimit(e.target.value)} className="field mt-1 w-full" disabled={ctrl.loading} /></label>}</>}
    {domain === 'subscription' && <><div className="flex gap-2"><button className="secondary" disabled={ctrl.loading} onClick={() => void read('get_active_subscriptions')}>查询有效订阅</button><button className="secondary" disabled={ctrl.loading} onClick={() => void read('get_monthly_subscription_cost')}>查询订阅费用</button></div>
      <label className="block">选择要取消的订阅<select aria-label="选择订阅" value={subscription} onChange={e => setSubscription(e.target.value)} className="field mt-1 w-full" disabled={ctrl.loading}><option value="">请先授权查询订阅</option>{subscriptions.map(s => <option key={s.subscription_id} value={s.subscription_id}>{s.name} · {s.amount.toFixed(2)} 元 / {s.cycle}</option>)}</select></label></>}
    {(domain === 'transfer' || domain === 'workflow') && <>{cardSelect}<label className="block">收款人<select aria-label="选择收款人" value={recipient} onChange={e => setRecipient(e.target.value)} className="field mt-1 w-full" disabled={ctrl.loading}><option value="">请选择</option>{recipients.map(r => <option key={r.recipient_id} value={r.recipient_id}>{r.name} · {r.bank_name} · {r.masked_account}</option>)}</select></label>{money}
      {domain === 'workflow' && <div className="bg-white border rounded-xl p-3 space-y-2"><label className="text-xs"><input type="checkbox" checked={condition} onChange={e => setCondition(e.target.checked)} /> 仅在查询到的支出低于指定金额时转账</label>{condition && <input aria-label="支出条件上限" className="field w-full" inputMode="decimal" value={threshold} onChange={e => setThreshold(e.target.value)} placeholder="支出条件上限（元）" />}</div>}</>}
    {domain === 'wealth' && <><div className="flex gap-2"><button className="secondary" disabled={ctrl.loading} onClick={() => void read('get_wealth_products')}>查看产品</button><button className="secondary" disabled={ctrl.loading} onClick={() => void read('get_wealth_holdings')}>查看持仓</button><button className="secondary" disabled={ctrl.loading} onClick={() => void read('get_risk_profile')}>风险等级</button></div>
      <select aria-label="理财操作" value={wealthAction} onChange={e => setWealthAction(e.target.value)} className="field w-full"><option value="purchase">购买模拟理财</option><option value="redeem">赎回模拟持仓</option></select>
      {wealthAction === 'purchase' ? <>{cardSelect}<label className="block">模拟理财产品<select aria-label="选择理财产品" value={product} onChange={e => setProduct(e.target.value)} className="field mt-1 w-full"><option value="">请选择</option>{products.map(p => <option key={p.product_id} value={p.product_id}>{p.name} · {p.risk_level} · 起购 {p.min_purchase} 元</option>)}</select></label></> :
        <label className="block">模拟持仓<select aria-label="选择持仓" value={holding} onChange={e => setHolding(e.target.value)} className="field mt-1 w-full"><option value="">请选择</option>{holdings.map(h => <option key={h.holding_id} value={h.holding_id}>{h.product_name} · 本金 {h.principal} 元</option>)}</select></label>}
      {money}
      <details className="bg-white border rounded-xl p-3"><summary className="text-xs cursor-pointer">产品对比 / 适当性解释</summary><div className="space-y-2 mt-2">
        <p className="text-xs text-slate-500">先在上方选择产品，以下操作只分析。</p>
        <select aria-label="第二个对比产品" value={product2} onChange={e => setProduct2(e.target.value)} className="field w-full"><option value="">选择另一款产品</option>{products.map(p => <option key={p.product_id} value={p.product_id}>{p.name} · {p.risk_level}</option>)}</select>
        <button className="secondary" disabled={ctrl.loading || !product || !product2 || product === product2} onClick={async () => { await ctrl.analysis('compare_wealth_products', { product_ids: [Number(product), Number(product2)] }); onChat() }}>对比所选产品</button>
        <button className="secondary" disabled={ctrl.loading || !product} onClick={async () => { await ctrl.analysis('explain_wealth_suitability', { product_id: Number(product), ...(card ? { card_id: Number(card) } : {}), ...(validMoney ? { amount } : {}) }); onChat() }}>解释适当性</button>
      </div></details></>}
    {domain !== 'bill' && <button className="primary w-full" disabled={ctrl.loading || !valid} onClick={() => void submit()}>生成任务并查看预览</button>}
    <p className="text-xs text-slate-400">本页使用模拟银行数据。生成预览后请核对参数，完成必要验证并明确确认。</p>
  </div>
}
