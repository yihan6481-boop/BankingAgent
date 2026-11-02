import { useEffect,useState } from 'react'
import { request } from '../api/client'
import { api } from '../api'
import type { CategorySpendingData, MonthlySpendingData } from '../types'
import { ResultCardRenderer } from './cards/ResultCardRenderer'
import type { UiResult } from '../types'
import { TransactionDetail } from './TransactionDetail'
import { MobileIcon } from './MobileIcon'

function currentMonth(){return new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit'}).format(new Date())}
const yuan = (num:number) => '¥' + num.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})
export function BillsHub({sid, busy, onBusiness, onChat, onPayments}:{sid:string;busy:boolean;onShowResult:(tool:string,args:Record<string,string>)=>Promise<void>;onBusiness:()=>void;onChat:(q:string)=>void;onPayments?:()=>void}){
 const [selectedTransaction,setSelectedTransaction]=useState<number|null>(null)
 const [month,setMonth]=useState(currentMonth)
 const [summary,setSummary]=useState<{income_cents:number;expense_cents:number;refund_cents:number;count:number}|null>(null)
 useEffect(()=>{let live=true;setSummary(null);void request<any>(`/secure/sessions/${sid}/mobile/monthly-summary?month=${month}`,sid).then(v=>{if(live)setSummary(v)}).catch(e=>{if(live)setError(e.message)});return()=>{live=false}},[sid,month])
 const [spending,setSpending]=useState<MonthlySpendingData|null>(null)
 const [categories,setCategories]=useState<CategorySpendingData|null>(null)
 const [ledger,setLedger]=useState<UiResult|null>(null)
 const [categoryError,setCategoryError]=useState('')
 const [error,setError]=useState('')
 const [pending,setPending]=useState(false)
 const load = async()=>{
   if(pending||busy)return
   setPending(true);setSpending(null);setCategories(null);setLedger(null);setCategoryError('');setError('')
   try{
     const data=await api.read(sid,'get_monthly_spending',{month})
     if(data.error?.code==='query_authorization_required'){setError('此项查询需要单次授权，请使用下方“完整账单业务”入口。');return}
     if(data.status==='failed'||data.result?.kind!=='monthly_spending'){setError(data.message||'查询未完成');return}
     setSpending(data.result.data)
     try { const c=await api.read(sid,'get_category_spending',{month})
     if(c.result?.kind==='category_spending'&&c.status==='success')setCategories(c.result.data)
     else setCategoryError('部分数据：月度支出已读取，分类查询未完成。请重试查询。') } catch {setCategoryError('部分数据：分类连接失败，月度支出仍可查看。')}
   }catch(e){setError(e instanceof Error?e.message:'账单连接失败，请检查后端。')}
   finally{setPending(false)}
 }
 const categoriesList = categories ? Object.entries(categories.categories).sort((a,b)=>b[1].amount-a[1].amount).slice(0,5) : []
 const max = Math.max(...categoriesList.map(c=>c[1].amount), 1)
 return <section className="mobile-page bills-page" aria-label="财务账单">
   <div className="eyebrow">SPENDING INSIGHTS</div><h1 className="mobile-heading">账单与分析</h1><p className="muted-copy">来源：模拟银行流水 · 全部银行卡。资金支出包含消费、转账与申购；退款和赎回计入收入。上传账单独立查询。</p>
   <div className="white-tile filter-tile"><label htmlFor="bill-month">账单月份</label><input id="bill-month" aria-label="账单月份" type="month" disabled={pending} value={month} onChange={e=>{setMonth(e.target.value);setSpending(null);setCategories(null);setLedger(null);setError('');setCategoryError('')}}/><button className="mobile-primary" disabled={busy||pending||!month} onClick={()=>void load()}>{pending?'查询中…':'查询'}</button></div>
   {summary&&<div className="summary-tiles" aria-label="月份收支统计"><div className="white-tile"><span>收入（含退款与赎回）</span><strong>{yuan(summary.income_cents/100)}</strong></div><div className="white-tile"><span>资金支出（含转账与申购）</span><strong>{yuan(summary.expense_cents/100)}</strong></div><p>{summary.count} 笔已发生交易 · 退款 ¥{(summary.refund_cents/100).toFixed(2)}</p></div>}
   {error&&<div className="mobile-warning" role="alert">{error}</div>}
   {spending ? <>
     <div className="summary-tiles"><div className="white-tile"><span>资金支出合计</span><strong>{yuan(spending.total_spending)}</strong></div><div className="white-tile"><span>支出交易数</span><strong>{spending.transaction_count} 笔</strong></div></div>
     <div className="white-tile chart-tile"><div className="section-head no-margin"><h2>支出最高的前五分类</h2><span>{spending.month}</span></div>
       {categoryError&&<p role="alert" className="mobile-warning">{categoryError}</p>}{categoriesList.length ? categoriesList.map(([name,stat])=><div className="spend-row" key={name}><div><span>{name}</span><strong>{yuan(stat.amount)}</strong></div><div className="progress-track"><span style={{width:`${Math.max(0,Math.min(100,stat.amount/max*100))}%`}}/></div></div>):<p className="muted-copy">没有可展示的分类数据。</p>}
     </div>
   </>:<div className="white-tile bills-empty"><MobileIcon name="chart" size={30}/><strong>{pending?'正在读取所选月份…':error?'账单暂不可用':'选择月份，查看账单'}</strong><p>不会填充虚构图表。查询成功后展示支出统计和分类。</p></div>}
   <div className="white-tile bill-action-list">
     <button disabled={busy||pending} onClick={async()=>{setPending(true);setError('');try{const out=await api.read(sid,'get_transactions_by_month',{month});if(out.status==='success'&&out.result?.kind==='transaction_list')setLedger(out.result);else setError(out.error?.code==='query_authorization_required'?'流水查询需要授权，请进入完整账单业务。':out.message||'流水查询失败')}catch{setError('流水连接失败，请重试。')}finally{setPending(false)}}}><MobileIcon name="receipt"/>查看该月交易流水<MobileIcon name="chevron" size={16}/></button>
     <button disabled={busy||pending} onClick={()=>onChat(`分析${month}银行流水的收入、支出和主要消费类别，不包含上传账单`)}><MobileIcon name="spark"/>让 AI 分析这一月<MobileIcon name="chevron" size={16}/></button>
     {onPayments&&<button disabled={busy||pending} onClick={onPayments}><MobileIcon name="receipt"/>缴费单据核对与凭证对账<MobileIcon name="chevron" size={16}/></button>}
     <button disabled={busy||pending} onClick={onBusiness}><MobileIcon name="wallet"/>完整账单业务与授权<MobileIcon name="chevron" size={16}/></button>
   </div>
   {spending?.transaction_count===0&&<p className="task-notice">该月在当前口径下无支出记录。</p>}{ledger&&<div className="mt-4"><h2 className="font-semibold">{month} · 银行流水</h2><ResultCardRenderer result={ledger} onTransaction={setSelectedTransaction}/>{ledger.kind==='transaction_list'&&ledger.data.length===0&&<p className="task-notice">该月没有银行流水。</p>}</div>}
   <p className="mobile-footnote">来源：本地模拟交易，包含按九月密度生成的历史；非真实银行导入。全部明细、收支统计与助手使用同一交易表，未来情景不计入已发生金额。</p>
   <p className="mobile-footnote">退款及其他收支应按后端报告分别呈现；此处“支出”只展示指定工具返回的口径。</p>
 {selectedTransaction!==null&&<TransactionDetail sid={sid} id={selectedTransaction} onClose={()=>setSelectedTransaction(null)}/>}
 </section>
}
