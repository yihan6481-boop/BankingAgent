import { useEffect, useRef, useState } from 'react'
import { ApiError, realApi, request } from './api/client'
import type { Binding, Login, TaskView, UiState } from './integrationTypes'
import { LoginPanel } from './components/LoginPanel'
import { SecureActionCard } from './components/SecureActionCard'
import { FactorPanel } from './components/FactorPanel'

type Data = Record<string, any>
interface Turn { id:string; client_message_id:string; number:number; message:string; answer:string; status:string; progress:string; error_code:string|null; calls:Data[] }
interface Conversation { id:string; turns:Turn[]; state:Data }
const saved = <T,>(key:string):T|null => { try{return JSON.parse(sessionStorage.getItem(key)||'null')}catch{return null} }
const yuan = (c:number) => (c/100).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})

function Results({calls}:{calls:Data[]}) {
  return <>{calls.filter(c=>c.status==='completed'&&c.result?.data).map(c=>{
    const d=c.result.data
    if(d.cards)return <section key={c.call_id} className="result-card p-3 rounded-xl bg-white border" aria-label="余额查询结果"><strong>本人卡片与余额</strong><small className="block text-slate-500">查询于 {new Date(d.queried_at).toLocaleString('zh-CN')}；后续操作以实时核验为准</small>{d.cards.map((x:Data)=><p key={x.card_id}>{x.bank_name} {x.masked_card_number} · ¥{Number(x.balance).toFixed(2)} · {x.status}</p>)}</section>
    const rows=d.fields?.result?.results
    if(rows)return <section key={c.call_id} className="result-card p-3 rounded-xl bg-white border" aria-label="账单查询结果"><strong>银行账单</strong><small className="block text-slate-500">查询于 {new Date(d.fields.produced_at).toLocaleString('zh-CN')}</small>{rows.map((r:Data,i:number)=><div key={i}><p>{r.current.scope.period?`${r.current.scope.period.start} 至 ${r.current.scope.period.end_exclusive}（不含结束日）`:'最近记录'} · {r.current.scope.merchants.join('、')||'全部商户'}</p>{r.plan.metrics.some((m:string)=>m!=='details')&&<p>购买 ¥{yuan(r.current.summary.purchase_cents)} · 退款 ¥{yuan(r.current.summary.refund_cents)} · 所选口径 {r.current.summary.count} 笔 / ¥{yuan(r.current.summary.amount_cents)}</p>}{r.current.summary.net_cents!==null&&<p>购买减退款 ¥{yuan(r.current.summary.net_cents)}（按发生日）</p>}{r.current.details.map((x:Data)=><p key={x.record_id}>{x.day} {x.merchant} ¥{yuan(x.amount_cents)}</p>)}{r.current.groups.map((x:Data)=><p key={x.label}>{x.label} {x.count} 笔 ¥{yuan(x.amount_cents)}</p>)}<small>{r.current.coverage}{r.current.truncated?' 当前明细有分页。':''}</small></div>)}</section>
    return null
  })}</>
}

export default function R2App(){
  const [login,setLogin]=useState<Login|null>(()=>saved('r2_login'))
  const [cid,setCid]=useState(()=>sessionStorage.getItem('r2_cid')||'')
  const [conv,setConv]=useState<Conversation|null>(null)
  const [ui,setUi]=useState<UiState|null>(null)
  const [input,setInput]=useState('');const [error,setError]=useState('')
  const [busy,setBusy]=useState(false);const [actionBusy,setActionBusy]=useState(false)
  const [security,setSecurity]=useState(false);const [choices,setChoices]=useState<{id:string;created_at:string}[]>([])
  const pending=useRef<{client_message_id:string;message:string}|null>(saved('r2_pending'))
  const guard=useRef(false)
  const base=login?`/secure/sessions/${encodeURIComponent(login.session_id)}/dialogues`:''
  const sync=async(id=cid)=>{
    if(!login||!id)return
    const value=await request<Conversation>(`${base}/${id}`,login.session_id)
    setConv(value)
    setUi(await request<UiState>(`${base}/${id}/ui-state`,login.session_id))
    if(pending.current&&value.turns.some(t=>t.client_message_id===pending.current?.client_message_id&&t.status!=='running')){pending.current=null;sessionStorage.removeItem('r2_pending')}
    return value
  }
  const handleError=(e:unknown)=>{
    setError(e instanceof Error?e.message:'请求未完成，请同步状态核实。')
    if(e instanceof ApiError&&['unauthorized','credential_expired','session_not_found'].includes(e.code)){
      setLogin(null);setConv(null);setUi(null);setCid('');sessionStorage.removeItem('r2_login');sessionStorage.removeItem('r2_cid')
    }
  }
  const select=(id:string)=>{setCid(id);sessionStorage.setItem('r2_cid',id);void sync(id).catch(handleError)}
  useEffect(()=>{
    if(!login)return
    let live=true
    void request<{dialogues:{id:string;created_at:string}[]}>(base,login.session_id).then(v=>{
      if(!live)return;setChoices(v.dialogues)
      if(cid)void sync(cid).catch(handleError)
    }).catch(handleError)
    return ()=>{live=false}
  },[login?.session_id,cid])
  useEffect(()=>{
    if(!login||!cid)return
    const timer=setInterval(()=>{void sync().catch(handleError)},busy||conv?.turns.some(t=>t.status==='running')?1000:8000)
    return ()=>clearInterval(timer)
  },[login?.session_id,cid,busy,conv?.turns.length])
  const make=async()=>{
    if(!login)return
    const value=await request<{id:string}>(base,login.session_id,'POST',{})
    select(value.id);return value.id
  }
  const doLogin=async(username:string,password:string)=>{
    setActionBusy(true);setError('')
    try{const value=await realApi.login(username,password);setLogin(value);sessionStorage.setItem('r2_login',JSON.stringify(value));setCid('');setConv(null);setUi(null);sessionStorage.removeItem('r2_cid');return value}catch(e){handleError(e);return null}finally{setActionBusy(false)}
  }
  const send=async(retry=false)=>{
    if(!login||guard.current)return
    const text=input.trim();if(!retry&&!text)return
    guard.current=true;setBusy(true);setError('')
    try{
      const id=cid||await make();if(!id)return
      if(!retry){pending.current={client_message_id:crypto.randomUUID(),message:text};sessionStorage.setItem('r2_pending',JSON.stringify(pending.current));setInput('')}
      if(!pending.current)return
      const value=await request<Conversation>(`${base}/${id}/messages`,login.session_id,'POST',pending.current)
      setConv(value);pending.current=null;sessionStorage.removeItem('r2_pending');await sync(id)
    }catch(e){handleError(e)}finally{guard.current=false;setBusy(false)}
  }
  const run=async<T,>(_sid:string|null,fn:()=>Promise<T>):Promise<T|null>=>{
    setActionBusy(true);setError('');try{const value=await fn();await sync();return value}catch(e){handleError(e);return null}finally{setActionBusy(false)}
  }
  const act=(task:TaskView,action:string)=>run(login?.session_id||null,()=>request(`${base}/${cid}/tasks/${task.task.task_id}/action`,login!.session_id,'POST',{revision:task.revision,action}))
  const confirm=(b:Binding,text:string)=>run(b.sessionId,()=>request(`${base}/${cid}/tasks/${b.taskId}/confirm`,b.sessionId,'POST',{revision:b.revision,preview_id:b.previewId,text}))
  const verify=(b:Binding,code:string,method:'totp'|'mock_otp')=>run(b.sessionId,()=>realApi.verify(b,code,method))
  const running=busy||!!conv?.turns.some(t=>t.status==='running')
  return <div className="mobile-stage"><main className="mobile-shell"><header className="mobile-app-header"><div className="mobile-brand-text"><strong>Banking Agent · 新对话</strong><small>{login?.principal.display_name||'请登录'} · 卡片、账单与转账</small></div><a className="text-xs" href="/ui/">原入口</a></header>
    {error&&<div role="alert" className="mobile-error">{error}<button onClick={()=>setError('')}>收起</button></div>}
    {!login?<LoginPanel busy={actionBusy} onLogin={doLogin}/>:<div className="mobile-screen mobile-chat-screen">
      <div className="flex gap-2 p-2 flex-wrap"><button className="secondary" disabled={running} onClick={()=>void make().catch(handleError)}>新对话</button><select aria-label="恢复对话" className="field" value={cid} disabled={running} onChange={e=>select(e.target.value)}><option value="">选择近期对话</option>{choices.map(c=><option key={c.id} value={c.id}>{new Date(c.created_at).toLocaleString('zh-CN')}</option>)}</select><button className="secondary" onClick={()=>{setSecurity(!security)}}>安全验证</button><button className="secondary" onClick={()=>void realApi.logout(login.session_id).catch(handleError).finally(()=>{setLogin(null);sessionStorage.removeItem('r2_login')})}>退出</button></div>
      <section aria-label="新对话消息" className="space-y-3 p-3" aria-live="polite">
        {!conv?.turns.length&&<p className="text-slate-500">可以直接聊天，查询余额、银行账单，或告诉我转账目标。资金操作在独立确认卡中完成。</p>}
        {conv?.turns.map(t=><article key={t.id} className="space-y-2"><p className="rounded-xl p-3 bg-webank-50 whitespace-pre-wrap" data-role="user">{t.message}</p>{t.answer&&<p className="rounded-xl p-3 bg-white whitespace-pre-wrap" data-role="assistant">{t.answer}</p>}{t.status==='running'&&<p role="status">{t.progress}…</p>}<Results calls={t.calls}/>{t.error_code&&<p className="text-amber-700 text-xs">本轮未完成；下方状态与已完成结果仍有效。</p>}</article>)}
      </section>
      {conv?.state.items?.filter((x:Data)=>['transfer','query'].includes(x.kind)&&x.status!=='closed').map((x:Data)=><section key={x.reference} className="p-3 m-2 rounded-xl bg-white border" aria-label="已保存目标"><strong>{x.kind==='transfer'?'转账目标':'账单查询'}</strong>{x.status==='linked'?<p>已关联任务，金额和对象请以下方当前任务为准。</p>:<p>{x.fields.recipient_label||x.resolved_recipient?.name||''} {x.fields.amount?`¥${x.fields.amount}`:''} {x.fields.from_card_id?`付款卡 ${x.fields.from_card_id}`:''} · {x.status==='preparing'?'准备结果需核实':'草稿已保存'}</p>}{x.status!=='linked'&&x.missing_fields?.length>0&&<p className="text-xs">仍需补充：{x.missing_fields.map((k:string)=>({from_card_id:'付款卡',amount:'金额',recipient_label_or_id:'收款人'}[k]||k)).join('、')}</p>}</section>)}
      {conv?.state.items?.filter((x:Data)=>x.kind==='task').map((x:Data)=><section key={x.reference} className="task-notice" aria-label="任务状态"><p>转账任务 · {({waiting:'待确认',completed:'已完成',paused:'已暂停',cancelled:'已取消',active:'处理中',failed:'失败'} as Data)[x.status]||x.status}</p>{x.task.steps.map((s:Data)=><p key={s.step_id}>{s.arguments.recipient_name||`收款人 ${s.arguments.recipient_id}`} · ¥{s.arguments.amount} · 付款卡 {s.arguments.from_card_id}</p>)}{!ui?.task?.preview&&['waiting','paused','active'].includes(x.status)&&<button className="secondary" onClick={()=>void act(x as TaskView,'resume')}>恢复并重新预览</button>}</section>)}
      {ui?.task?.preview&&<SecureActionCard key={ui.task.preview.preview_id} sid={login.session_id} state={ui} busy={actionBusy} onConfirm={confirm} onVerify={verify} onCancel={t=>act(t,'cancel')} onResume={t=>act(t,'resume')} onSecurity={()=>setSecurity(true)}/>}
      {security&&<FactorPanel sid={login.session_id} factor={ui?.factor} busy={actionBusy} run={run}/>}
      <div className="p-3 space-y-2"><button className="secondary" onClick={()=>void sync().catch(handleError)}>同步真实状态</button>{running&&<button className="secondary" onClick={()=>void request<{message:string}>(`${base}/${cid}/cancel`,login.session_id,'POST',{}).then(v=>setError(v.message)).catch(handleError)}>停止等待</button>}{pending.current&&!running&&<button className="secondary" onClick={()=>void send(true)}>核实或重试上一条消息</button>}
      <form onSubmit={e=>{e.preventDefault();void send()}} className="flex gap-2"><textarea aria-label="新对话输入" className="field flex-1" value={input} maxLength={16000} onChange={e=>setInput(e.target.value)} placeholder="说说你想做什么…" disabled={running}/><button className="primary" disabled={running||!input.trim()}>发送</button></form></div>
    </div>}
  </main></div>
}
