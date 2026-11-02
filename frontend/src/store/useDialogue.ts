import {useCallback,useEffect,useRef,useState} from 'react'
import {request,readDialogueStream,realApi,ApiError} from '../api/client'
import type {Session,ChatMessage,SubscriptionQuery} from './useChat'
import type {Binding,Json,TaskView,UiState,DisplayResult} from '../integrationTypes'
import {bindingOf,sameBinding} from '../integrationTypes'
import type {ActionError} from '../components/secureErrors'
import {originalTurn} from '../components/conversationRecovery'

export interface DialogueTurn {id:string;client_message_id:string;message:string;answer:string;answer_view?:{body:string;notice?:string};can_rephrase?:boolean;status:string;recoverable?:boolean;error_code:string|null;progress?:string|null;number?:number;calls:Record<string,any>[];memory_operations?:Record<string,any>[];query_authorizations?:SubscriptionQuery[];authorization_answer_pending?:boolean;authorization_answer_notice?:string;query_status?:string}
export interface DialoguePage {has_more?:boolean;next_before?:number|null;total?:number|null;returned?:number;oldest_number?:number|null;model_window_turns?:number;limit?:number;window?:number}
export interface Dialogue {id:string;turns:DialogueTurn[];state:{items:Record<string,any>[]};history?:DialoguePage;has_more?:boolean;next_before?:number|null;total_turns?:number|null;model_window_turns?:number}
interface Pending {client_message_id:string;message:string;state:'sending'|'uncertain'|'rejected';reason?:string}
const selectedKey=(p:string)=>'banking-r3-dialogue:'+p
const pendingKey=(p:string,id:string)=>selectedKey(p)+':pending:'+id
function readPending(p:string,id:string):Pending|null {try{const v=JSON.parse(sessionStorage.getItem(pendingKey(p,id))||'null');return v&&typeof v.client_message_id==='string'&&typeof v.message==='string'?{...v,state:v.state==='rejected'?'rejected':'uncertain'}:null}catch{return null}}
const names:Record<string,string>={cards_balance:'银行卡余额',recipient_candidates:'收款人',query_execute:'账单查询',query_data:'账单查询',query_uploaded_bills:'已确认上传账单',get_monthly_spending:'月度支出',get_transactions_by_month:'交易明细',get_category_spending:'支出分类',query_financial_records:'商户账单',get_monthly_cash_flow_report:'月度收支报告',get_annual_cash_flow_report:'年度收支报告',get_expense_clues:'支出核查线索',get_risk_profile:'模拟风险等级',get_wealth_products:'模拟理财产品',get_wealth_holdings:'模拟持仓',compare_wealth_products:'产品对比',explain_wealth_suitability:'适当性说明',call_result_read:'历史查询结果'}
function displays(c:Record<string,any>):DisplayResult[]{const r=c.effective_result||c.result,d=r?.data;if(!d||r.status!=='completed'||!names[c.name])return [];const data=d.cards||d.recipients||d.fields?.result||d.data||d.result||null;return data===null?[]:[{title:names[c.name],step_id:null,business:null,detail:data as Json}]}
function pageOf(v:Dialogue):DialoguePage{return {has_more:v.has_more,next_before:v.next_before,total:v.total_turns,model_window_turns:v.model_window_turns,...v.history}}
export function useDialogue(session:Session|null,enabled:boolean){
  const sid=session?.valid?session.id:'',principal=session?.principal.principal_id||''
  const [conv,setConv]=useState<Dialogue|null>(null),[ui,setUi]=useState<UiState|null>(null),[cid,setCid]=useState('')
  const [choices,setChoices]=useState<{id:string;created_at:string}[]>([]),[localBusy,setBusy]=useState(false),[error,setError]=useState('')
  const [actionError,setActionError]=useState<ActionError|null>(null),[historyBusy,setHistoryBusy]=useState(false),[pending,setPending]=useState<Pending|null>(null)
  const [draft,setDraftValue]=useState(''),[stream,setStream]=useState<{client:string;text:string;done:boolean;interrupted?:boolean}|null>(null)
  const generation=useRef(0),syncSeq=useRef(0),older=useRef<Set<string>>(new Set()),lock=useRef<symbol|null>(null),draftRef=useRef(draft)
  const creation=useRef<{sid:string;epoch:number;promise:Promise<string>}|null>(null)
  const syncFlight=useRef<{key:string;promise:Promise<Dialogue>}|null>(null),uiFlight=useRef<{key:string;promise:Promise<void>}|null>(null)
  const authorizationFlights=useRef(new Set<string>())
  const authorizationNextTry=useRef(new Map<string,number>())
  const renderedOwner=useRef(sid),visible=renderedOwner.current===sid
  draftRef.current=draft
  const current=useRef({sid,cid,principal});current.current={sid,cid,principal}
  const base=sid?`/secure/sessions/${encodeURIComponent(sid)}/dialogues`:''
  const busy=localBusy||!!conv?.turns.some(t=>t.status==='running'&&!t.recoverable)
  const owns=(epoch:number,id:string,owner=sid)=>epoch===generation.current&&current.current.sid===owner&&current.current.cid===id
  const savePending=(id:string,value:Pending|null)=>{if(value)sessionStorage.setItem(pendingKey(principal,id),JSON.stringify(value));else sessionStorage.removeItem(pendingKey(principal,id));if(current.current.sid===sid&&current.current.cid===id)setPending(value)}
  const setDraft=(value:string)=>{draftRef.current=value;setDraftValue(value);sessionStorage.setItem(selectedKey(principal)+':draft:'+current.current.cid,value)}
  const handleError=useCallback((e:unknown)=>{const message=e instanceof Error?e.message:'请求未完成，请核实原轮次。';setError(message);setActionError({code:e instanceof ApiError?e.code:'unknown',message})},[])
  const merge=(value:Dialogue)=>setConv(previous=>{
    if(!previous||previous.id!==value.id)return value
    const ids=new Set(value.turns.map(t=>t.id)),kept=previous.turns.filter(t=>older.current.has(t.id)&&!ids.has(t.id))
    return kept.length?{...value,turns:[...kept,...value.turns].sort((a,b)=>(a.number??0)-(b.number??0)),history:{...pageOf(value),...previous.history,total:Math.max(pageOf(value).total??0,previous.history?.total??0)}}:value
  })
  const sync=useCallback((id=current.current.cid):Promise<Dialogue|undefined>=>{
    if(!sid||!id)return Promise.resolve(undefined)
    const epoch=generation.current,seq=syncSeq.current,key=`${sid}:${id}:${epoch}:${seq}`
    if(uiFlight.current?.key!==key){
      const promise=request<UiState>(`${base}/${id}/ui-state`,sid).then(state=>{if(owns(epoch,id)&&seq===syncSeq.current)setUi(state)}).catch(()=>{/* Dialogue status can still finish; keep the last task for explicit verification. */}).finally(()=>{if(uiFlight.current?.promise===promise)uiFlight.current=null})
      uiFlight.current={key,promise}
    }
    if(syncFlight.current?.key===key)return syncFlight.current.promise
    const promise=request<Dialogue>(`${base}/${id}`,sid).then(value=>{
      if(!owns(epoch,id)||seq!==syncSeq.current)return value
      merge(value)
      const saved=readPending(principal,id),turn=saved&&value.turns.find(t=>t.client_message_id===saved.client_message_id)
      if(turn&&turn.status!=='running'){savePending(id,null);if(!lock.current)setStream(null)}
      if(!lock.current)setStream(previous=>previous&&value.turns.some(t=>t.client_message_id===previous.client&&t.status!=='running')?null:previous)
      return value
    }).finally(()=>{if(syncFlight.current?.promise===promise)syncFlight.current=null})
    syncFlight.current={key,promise};return promise
  },[sid,base,principal])
  const select=(id:string)=>{generation.current++;syncSeq.current++;lock.current=null;setBusy(false);setHistoryBusy(false);older.current=new Set();current.current={sid,cid:id,principal};setConv(null);setUi(null);setStream(null);setError('');setActionError(null);setPending(readPending(principal,id));setDraftValue(sessionStorage.getItem(selectedKey(principal)+':draft:'+id)||'');setCid(id);sessionStorage.setItem(selectedKey(principal),id)}
  useEffect(()=>{
    renderedOwner.current=sid
    generation.current++;syncSeq.current++;lock.current=null;setBusy(false);setHistoryBusy(false);setConv(null);setUi(null);setCid('');setChoices([]);setError('');setActionError(null);setStream(null);setPending(null);setDraftValue('');older.current=new Set()
    current.current={sid,cid:'',principal};draftRef.current=''
    if(!enabled||!sid)return
    // A draft/outbox typed before history discovery belongs to a new question,
    // not whichever old dialogue happens to arrive last. Keep it owner-scoped.
    const unbound=sessionStorage.getItem(selectedKey(principal)+':draft:')||''
    draftRef.current=unbound;setDraftValue(unbound);setPending(readPending(principal,''))
    let live=true
    void request<{dialogues:{id:string;created_at:string}[]}>(base,sid).then(v=>{
      if(!live||current.current.sid!==sid)return
      const ids=new Set(v.dialogues.map(d=>d.id)),selected=current.current.cid
      setChoices(previous=>[...previous.filter(d=>d.id===selected&&!ids.has(d.id)),...v.dialogues])
      if(selected||lock.current||draftRef.current.trim()||readPending(principal,''))return
      const saved=sessionStorage.getItem(selectedKey(principal));select(v.dialogues.find(d=>d.id===saved)?.id||v.dialogues[0]?.id||'')
    }).catch(e=>{if(live)handleError(e)})
    return()=>{live=false;generation.current++}
  },[enabled,sid,principal,base,handleError])
  useEffect(()=>{if(!enabled||!cid)return;const epoch=generation.current;void sync().catch(e=>{if(owns(epoch,cid))handleError(e)})},[enabled,cid,sync,handleError])
  useEffect(()=>{if(!enabled||!cid||!sid)return;const epoch=generation.current;const timer=setInterval(()=>void sync().catch(e=>{if(owns(epoch,cid))handleError(e)}),busy?1500:8000);return()=>clearInterval(timer)},[enabled,cid,sid,busy,sync,handleError])
  const make=()=>{
    const epoch=generation.current,id=current.current.cid
    if(creation.current?.sid===sid&&creation.current.epoch===epoch)return creation.current.promise
    const promise=request<{id:string}>(base,sid,'POST',{}).then(value=>{if(owns(epoch,id)){setChoices(v=>[{id:value.id,created_at:new Date().toISOString()},...v]);select(value.id)}return value.id}).finally(()=>{if(creation.current?.promise===promise)creation.current=null})
    creation.current={sid,epoch,promise};return promise
  }
  const run=async<T,>(_sid:string|null,fn:()=>Promise<T>,quiet=false):Promise<T|null>=>{
    if(lock.current)return null
    const marker=Symbol(),epoch=generation.current,id=current.current.cid;lock.current=marker;setBusy(true);if(!quiet){setError('');setActionError(null)}
    syncSeq.current++
    try{const value=await fn();if(owns(epoch,id))await sync(id);return value}catch(e){if(owns(epoch,id)&&!quiet)handleError(e);return null}finally{if(lock.current===marker){lock.current=null;setBusy(false)}}
  }
  const post=async(id:string,value:Pending,epoch:number)=>{
    if(owns(epoch,id))setStream({client:value.client_message_id,text:'',done:false})
    try{
      const result=await readDialogueStream<Dialogue>(`${base}/${id}/messages/stream`,sid,{client_message_id:value.client_message_id,message:value.message},event=>{
        if(!owns(epoch,id))return
        if(event.type==='answer')setStream({client:value.client_message_id,text:event.text,done:event.done})
      })
      if(result.id!==id)throw new ApiError('stream_binding','回复所属会话不一致，请核实原轮次。')
      if(owns(epoch,id)){syncSeq.current++;merge(result);setStream(null);setError('');void sync(id).catch(e=>{if(owns(epoch,id))handleError(e)})}
      // A stale response may acknowledge only its own outbox, never a later send.
      const saved=readPending(principal,id);if(saved?.client_message_id===value.client_message_id&&result.turns.some(t=>t.client_message_id===value.client_message_id&&t.status!=='running'))savePending(id,null)
      return result
    }catch(e){
      const saved=readPending(principal,id),data=e instanceof ApiError?e.data as {request_state?:string;dialogue_id?:string;client_message_id?:string}|null:null
      const rejected=e instanceof ApiError&&data?.request_state==='not_accepted'&&data.dialogue_id===id&&data.client_message_id===value.client_message_id
      if(saved?.client_message_id===value.client_message_id)savePending(id,{...value,state:rejected?'rejected':'uncertain',reason:e instanceof Error?e.message:'回复未完成，请读取本条消息状态。'})
      if(owns(epoch,id)){setStream(v=>!rejected&&v?.text?{...v,done:false,interrupted:true}:null);setError('');setActionError(null)}
      return null
    }
  }
  const send=async(text:string,onAccepted?:()=>void)=>{
    if(lock.current||busy)return null
    if(/otpauth:\/\/|\bBearer\s|\bs[12]_|(?:验证码|校验码|动态码|OTP|TOTP)\s*(?:是|为|[:：=])?\s*\d{6}/i.test(text)){handleError(new Error('验证码和凭据请在独立安全入口输入。'));return null}
    let id=current.current.cid,epoch=generation.current
    if(readPending(principal,id)){return null} // The message's own card owns recovery, without a duplicate banner.
    const marker=Symbol();lock.current=marker;setBusy(true);setError('');setActionError(null)
    syncSeq.current++
    const value:Pending={client_message_id:crypto.randomUUID(),message:text,state:'sending'};savePending(id,value);onAccepted?.()
    try{
      if(!id){const created=await request<{id:string}>(base,sid,'POST',{});if(!owns(epoch,''))return null;sessionStorage.removeItem(pendingKey(principal,''));id=created.id;current.current={sid,cid:id,principal};setCid(id);sessionStorage.setItem(selectedKey(principal),id);sessionStorage.setItem(selectedKey(principal)+':draft:'+id,draftRef.current);sessionStorage.removeItem(selectedKey(principal)+':draft:');setChoices(v=>[{id,created_at:new Date().toISOString()},...v]);savePending(id,value)}
      return await post(id,value,epoch)
    }catch(e){if(owns(epoch,id)){savePending(id,{...value,state:'uncertain',reason:e instanceof Error?e.message:'发送未确认'});handleError(e)}return null}
    finally{if(lock.current===marker){lock.current=null;setBusy(false)}}
  }
  // Inspection never resumes a model or resubmits a business operation.
  const retry=async(turnId?:string,clientId?:string)=>run(sid,async()=>{
    const id=current.current.cid,epoch=generation.current
    if(!id){setError('原消息仍待核实，未自动重新提交。');return}
    let value=await request<Dialogue>(`${base}/${id}`,sid)
    if(!owns(epoch,id))return
    const saved=readPending(principal,id)
    let turn=originalTurn(value.turns,{turnId,clientId},saved?.client_message_id)
    const known=conv?.turns.find(t=>t.id===turnId&&(!clientId||t.client_message_id===clientId))
    if(!turn&&known?.number){
      const page=await request<Dialogue>(`${base}/${id}?history_limit=1&before=${known.number+1}`,sid)
      if(!owns(epoch,id))return
      turn=originalTurn(page.turns,{turnId,clientId})
      if(turn)value={...value,turns:[turn,...value.turns]}
    }
    merge(value);setStream(null)
    if(!turn){
      if(saved&&!turnId&&(!clientId||clientId===saved.client_message_id)){savePending(id,{...saved,reason:'暂未查到本条消息的服务端记录，未自动重发；可取回草稿或按原身份继续恢复。'});setError('')}
      else setError('未找到所选原轮次；未改为恢复最新任务，也未重新提交。')
      return value
    }
    if(saved?.client_message_id===turn.client_message_id&&turn.status!=='running')savePending(id,null)
    return value
  })
  const resume=async(turnId?:string,clientId?:string)=>run(sid,async()=>{
    const id=current.current.cid,epoch=generation.current
    if(!id){const saved=readPending(principal,'');if(!saved)return;const created=await request<{id:string}>(base,sid,'POST',{});if(!owns(epoch,''))return;const next=created.id;current.current={sid,cid:next,principal};setCid(next);sessionStorage.setItem(selectedKey(principal),next);setChoices(v=>[{id:next,created_at:new Date().toISOString()},...v]);sessionStorage.removeItem(pendingKey(principal,''));savePending(next,saved);return post(next,saved,epoch)}
    let value=await request<Dialogue>(`${base}/${id}`,sid)
    if(!owns(epoch,id))return
    merge(value);setStream(null)
     const saved=readPending(principal,id),original=originalTurn(value.turns,{turnId,clientId},saved?.client_message_id)
     if(saved&&!original&&(!clientId||clientId===saved.client_message_id)&&!turnId)return post(id,saved,epoch) // Explicit recovery, same ID/body.
     if((turnId||clientId)&&!original){setError('未找到所选原轮次，未恢复其他任务。');return value}
    if(saved&&original?.client_message_id===saved.client_message_id&&original.status!=='running')savePending(id,null)
     const last=original
    if(last?.status==='running'&&!last.recoverable)return value
    // Checking a known answer failure does not pay for another generation.
    if(last?.error_code&&['answer_semantically_unverified','answer_contract_invalid','semantic_review_unavailable','semantic_review_protocol_invalid','semantic_review_budget_exhausted'].includes(last.error_code))return value
    if(last&&(['interrupted','failed'].includes(last.status)||last.recoverable)){
      value=await request<Dialogue>(`${base}/${id}/turns/${last.id}/resume`,sid,'POST',{})
      if(owns(epoch,id))merge(value)
    }
    return value
  })
  const editPending=async()=>{
    const id=current.current.cid,epoch=generation.current,saved=readPending(principal,id);if(!saved)return
    if(draft.trim()){setError('下一条草稿已保留；请先处理该草稿，再编辑失败消息。');return}
    try{if(id){const value=await request<Dialogue>(`${base}/${id}`,sid);if(!owns(epoch,id))return;merge(value);if(value.turns.some(t=>t.client_message_id===saved.client_message_id)){setError('服务端已收到原轮次，请先核实结果；不能把它当作未发送消息修改重提。');return}}if(draftRef.current.trim()){setError('已保留你新输入的草稿，未用失败消息覆盖。');return}setDraft(saved.message);savePending(id,null)}catch(e){if(owns(epoch,id))handleError(e)}
  }
  const check=async(b:Binding)=>{const state=await request<UiState>(`${base}/${cid}/ui-state`,sid);if(current.current.sid!==b.sessionId||current.current.cid!==cid||!sameBinding(b,bindingOf(sid,state.task)))throw new ApiError('stale_preview','操作已变化，请使用最新预览。')}
  const confirm=(b:Binding,text:string)=>run(sid,async()=>{await check(b);return request(`${base}/${cid}/tasks/${b.taskId}/confirm`,sid,'POST',{revision:b.revision,preview_id:b.previewId,text})})
  const verify=(b:Binding,code:string,method:'totp'|'mock_otp')=>run(sid,async()=>{await check(b);return realApi.verify(b,code,method)})
  const act=(task:TaskView,action:'resume'|'pause'|'cancel')=>run(sid,()=>request(`${base}/${cid}/tasks/${task.task.task_id}/action`,sid,'POST',{revision:task.revision,action}))
  const authorize=(tid:string,callId:string,queries?:SubscriptionQuery[])=>run(sid,async()=>{
    const id=cid,epoch=generation.current
    const value=await request<Dialogue>(`${base}/${id}/turns/${tid}/reads/${encodeURIComponent(callId)}/authorize`,sid,'POST',queries?{queries:queries.map(q=>({call_id:q.call_id,scope_id:q.scope_id}))}:undefined)
    if(owns(epoch,id)){syncSeq.current++;merge(value)}
    return value
  })
  // Lost authorization replies and busy original turns recover by the same
  // server evidence revision. This endpoint only expresses saved evidence.
  useEffect(()=>{
    if(!enabled||busy||!sid||!cid)return
    const turn=conv?.turns.find(t=>t.authorization_answer_pending)
    if(!turn)return
    const id=cid,epoch=generation.current,key=`${sid}:${id}:${turn.id}`
    if(authorizationFlights.current.has(key)||(authorizationNextTry.current.get(key)||0)>Date.now())return
    authorizationFlights.current.add(key)
    authorizationNextTry.current.set(key,Date.now()+8000)
    void run(sid,async()=>{const value=await request<Dialogue>(`${base}/${id}/turns/${turn.id}/authorized-answer`,sid,'POST',{});if(owns(epoch,id)){syncSeq.current++;merge(value)}return value},true).finally(()=>authorizationFlights.current.delete(key))
  },[enabled,busy,sid,cid,conv])
  const applyOperation=(tid:string,callId:string)=>run(sid,()=>request(`${base}/${cid}/turns/${tid}/operations/${encodeURIComponent(callId)}/apply`,sid,'POST',{}))
  const rephrase=(tid:string)=>run(sid,async()=>{
    const id=current.current.cid,epoch=generation.current,key=`${selectedKey(principal)}:answer:${id}:${tid}`
    const requestId=sessionStorage.getItem(key)||crypto.randomUUID();sessionStorage.setItem(key,requestId)
    const value=await request<Dialogue>(`${base}/${id}/turns/${tid}/answer`,sid,'POST',{request_id:requestId})
    if(owns(epoch,id)){syncSeq.current++;merge(value)}
    return value
  })
  const page=conv?pageOf(conv):{},hasMore=!!page.has_more&&page.next_before!=null
  const loadEarlier=async()=>{if(!cid||!conv||historyBusy||!hasMore)return;const epoch=generation.current;setHistoryBusy(true)
    try{const value=await request<Dialogue>(`${base}/${cid}?history_limit=50&before=${encodeURIComponent(String(page.next_before))}`,sid);if(!owns(epoch,cid))return;value.turns.forEach(t=>older.current.add(t.id));setConv(previous=>{if(!previous||previous.id!==cid)return previous;const known=new Set(previous.turns.map(t=>t.id));return {...previous,turns:[...value.turns.filter(t=>!known.has(t.id)),...previous.turns],history:pageOf(value)}})}catch(e){if(owns(epoch,cid))handleError(e)}finally{if(owns(epoch,cid))setHistoryBusy(false)}}
  const messages:ChatMessage[]=(conv?.turns||[]).flatMap(t=>{
    const key=t.client_message_id||t.id,live=stream?.client===key?stream:null,waiting=live?!live.text&&!live.interrupted:!t.answer&&t.status==='running'&&!t.recoverable
    return [{id:key+':user',role:'user',text:t.message,historical:older.current.has(t.id),canInspect:!!t.error_code||!!t.recoverable,delivery:pending?.client_message_id===key&&pending.state==='uncertain'?'uncertain':undefined,onRetry:()=>void retry(t.id,key),onResume:(t.recoverable||t.status==='interrupted')&&t.id===conv?.turns[conv.turns.length-1]?.id?()=>void resume(t.id,key):undefined,onEdit:()=>void editPending()},
      {id:key+':assistant',role:'agent',text:live?(live.text||(live.interrupted?'回复连接中断，请核实上一轮。':'正在处理…')):t.answer_view?.body??(t.answer||(waiting?'正在处理…':'本轮未完成回复，请核实上一轮。')),answerNotice:!live?t.answer_view?.notice:undefined,onRephrase:t.can_rephrase?()=>void rephrase(t.id):undefined,pending:waiting,streaming:!!live&&!live.done&&!live.interrupted,interrupted:live?.interrupted,animate:!!live,error:!!t.error_code&&!live,historical:older.current.has(t.id),results:t.calls.flatMap(displays),queryStatus:t.query_status,queryAuthorization:t.query_authorizations?.length?{queries:t.query_authorizations,busy,answerPending:!!t.authorization_answer_pending,answerNotice:t.authorization_answer_notice,collapsed:t.id!==conv?.turns[conv.turns.length-1]?.id,onAuthorize:queries=>authorize(t.id,queries[0].call_id,queries)}:undefined}]
  })
  if(pending&&!conv?.turns.some(t=>t.client_message_id===pending.client_message_id)){
    messages.push({id:pending.client_message_id+':user',role:'user',text:pending.message,delivery:pending.state==='rejected'?'rejected':pending.state==='uncertain'?'uncertain':'sending',deliveryMessage:pending.reason,onRetry:()=>void retry(undefined,pending.client_message_id),onResume:()=>void resume(undefined,pending.client_message_id),onEdit:()=>void editPending()})
    if(stream?.client===pending.client_message_id&&stream.text)messages.push({id:pending.client_message_id+':assistant',role:'agent',text:stream.text,streaming:!stream.done&&!stream.interrupted,interrupted:stream.interrupted,animate:true})
    else if(pending.state==='sending')messages.push({id:pending.client_message_id+':assistant',role:'agent',text:'正在处理…',pending:true})
  }
  const lastTurn=conv?.turns[conv.turns.length-1]||null
  return {cid:visible?cid:'',conv:visible?conv:null,ui:visible?ui:null,messages:visible?messages:[],choices:visible?choices:[],busy:visible?busy:true,localBusy:visible?localBusy:true,error:visible?error:'',setError,actionError:visible?actionError:null,setActionError,draft:visible?draft:'',setDraft,historyBusy,history:{hasMore:visible&&hasMore,nextBefore:visible?page.next_before??null:null,total:visible?page.total??null:null,modelWindow:page.model_window_turns??null},loadEarlier,lastTurn:visible?lastTurn:null,sync,send,confirm,verify,act,authorize,applyOperation,run,make,select,retry,resume}
}
