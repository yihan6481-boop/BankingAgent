import {useEffect,useState} from 'react'
import {createPortal} from 'react-dom'
import {request} from '../api/client'
const kinds:Record<string,string>={purchase:'购买 / 消费',income:'收入',transfer:'转账',refund:'退款',wealth_buy:'申购',wealth_redeem:'赎回',unknown:'类型待核实'}
export function TransactionDetail({sid,id,onClose}:{sid:string;id:number;onClose:()=>void}){
 const [value,setValue]=useState<any>(null),[error,setError]=useState(''),[receipt,setReceipt]=useState<any>(null)
 useEffect(()=>{let live=true;void request<any>(`/secure/sessions/${sid}/mobile/transactions/${id}`,sid).then(v=>{if(live)setValue(v)}).catch(e=>{if(live)setError(e.message)});return()=>{live=false}},[sid,id])
 const dialog=<div className="modal-shade" onClick={onClose}><section className="detail-dialog" role="dialog" aria-label="交易详情" onClick={e=>e.stopPropagation()}><button className="dialog-close" aria-label="关闭交易详情" onClick={onClose}>×</button><h2>交易详情</h2>{error&&<p role="alert">{error}</p>}{!value?<p>正在读取交易…</p>:<><strong>¥ {(value.amount_cents/100).toFixed(2)}</strong><p>{value.merchant} · {value.direction==='income'?'收入':'支出'} · 已入账</p><p>{kinds[value.transaction_type]} · {value.category}</p><p>银行卡尾号 {value.card_tail}</p><p>{value.event_at?new Date(value.event_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):value.date+'（时间精度未知，仅保留业务日期）'}</p><p>{value.time_source==='demo_rule_v2'?'时间来自可复现的演示生成规则；非真实银行时间。':value.time_source?'时间来自原交易回执。':'原记录未提供秒级时间。'}</p><p>备注：{value.remark||'无'}</p><p>交易编号：{value.transaction_id}</p>{value.receipt_task_id?<button className="secondary" onClick={()=>void request<any>(`/secure/sessions/${sid}/tasks/${value.receipt_task_id}`,sid).then(setReceipt).catch(e=>setError(e.message))}>查看原任务与回执</button>:<p>来源：模拟历史记录，未重新执行资金交易。</p>}{receipt&&<p>原任务：{receipt.task?.task?.contract?.goal} · {receipt.task?.status==='completed'?'已完成':'以原任务状态为准'}</p>}</>}</section></div>
 // Keep nested card activity scroll containers from clipping the detail overlay.
 return createPortal(dialog,document.querySelector('.mobile-shell')||document.body)
}
