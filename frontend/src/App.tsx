import { useEffect, useState } from 'react'
import { useDialogue } from './store/useDialogue'
import { DialogueState } from './components/DialogueState'
import { useChat } from './store/useChat'
import { ChatPanel } from './components/AssistantChatPanel'
import { LoginPanel } from './components/LoginPanel'
import { SecureActionCard } from './components/SecureActionCard'
import { FactorPanel } from './components/FactorPanel'
import { TasksPanel } from './components/TasksPanel'
import { MemoryPanel } from './components/MemoryPanel'
import { BusinessPanel } from './components/BusinessPanel'
import { BillVisionPanel } from './components/BillVisionPanel'
import { api } from './api'
import { HomeDashboard } from './components/HomeDashboard'
import { BillsHub } from './components/BillsHub'
import { ProfileHub } from './components/AccountProfileHub'
import { CurrentTaskPanel } from './components/CurrentTaskPanel'
import { MobileIcon } from './components/MobileIcon'
import type { MobileIconName } from './components/MobileIcon'
import { buildStatusLayers } from './components/statusModel'
import { factorRecovery } from './components/factorRecovery'
import { FactorBindingGuide } from './components/FactorBindingGuide'
import { PaymentsHub } from './components/payments/PaymentsHub'
import type { TaskView } from './integrationTypes'
import { request } from './api/client'
import { PaymentActionCard,PaymentSecurityCenter } from './components/PaymentSecurity'
import type { PaymentSecurityStatus } from './components/PaymentSecurity'
import { SmartTransfer } from './components/SmartTransfer'
import { ProductCatalog } from './components/ProductCatalog'
import { SupportPage } from './components/SupportPage'
import { BrandMark } from './components/BrandMark'
import { FaultNotice } from './components/FaultNotice'
import { useMobileViewport } from './hooks/useMobileViewport'

type Tab = 'home'|'chat'|'bills'|'profile'|'business'|'tasks'|'memory'|'security'|'billvision'|'payments'|'support'
type CapabilityStatus={paid_model_enabled:boolean;reason:string;ocr_enabled:boolean;ocr_reason:string;memory_configured:boolean}
const serviceReason=(code:string)=>({offline_disabled:'离线模式已关闭调用',model_disabled:'服务端未启用',vision_not_configured:'图像服务未配置',paid_budget_exhausted:'本地调用额度已用尽',budget_configuration_invalid:'本地预算配置无效',budget_configuration_mismatch:'本地预算与账本不匹配',budget_ledger_invalid:'本地调用账本不可读',provider_authentication_failed:'服务商未接受凭据',provider_balance_insufficient:'服务商余额不足，需充值',provider_rate_limited:'服务商最近一次限流，请稍后重试',provider_network_unavailable:'最近一次连接服务商失败'}[code]||'配置待核实')
const TITLES:Record<Tab,string> = {
  home:'AI助手',chat:'AI助手',bills:'账单与收支',profile:'我的',business:'银行业务',tasks:'我的待办与回执',memory:'记忆与偏好',security:'安全中心',billvision:'识图记账',payments:'单据核对与转账',support:'人工客服',
}

export default function App(){
  useMobileViewport()
  const ctrl = useChat(true)
  const {active} = ctrl
  const [paidStatus,setPaidStatus]=useState<CapabilityStatus|null>(null)
  const dialogue = useDialogue(active,true)
  useEffect(()=>{let live=true;if(active?.valid&&!dialogue.busy)void request<CapabilityStatus>(`/secure/sessions/${encodeURIComponent(active.id)}/mobile/capabilities`,active.id).then(v=>{if(live)setPaidStatus(v)}).catch(()=>{});else if(!active?.valid)setPaidStatus(null);return()=>{live=false}},[active?.id,active?.valid,dialogue.busy])
  const [online,setOnline]=useState(navigator.onLine),[support,setSupport]=useState<{automation_paused:boolean;state:string}|null>(null)
  useEffect(()=>{const update=()=>setOnline(navigator.onLine);window.addEventListener('online',update);window.addEventListener('offline',update);return()=>{window.removeEventListener('online',update);window.removeEventListener('offline',update)}},[])
  useEffect(()=>{if(!active?.valid){setSupport(null);return}let live=true;const load=()=>{if(document.visibilityState==='visible')void request<{automation_paused:boolean;state:string}>(`/secure/sessions/${encodeURIComponent(active.id)}/mobile/support`,active.id).then(v=>{if(live)setSupport(v)}).catch(()=>{})};load();const t=setInterval(load,3000);return()=>{live=false;clearInterval(t)}},[active?.id,active?.valid])
  const [quick,setQuick]=useState(false)
  const [billBusy,setBillBusy] = useState(false)
  const loading = ctrl.loading || billBusy || dialogue.busy
  const bankTask=active?.state?.task,dialogueTask=dialogue.ui?.task
  const bankTaskActive=bankTask&&!['completed','cancelled','failed'].includes(bankTask.task.status)
  const standaloneTask=quick||!!bankTask&&(!dialogueTask||!!bankTaskActive&&bankTask.task.task.task_id!==dialogueTask.task.task.task_id)
  // A refreshed manual task survives an empty/unrelated dialogue. Its actions
  // keep the controller that owns the canonical preview; query text stays in chat.
  const currentState = standaloneTask ? (active?.state ?? null) : dialogue.ui
  const currentTask = currentState?.task ?? null
  const factor = active?.state?.factor
  const drafts = quick ? [] : (dialogue.conv?.state.items ?? [])
  const recovery = factorRecovery({ factor, tasks: ctrl.tasks, task: currentTask, turns: quick ? [] : (dialogue.conv?.turns ?? []) })
  const layers = buildStatusLayers({ turn: quick ? null : dialogue.lastTurn, task: currentTask, drafts, factor, results: currentState?.results, now: Date.now() })
  const [tab,setTab] = useState<Tab>('chat')
  const [reading,setReading]=useState(false)
  const [history,setHistory] = useState<Tab[]>([])
  const [visited,setVisited] = useState<Tab[]>(['chat'])
  const [domain,setDomain] = useState('bill')
  const [productSection,setProductSection]=useState<'products'|'compare'|'holdings'>('products')
  const [productVisit,setProductVisit]=useState(0)
  const [contactVisit,setContactVisit]=useState(0)
  const [transferSection,setTransferSection]=useState<'transfer'|'contacts'>('transfer')
  const [drawer,setDrawer] = useState(false)
  const [services,setServices]=useState(false)
  const [attachments,setAttachments]=useState(false)
  const [taskOpen,setTaskOpen]=useState(false)
  const [invoiceDocument,setInvoiceDocument]=useState('')
  const [invoiceVersion,setInvoiceVersion]=useState(0)
  const [loginOpen,setLoginOpen] = useState(false)
  const [paymentSecurity,setPaymentSecurity]=useState<PaymentSecurityStatus>({enabled:false})
  useEffect(()=>{let live=true;setPaymentSecurity({enabled:false});if(active?.valid)void request<PaymentSecurityStatus>(`/secure/sessions/${active.id}/mobile/security`,active.id).then(v=>{if(live)setPaymentSecurity(v)}).catch(()=>{});return()=>{live=false}},[active?.id,active?.valid])
  useEffect(()=>{
    if(!drawer)return
    const previous=document.activeElement as HTMLElement|null
    const panel=document.querySelector('.mobile-drawer')
    const controls=()=>Array.from(panel?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled)')||[])
    controls()[0]?.focus()
    const handle=(e:KeyboardEvent)=>{
      if(e.key==='Escape'){setDrawer(false);return}
      if(e.key!=='Tab')return
      const items=controls(), first=items[0], last=items[items.length-1]
      if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus()}
      else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus()}
    }
    document.addEventListener('keydown',handle)
    return()=>{document.removeEventListener('keydown',handle);previous?.focus()}
  },[drawer])
  useEffect(()=>{
    if(!services&&!attachments)return
    const previous=document.activeElement as HTMLElement|null
    const panel=document.querySelector('.services-sheet')
    const controls=()=>Array.from(panel?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled)')||[])
    controls()[0]?.focus()
    const handler=(event:KeyboardEvent)=>{if(event.key==='Escape'){setServices(false);setAttachments(false);return}if(event.key==='Tab'){const all=controls(),first=all[0],last=all[all.length-1];if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus()}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus()}}}
    document.addEventListener('keydown',handler);return()=>{document.removeEventListener('keydown',handler);previous?.focus()}
  },[services,attachments])
  const [,setHealth] = useState<{backend_version:string;llm_configured:boolean}|null>(null)
  const [healthStatus,setHealthStatus] = useState<'checking'|'ready'|'failed'>('checking')
  const checkHealth=()=>{setHealthStatus('checking');void api.health().then(v=>{setHealth(v);setHealthStatus('ready')}).catch(()=>setHealthStatus('failed'))}
  useEffect(checkHealth,[])
  useEffect(()=>{setVisited(v=>v.includes(tab)?v:[...v,tab])},[tab])
  useEffect(()=>{
    if(!active?.valid)return
    const timer=window.setInterval(()=>{if(!loading&&document.visibilityState==='visible')void ctrl.sync(true)},15000)
    return ()=>clearInterval(timer)
  },[active?.id,active?.valid,loading,ctrl.sync])
  const go=(page:Tab)=>{if(page!=='chat')setTaskOpen(false);setDrawer(false);setServices(false);setAttachments(false);if(page!==tab){if(tab==='chat'&&/^[0-9]{6}$|验证码|动态码|密码|otpauth:|Bearer\s/i.test(active?.draft||''))ctrl.setDraft('');setHistory(h=>[...h.slice(-19),tab]);setTab(page)}}
  const back=()=>{setTab(history[history.length-1]||'chat');setHistory(h=>h.slice(0,-1))}
  const goBusiness=(nextDomain:string)=>{if(nextDomain==='wealth'){setProductSection('products');setProductVisit(v=>v+1)}if(nextDomain==='transfer'){setTransferSection('transfer');setContactVisit(v=>v+1)}setDomain(nextDomain);go('business')}
  const openProducts=(section:'products'|'compare'|'holdings')=>{setProductSection(section);setProductVisit(v=>v+1);setDomain('wealth');go('business')}
  const openContacts=()=>{setTransferSection('contacts');setContactVisit(v=>v+1);setDomain('transfer');go('business')}
  const shortcut=(target:string)=>target==='bill'?go('bills'):target==='billvision'?go('payments'):goBusiness(target)
  const goChat=(prompt?:string)=>{
    if(loading&&prompt)return
    setQuick(false);go('chat')
    // Send after selecting the tab; the controller still owns the live intent mode and validation.
    if(prompt)void dialogue.send(prompt)
  }
  const onBillsRead=async(tool:string,args:Record<string,string>)=>{
    if(loading)return
    const out=await ctrl.read(tool,args)
    if(out?.error?.code==='query_authorization_required')goBusiness('bill')
    else if(out){setQuick(true);go('chat')}
  }
  const resumeFromRecovery=async(task:TaskView)=>{
    const out=await ctrl.taskAction(task,'resume')
    setQuick(true);go('chat');return out
  }
  const returnToRecoveryTask=()=>{setQuick(true);go('chat');void ctrl.sync(true)}
  const authScreen=!active?.valid||loginOpen
  const isDetail=tab!=='chat'
  return <div className="mobile-stage visual-refresh"><main className="mobile-shell" data-reading={tab==='chat'&&!authScreen&&reading}>
    {!online&&<FaultNotice kind="offline"/>}
    {active?.valid&&paidStatus&&!paidStatus.paid_model_enabled&&<FaultNotice kind="model" onSupport={()=>go('support')} detail={serviceReason(paidStatus.reason)}/>}
    {(isDetail&&!authScreen)&&<header className="mobile-app-header">
      <div className="mobile-header-start">
        {isDetail&&!authScreen?<button type="button" className="mobile-icon-button" onClick={back} aria-label="返回"><MobileIcon name="back"/></button>:<span className="mobile-brand-mark" aria-hidden="true">B<span>·</span></span>}
        <div className="mobile-brand-text"><strong>{tab==='business'?domain==='transfer'?'智能转账':domain==='wealth'?'理财产品':TITLES[tab]:TITLES[tab]}</strong></div>
      </div>
      <button type="button" className="mobile-icon-button" aria-label="历史对话" disabled={loading} onClick={()=>setDrawer(true)}><MobileIcon name="menu"/></button>
    </header>}
    {dialogue.error&&<div className="mobile-error" role="alert">{dialogue.error}<button onClick={()=>dialogue.setError('')}>收起</button></div>}
    {ctrl.error&&['network_error','commit_unknown','commit_outcome_unknown'].includes(ctrl.actionError?.code||'')?<FaultNotice kind="unknown" detail={ctrl.error} onRetry={()=>void ctrl.sync()} onTasks={()=>go('tasks')} onSupport={()=>go('support')}/>:ctrl.error&&<div className="mobile-error" role="alert">{ctrl.error}<button onClick={()=>ctrl.setError('')}>收起</button></div>}
    {drawer&&<div className="drawer-shade" onClick={()=>setDrawer(false)}><aside aria-label="会话管理" className="mobile-drawer" onClick={e=>e.stopPropagation()}>
      <div className="drawer-top"><strong>历史对话</strong><button className="mobile-icon-button" aria-label="关闭会话管理" onClick={()=>setDrawer(false)}>×</button></div>
      <p className="muted-copy">对话记录与登录会话分开管理。历史金融数据需要重新查询，退出会撤销当前凭据。</p>
      {active?.valid&&<><button className="mobile-primary full-width" disabled={loading} onClick={()=>{setDrawer(false);setQuick(false);setTab('chat');ctrl.clearReadError();void dialogue.make().catch(e=>dialogue.setError(e.message))}}>新对话</button><h3 className="drawer-section-label">我的对话记录</h3>{dialogue.choices.map(c=><button key={c.id} className={`session-button ${dialogue.cid===c.id?'selected':''}`} onClick={()=>{ctrl.clearReadError();setQuick(false);dialogue.select(c.id);setDrawer(false);setTab('chat')}}><strong>对话 · {new Date(c.created_at).toLocaleString('zh-CN')}</strong><small>打开后读取该对话的近期记录</small></button>)}</>}
      <h3 className="drawer-section-label">登录会话</h3><button className="mobile-primary drawer-login" disabled={loading} onClick={()=>{setDrawer(false);setLoginOpen(true)}}>登录并建立新会话</button>
      {ctrl.sessions.map(s=><button key={s.id} className={`session-button ${s.id===active?.id?'selected':''}`} disabled={loading||!s.valid} onClick={()=>{setDrawer(false);setTab('chat');void ctrl.selectSession(s.id)}}><strong>{s.title}</strong><small>{s.principal.display_name} · {s.valid?'会话可用':'需重新登录'}</small></button>)}
      {active?.valid&&<button className="mobile-secondary full-width" disabled={loading} onClick={async()=>{await ctrl.logout();setDrawer(false);setTab('chat')}}>退出当前会话</button>}
      <button className="mobile-secondary full-width" onClick={()=>setDrawer(false)}>关闭</button>
    </aside></div>}
    {authScreen?<div className="mobile-screen mobile-auth"><div className="auth-hero"><BrandMark size={52}/><h1>你好，欢迎回来</h1><p>让每一笔，都心中有数</p></div><LoginPanel busy={loading} onLogin={async(u,p)=>{const out=await ctrl.login(u,p);if(out){setLoginOpen(false);setTab('chat');setHistory([])}return out}} onClose={active?.valid?()=>{setLoginOpen(false);setTab('chat')}:undefined}/></div>:active ? <>
      {healthStatus==='failed'&&<FaultNotice kind="service" onRetry={checkHealth} onTasks={()=>go('tasks')} onSupport={()=>go('support')}/>}
      {support?.automation_paused&&tab!=='support'&&<div className="support-paused-strip"><span>{support.state==='ended'?'人工服务已结束，AI 等待你恢复':'人工服务处理中，AI 已暂停新办理'}</span><button onClick={()=>go('support')}>查看人工服务</button></div>}
      <div className={`mobile-screen ${tab==='chat'?'mobile-chat-screen':''}`}>
        {(visited.includes('home')||tab==='home')&&<div hidden={tab!=='home'} className="retained-page"><HomeDashboard key={active.id} sid={active.id} name={active.principal.display_name} disabled={false} tasks={ctrl.tasks} onTasks={()=>go('tasks')} onShortcut={value=>value==='billvision'?go('billvision'):value==='payments'?go('payments'):goBusiness(value)} onBills={()=>go('bills')} onChat={goChat}/></div>}
        {(visited.includes('bills')||tab==='bills')&&<div hidden={tab!=='bills'} className="retained-page"><BillsHub key={active.id} sid={active.id} busy={loading} onBusiness={()=>goBusiness('bill')} onShowResult={onBillsRead} onChat={goChat} onPayments={()=>go('payments')}/></div>}
        {tab==='profile'&&<ProfileHub key={active.id} sid={active.id} name={active.principal.display_name} sessionCount={ctrl.sessions.length} taskCount={ctrl.tasks.length} busy={loading} onOpen={p=>p==='sessions'?setDrawer(true):p==='business'?goBusiness('card'):p==='payments'?go('payments'):go(p)} onSupport={()=>go('support')} onLogout={()=>{void ctrl.logout();setTab('chat')}}/>}
        {(visited.includes('chat')||tab==='chat')&&<div hidden={tab!=='chat'} className="retained-chat">
          <div className="assistant-workspace"><ChatPanel key={active.id+":"+dialogue.cid} sid={active.id} voiceScope={dialogue.cid+':'+quick} onReadingChange={setReading} onHistory={()=>setDrawer(true)} onProfile={()=>go('profile')} active={tab==='chat'} paused={!!support?.automation_paused} onSupport={()=>go('support')} name={active.principal.display_name} onShortcut={shortcut} onServices={()=>setServices(true)} onAttach={()=>setAttachments(true)} draft={quick?(active.draft||''):dialogue.draft} setDraft={quick?ctrl.setDraft:dialogue.setDraft} messages={quick?active.messages:dialogue.messages} loading={loading} onSend={(text,accepted)=>{setQuick(false);return dialogue.send(text,accepted)}} mode={ctrl.mode} setMode={ctrl.setMode} automatic
            top={currentTask&&!['completed','cancelled','failed'].includes(currentTask.task.status)&&<section className="chat-task-top"><button className="active-task-strip" aria-expanded={taskOpen} onClick={()=>setTaskOpen(!taskOpen)}><MobileIcon name="tasks" size={18}/><span>当前任务 · 查看进度与核对</span><MobileIcon name="chevron" size={16}/></button>{taskOpen&&<div className="chat-task-content"><CurrentTaskPanel state={currentState} busy={loading} onTasks={()=>go('tasks')} layers={layers}>
            {!paymentSecurity.enabled&&<FactorBindingGuide recovery={recovery} factor={factor} tasks={ctrl.tasks} task={currentTask} busy={loading} onOpenSecurity={()=>go('security')} onSync={()=>quick?ctrl.sync():dialogue.sync().catch(e=>dialogue.setError(e.message))} onResumeTask={resumeFromRecovery}/>}
            {(()=>{const state=currentState;return state?.task?.preview?(paymentSecurity.enabled?<PaymentActionCard key={active.id+state.task.preview.preview_id} sid={active.id} state={state} busy={loading} onConfirm={standaloneTask?ctrl.confirm:dialogue.confirm} onCancel={t=>standaloneTask?ctrl.taskAction(t,'cancel'):dialogue.act(t,'cancel')} onResume={t=>standaloneTask?ctrl.taskAction(t,'resume'):dialogue.act(t,'resume')} onSecurity={()=>go('security')} onSupport={()=>go('support')} onModify={()=>go('tasks')} onRefresh={()=>standaloneTask?ctrl.sync(true):dialogue.sync()}/>:<SecureActionCard key={active.id+state.task.preview.preview_id} sid={active.id} state={state} busy={loading} actionError={quick?ctrl.actionError:dialogue.actionError} onConfirm={standaloneTask?ctrl.confirm:dialogue.confirm} onVerify={standaloneTask?ctrl.verify:dialogue.verify} onCancel={t=>standaloneTask?ctrl.taskAction(t,'cancel'):dialogue.act(t,'cancel')} onResume={t=>standaloneTask?ctrl.taskAction(t,'resume'):dialogue.act(t,'resume')} onSecurity={()=>go('security')}/>):quick&&state?.task&&['waiting','paused','active'].includes(state.task.task.status)?<button disabled={loading} className="primary" onClick={()=>void ctrl.taskAction(state.task!.task,'resume')}>恢复并重新预览</button>:null})()}
          </CurrentTaskPanel><div className="task-state-actions" aria-label="当前任务状态操作"><button className="secondary" disabled={loading} onClick={()=>{if(standaloneTask)void ctrl.sync();else void dialogue.sync().catch(e=>dialogue.setError(e.message))}}>同步当前状态</button><button className="secondary" disabled={loading} onClick={()=>{if(standaloneTask)void ctrl.sync();else void dialogue.sync().catch(e=>dialogue.setError(e.message))}}>核实当前任务</button></div></div>}</section>}
            history={quick?undefined:{hasMore:dialogue.history.hasMore,total:dialogue.history.total,modelWindow:dialogue.history.modelWindow,loading:dialogue.historyBusy,onLoadEarlier:()=>void dialogue.loadEarlier()}}>
            {!quick&&dialogue.conv&&(dialogue.conv.state.items.length>0||dialogue.conv.turns.some(t=>t.calls.some(c=>['explicit_action_required','query_authorization_required'].includes(c.result?.code))))&&<details className="dialogue-details"><summary>查看草稿与待核对操作</summary><DialogueState dialogue={dialogue.conv} busy={loading} onResume={t=>dialogue.act(t,'resume')} onAuthorize={dialogue.authorize} onApplyOperation={dialogue.applyOperation}/></details>}
            {(quick||dialogue.lastTurn||currentTask)&&<details className="conversation-result-tools"><summary>本次结果与状态</summary>{currentTask&&['completed','cancelled','failed'].includes(currentTask.task.status)&&<CurrentTaskPanel state={currentState} busy={loading} onTasks={()=>go('tasks')} layers={layers}>{!paymentSecurity.enabled&&<FactorBindingGuide recovery={recovery} factor={factor} tasks={ctrl.tasks} task={currentTask} busy={loading} onOpenSecurity={()=>go('security')} onSync={()=>quick?ctrl.sync():dialogue.sync().catch(e=>dialogue.setError(e.message))} onResumeTask={resumeFromRecovery}/>}</CurrentTaskPanel>}<p>仅读取当前对话或原任务，不会重新发送业务。</p><button className="secondary" disabled={loading} onClick={()=>{if(quick)void ctrl.sync();else void dialogue.sync().catch(e=>dialogue.setError(e.message))}}>同步当前状态</button><button className="secondary" disabled={quick?ctrl.loading:dialogue.localBusy} onClick={()=>{if(quick)void ctrl.sync();else void dialogue.retry(dialogue.lastTurn?.id,dialogue.lastTurn?.client_message_id)}}>{quick?'核实当前任务':'核实上一轮'}</button></details>}
            {quick&&<button className="mobile-sync" onClick={()=>setQuick(false)}>返回 AI 对话任务</button>}
            {!paymentSecurity.enabled&&!currentTask&&recovery.needed&&<FactorBindingGuide recovery={recovery} factor={factor} tasks={ctrl.tasks} task={currentTask} busy={loading} onOpenSecurity={()=>go('security')} onSync={()=>ctrl.sync()} onResumeTask={resumeFromRecovery}/>}
          </ChatPanel></div>
        </div>}
        {(visited.includes('business')||tab==='business')&&<div hidden={tab!=='business'} className="retained-page">{domain==='transfer'?<SmartTransfer sid={active.id} initialSection={transferSection} navigationVersion={contactVisit} ctrl={ctrl} onPrepared={()=>{setQuick(true);setTaskOpen(true);go('chat')}} onScan={()=>go('payments')}/>:domain==='wealth'?<ProductCatalog sid={active.id} initialSection={productSection} navigationVersion={productVisit} ctrl={ctrl} onPrepared={()=>{setQuick(true);setTaskOpen(true);go('chat')}}/>:<fieldset disabled={loading}><BusinessPanel key={active.id} sid={active.id} initialDomain={domain} ctrl={ctrl} onChat={()=>{setQuick(true);go('chat')}}/></fieldset>}</div>}
        {(visited.includes('billvision')||tab==='billvision')&&<div hidden={tab!=='billvision'} className="retained-page"><BillVisionPanel key={active.id+':'+invoiceVersion} initialDocumentId={invoiceDocument||undefined} sid={active.id} onBusy={setBillBusy} onExpired={()=>{void ctrl.sync()}} onChanged={()=>ctrl.invalidateUploaded(active.id)}/></div>}
        {(visited.includes('payments')||tab==='payments')&&<div hidden={tab!=='payments'} className="retained-page"><PaymentsHub key={active.id} onInvoice={id=>{setInvoiceDocument(id);setInvoiceVersion(v=>v+1);go('billvision')}} sid={active.id} userId={active.principal.user_id} busy={loading} onOpenTasks={()=>go('tasks')} onChat={(prompt)=>{setQuick(false);go('chat');if(prompt)void dialogue.send(prompt)}} onPrepared={()=>{setQuick(true);setTab('chat');ctrl.setMode('action');void ctrl.sync(true)}}/></div>}
        {(visited.includes('tasks')||tab==='tasks')&&<div hidden={tab!=='tasks'} className="retained-page"><TasksPanel key={active.id} sid={active.id} tasks={ctrl.tasks} busy={loading} onAction={async(t,a,v)=>{const out=await ctrl.taskAction(t,a,v);if(out&&['resume','return'].includes(a)){setQuick(true);ctrl.setMode('action');go('chat')}return out}} run={ctrl.run} onSync={ctrl.sync}/>{dialogue.conv&&dialogue.conv.state.items.some(x=>['transfer','query','result'].includes(x.kind))&&<details className="mobile-page current-conversation-drafts"><summary>当前对话的草稿与记录</summary><p className="muted-copy">仅来自当前对话；已关联草稿是任务来源，不是另一笔付款。</p><DialogueState dialogue={dialogue.conv} includeTasks={false} busy={loading} onResume={t=>dialogue.act(t,'resume')} onAuthorize={dialogue.authorize} onApplyOperation={dialogue.applyOperation}/></details>}</div>}
        {tab==='memory'&&<MemoryPanel key={active.id} sid={active.id}/>}
        {tab==='security'&&(paymentSecurity.enabled?<PaymentSecurityCenter sid={active.id} onChanged={()=>{void ctrl.sync(true)}}/>:<FactorPanel key={active.id} sid={active.id} factor={factor} busy={loading} run={ctrl.run} actionError={ctrl.actionError} recoveryTask={recovery.needed?{goal:recovery.goal,taskId:recovery.taskId}:null} onReturnToTask={returnToRecoveryTask} onRefreshFactor={()=>ctrl.sync(true)}/>)}
        {(visited.includes('support')||tab==='support')&&<div hidden={tab!=='support'} className="retained-support"><SupportPage key={active.id} active={tab==='support'} sid={active.id} onTasks={()=>go('tasks')} onResume={()=>{setSupport(null);goChat()}}/></div>}
      </div>
      {services&&<div className="service-shade" onClick={()=>setServices(false)}><section className="services-sheet" role="dialog" aria-modal="true" aria-label="全部服务抽屉" onClick={e=>e.stopPropagation()}><div className="sheet-grip"/><div className="sheet-heading"><h2>全部服务</h2><button className="plain-icon" aria-label="关闭全部服务" onClick={()=>setServices(false)}>×</button></div>{([
        ['账户与交易',[['wallet','查余额','银行卡余额合计',()=>go('profile')],['cards','银行卡管理','限额与挂失',()=>goBusiness('card')],['receipt','查账单','账单与收支',()=>go('bills')],['transfer','智能转账','核对收款对象后办理',()=>goBusiness('transfer')],['camera','识图办理','缴费、记账与对账',()=>go('payments')],['user','收款联系人','管理既有收款档案',openContacts]]],
        ['理财与订阅',[['wallet','选理财','四类模拟产品',()=>openProducts('products')],['cards','产品对比','选择两至三款',()=>openProducts('compare')],['chart','持仓与合同','本金、份额与合同',()=>openProducts('holdings')],['receipt','订阅与代扣','先授权查询，再核对取消',()=>goBusiness('subscription')]]],
        ['办理与帮助',[['tasks','我的待办','任务与核验回执',()=>go('tasks')],['transfer','组合办理','保留条件与步骤依赖',()=>goBusiness('workflow')],['memory','记忆与偏好','管理保存与使用范围',()=>go('memory')],['chart','收支分析','月份与统计口径',()=>go('bills')],['headset','客服与反馈','查看真实接入状态',()=>go('support')]]]
      ] as [string,[MobileIconName,string,string,()=>void][]][]).map(([title,items])=><div className="service-group" key={title}><h3>{title}</h3><div>{items.map(([icon,label,desc,action])=><button key={label} onClick={action}><span className="service-icon"><MobileIcon name={icon} size={23}/></span><strong>{label}</strong><small>{desc}</small><MobileIcon name="chevron" size={16}/></button>)}</div></div>)}</section></div>}
      {attachments&&<div className="service-shade" onClick={()=>setAttachments(false)}><section className="services-sheet attachment-sheet" role="dialog" aria-modal="true" aria-label="添加附件" onClick={e=>e.stopPropagation()}><div className="sheet-heading"><h2>添加图片或文件</h2><button className="plain-icon" aria-label="关闭添加附件" onClick={()=>setAttachments(false)}>×</button></div><button className="attachment-option" onClick={()=>go('payments')}><MobileIcon name="camera" size={28}/><strong>识别缴费单与凭证</strong><small>核对后准备转账，已付款不重复扣款</small></button><button className="attachment-option" onClick={()=>{setInvoiceDocument('');setInvoiceVersion(v=>v+1);go('billvision')}}><MobileIcon name="receipt" size={28}/><strong>识别消费账单</strong><small>支持 JPEG / PNG 图片；导入记账不发起付款</small></button></section></div>}
    </> : null}
  </main></div>
}
