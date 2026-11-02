import { useState } from 'react'
import { request } from '../api/client'
export function DemoLedger({sid}:{sid:string}){
 const [events,setEvents]=useState<any[]|null>(null),[future,setFuture]=useState(false),[error,setError]=useState('')
 const load=async(include:boolean)=>{setFuture(include);try{const r=await request<{events:any[]}>(`/secure/sessions/${sid}/mobile/demo-ledger?include_future=${include}`,sid);setEvents(r.events)}catch(e){setError(e instanceof Error?e.message:'读取未完成')}}
 return <details className="demo-ledger white-tile" onToggle={e=>{if(e.currentTarget.open&&events===null)void load(false)}}><summary>独立演示账本 · 2026年1—10月</summary><p className="field-note">下列是单独的余额快照与演示情景，不是已发生银行流水，不参与上方收支统计。每月演示净额为零，未改变现有余额。</p><label className="field-note"><input type="checkbox" checked={future} onChange={e=>void load(e.target.checked)}/>显式查看10月31日未来情景</label>{error&&<p role="alert">{error}</p>}{events?.length===0&&<p className="empty-copy">当前环境没有追加演示情景。</p>}<div className="demo-events">{events?.map((r,i)=><div key={i}><span>{r.day}{r.future_scenario?' · 未来情景':''}<small>{r.label}</small></span><strong>{r.direction==='income'?'+':'−'}¥{(r.amount_cents/100).toFixed(2)}</strong></div>)}</div></details>
}
