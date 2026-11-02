import type { Binding, Factor, Json, Login, NLResponse, Plan, ReadEnvelope, SecureTask, TaskAction, TaskView, UiState } from '../integrationTypes'
const BASE = (import.meta.env.VITE_API_BASE || '/api').replace(/\/$/, '')
export interface DialogueStreamEvent {type:string;text:string;done:boolean}
/** Only server-reviewed answer snapshots are accepted here, never model deltas. */
export async function readDialogueStream<T>(path:string,sid:string,body:unknown,onEvent:(event:DialogueStreamEvent)=>void,limits={totalMs:270000,idleMs:30000}):Promise<T>{
  if(navigator.onLine===false)throw new ApiError('offline_before_submit','网络已断开，消息尚未发出。连接后可核实并重试。')
  const controller=new AbortController(),deadline=setTimeout(()=>controller.abort(),limits.totalMs)
  let response:Response
  try{response=await fetch(`${BASE}${path}`,{method:'POST',cache:'no-store',signal:controller.signal,headers:{'Content-Type':'application/json',Authorization:`Bearer ${sid}`},body:JSON.stringify(body)})}catch{clearTimeout(deadline);throw new ApiError('network_error','连接中断或等待超时，请先核实原轮次；未自动重发。')}
  if(!response.ok){let timer:ReturnType<typeof setTimeout>|undefined,value:any
    try{value=await Promise.race([response.json().catch(()=>null),new Promise(resolve=>{timer=setTimeout(()=>resolve(null),2000)})])}
    finally{if(timer!==undefined)clearTimeout(timer);clearTimeout(deadline);controller.abort()}
    throw new ApiError(value?.error?.code||value?.code||'request_failed',value?.error?.message||value?.message||'暂时无法接收回复，请读取本条消息状态后再试。',response.status,value?.data??null)
  }
  if(!response.body){clearTimeout(deadline);throw new ApiError('stream_unavailable','当前连接无法接收回复，请核实原轮次。')}
  const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',client='';let last=0
  const read=async()=>{let timer:ReturnType<typeof setTimeout>|undefined
    try{return await Promise.race([reader.read(),new Promise<never>((_,reject)=>{timer=setTimeout(()=>reject(new ApiError('stream_timeout','回复等待超时，结果待核实；请读取原轮次，未重新提交业务。')),limits.idleMs)})])}
    finally{if(timer!==undefined)clearTimeout(timer)}
  }
  try{while(true){const part=await read();buffer+=decoder.decode(part.value,{stream:!part.done});if(buffer.length>2_000_000)throw new Error('stream_limit');let end:number
    while((end=buffer.indexOf('\n\n'))>=0){const frame=buffer.slice(0,end);buffer=buffer.slice(end+2);const data=frame.split('\n').filter(s=>s.startsWith('data:')).map(s=>s.slice(5)).join('\n');if(!data)continue
      const event=JSON.parse(data)
      if(event.client_message_id!==((body as any).client_message_id))throw new Error('stream_binding')
      if(client&&client!==event.client_message_id)throw new Error('stream_binding');client=event.client_message_id
      if(event.type==='answer'){if(event.sequence<=last)continue;if(event.sequence!==last+1||typeof event.text!=='string'||event.publication!=='reviewed_segments')throw new Error('stream_sequence');last=event.sequence;onEvent(event)}
      if(event.type==='complete'){
        const value=event.dialogue,target=decodeURIComponent(path.match(/\/dialogues\/([^/]+)\//)?.[1]||'')
        const turns=value?.turns?.filter((t:any)=>t.client_message_id===(body as any).client_message_id)
        if(event.publication!=='reviewed_segments'||value?.id!==target||!Array.isArray(turns)||turns.length!==1)throw new Error('stream_terminal_binding')
        if(!['completed','failed','cancelled','interrupted'].includes(turns[0].status))throw new ApiError('stream_pending','原轮次尚未结束，请读取状态核实，未重新提交。')
        // The bound terminal envelope is the protocol boundary. A proxy may
        // keep the HTTP body open after it; waiting for EOF would hold busy.
        return value as T
      }
      if(event.type==='error')throw new ApiError(event.code||'stream_failed',event.message||'回复连接未完成，请核实原轮次。',typeof event.status==='number'?event.status:0,event.data??null)
    }
    if(part.done)break
  }}catch(e){if(e instanceof ApiError)throw e;throw new ApiError('stream_interrupted','回复接收中断，已显示部分保留；请核实原轮次。')}finally{clearTimeout(deadline);controller.abort();void reader.cancel().catch(()=>{});reader.releaseLock()}
  throw new ApiError('stream_interrupted','回复接收中断，已显示部分保留；请核实原轮次。')
}
export class ApiError extends Error {
  constructor(public code: string, message: string, public status = 0, public data: unknown = null) { super(message) }
}
export async function request<T>(path: string, sid?: string, method = 'GET', body?: unknown): Promise<T> {
  if(navigator.onLine===false)throw new ApiError('offline_before_submit','网络已断开，本次请求未发出。输入和表单已保留，连接后可继续。')
  const read=method==='GET'||path.endsWith('/read')||path.endsWith('/state')||path.endsWith('/capabilities')
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),read?30000:270000)
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, { method, cache: 'no-store',signal:controller.signal,
      headers: { 'Content-Type': body instanceof Blob ? 'application/octet-stream' : 'application/json', ...(sid ? { Authorization: `Bearer ${sid}` } : {}) },
      body: body instanceof Blob ? body : body === undefined ? undefined : JSON.stringify(body) })
  } catch {
    clearTimeout(timer)
    throw new ApiError(read?'service_unreachable':'network_error',read?'暂时无法连接服务。已显示的数据可能是旧快照，请重新查询。':'结果确认中：连接中断，尚未确认服务是否收到本次操作。请查询原任务，勿重新付款；系统不会自动重提。')
  }
  let value: unknown
  try { value = await response.json() } catch { throw new ApiError(read?'invalid_response':'commit_outcome_unknown', '服务结果未能完整读取，请核对原任务或轮次；未自动重提。', response.status) }
  finally{clearTimeout(timer)}
  if (!response.ok) {
    const p = value as { code?: string; message?: string; data?: unknown; error?: {code: string; message: string} }
    const code = p.error?.code || p.code || 'request_failed'
    const message = p.error?.message || p.message
    throw new ApiError(code, !message || message === code
      ? '操作未完成，请同步任务核对当前状态；已提交业务以核验回执为准。' : message, response.status, p.data ?? null)
  }
  return value as T
}
const secure = (sid: string) => `/secure/sessions/${encodeURIComponent(sid)}`
const v2 = (sid: string) => `/v2/sessions/${encodeURIComponent(sid)}`
const taskPath = (sid: string, tid: string) => `${secure(sid)}/tasks/${encodeURIComponent(tid)}`
export const realApi = {
  login: (username: string, password: string) => request<Login>('/v2/auth/login', undefined, 'POST', { username, password }),
  logout: (sid: string) => request(`/v2/auth/logout?session_id=${encodeURIComponent(sid)}`, sid, 'POST'),
  health: () => request<{ backend_version: string; llm_configured: boolean }>('/v1/health'),
  state: (sid: string) => request<UiState>(`${secure(sid)}/ui-state`, sid),
  chat: (sid: string, message: string, intent_mode: 'analysis' | 'action') =>
    request<NLResponse>(`/v1/chat?session_id=${encodeURIComponent(sid)}`, sid, 'POST', { message, intent_mode }),
  read: (sid: string, tool: string, args: Record<string, Json>, authorize_query = false) =>
    request<ReadEnvelope>(`${v2(sid)}/read`, sid, 'POST', { tool, arguments: args, authorize_query }),
  analysis: (sid: string, tool: string, args: Record<string, Json>) =>
    request<{ business: { message: string }; analyses: { result: Json }[]; error: { code: string; message: string } | null }>(
      `/analysis/sessions/${encodeURIComponent(sid)}/read`, sid, 'POST', { tool, arguments: args }),
  createTask: (sid: string, plan: Plan, binding?:import('../components/phoneResolution').PhoneBinding) => request<SecureTask>(binding?`${secure(sid)}/mobile/phone-transfer`:`${secure(sid)}/tasks`, sid, 'POST', binding?{plan,binding}:{plan}),
  tasks: (sid: string) => request<{ tasks: TaskView[] }>(`${secure(sid)}/tasks`, sid),
  task: (sid: string, tid: string) => request<SecureTask>(taskPath(sid, tid), sid),
  act: (sid: string, task: TaskView, action: TaskAction, values: Record<string, unknown> = {}) =>
    request<SecureTask>(`${taskPath(sid, task.task.task_id)}/${action}`, sid, 'POST', { ...values, revision: task.revision }),
  confirm: (binding: Binding, text: string) => request<SecureTask>(
    `${secure(binding.sessionId)}/ui/tasks/${encodeURIComponent(binding.taskId)}/confirm`, binding.sessionId, 'POST',
    { revision: binding.revision, preview_id: binding.previewId, text }),
  verify: (binding: Binding, code: string, method: 'totp' | 'mock_otp') => {
    if (!binding.challengeId) throw new ApiError('challenge_not_found', '当前操作没有验证挑战。')
    if (method === 'totp') return request(`${secure(binding.sessionId)}/challenges/${encodeURIComponent(binding.challengeId)}/verify`,
      binding.sessionId, 'POST', { code, task_id: binding.taskId, revision: binding.revision, preview_id: binding.previewId })
    return request<ReadEnvelope>(`${v2(binding.sessionId)}/challenges/${encodeURIComponent(binding.challengeId)}/verify`,
      binding.sessionId, 'POST', { code })
  },
  factor: (sid: string) => request<Factor>(`${secure(sid)}/factor`, sid),
  enroll: (sid: string, password: string) => request<{ otpauth_uri: string; expires_at: string }>(`${secure(sid)}/factor/enroll`, sid, 'POST', { password }),
  confirmFactor: (sid: string, code: string) => request<Factor>(`${secure(sid)}/factor/confirm`, sid, 'POST', { code }),
  audit: (sid: string, tid: string) => request<{ records: Json[]; truncated: boolean }>(`${taskPath(sid, tid)}/audit`, sid),
}
