import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import {request} from '../api/client'
import type { Binding, DisplayResult, Json, Login, Plan, TaskAction, TaskView, UiState } from '../integrationTypes'
import { bindingOf, sameBinding, timeline } from '../integrationTypes'
import type { ActionError } from '../components/secureErrors'
import type { UiTask } from '../types'

export interface SubscriptionQuery {call_id:string;scope_id:string;label:string;scope:string;state:'pending'|'authorized'|'failed'|'expired'|'revoked';result:import('../types').UiResult|null;next_step:string}
export interface ChatMessage {
  id: string; role: 'user' | 'agent'; text: string
  results?: DisplayResult[]; task?: UiTask; error?: boolean
  source?: 'uploaded' | 'bank' | null
  historical?: boolean
  pending?: boolean; streaming?: boolean; interrupted?:boolean; animate?: boolean
  delivery?: 'sending'|'uncertain'|'rejected'; deliveryMessage?:string
  onRetry?: () => void; onEdit?: () => void
  onResume?: () => void; onRephrase?:()=>void; answerNotice?:string; canInspect?:boolean
  queryAuthorization?:{queries:SubscriptionQuery[];busy:boolean;collapsed:boolean;answerPending:boolean;answerNotice?:string;onAuthorize:(queries:SubscriptionQuery[])=>unknown}
  queryStatus?:string
}
export interface Session {
  id: string; title: string; principal: Login['principal']; expiresAt: string
  draft?: string; selectedTaskId?: string; createdAt: number; messages: ChatMessage[]; state: UiState | null; valid: boolean
}
const KEY = 'banking-agent-ui-1.0-sessions'
const uid = () => crypto.randomUUID()
function restore(): Session[] {
  try {
    const value = JSON.parse(sessionStorage.getItem(KEY) || '[]') as Session[]
    return Array.isArray(value) ? value.filter(s => typeof s.id === 'string' && s.id.startsWith('s2_') &&
      s.principal && typeof s.principal.user_id === 'number' && Date.parse(s.expiresAt) > Date.now())
      .slice(0, 8).map(s => ({ ...s, messages: [], state: null, valid: true })) : []
  } catch { return [] }
}
export function useChat(bootstrapDialogue=false) {
  const [sessions, setSessions] = useState<Session[]>(restore)
  const [activeId, setActiveId] = useState<string | null>(() => sessions[0]?.id ?? null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const errorSource=useRef<'read'|'operation'>('operation')
  const uncertainErrors=useRef(new Map<string,ActionError>())
  // Structured error code for the latest user-triggered action. The UI uses it to
  // explain secure-action failures (replayed/expired/unbound) without guessing.
  const [actionError, setActionError] = useState<ActionError | null>(null)
  const [tasks, setTasks] = useState<TaskView[]>([])
  const [mode, setMode] = useState<'analysis' | 'action'>('analysis')
  const lock = useRef(false)
  const activeRef = useRef(activeId)
  const sessionsRef = useRef(sessions)
  const uploadsBus = useRef<BroadcastChannel | null>(null)
  const uploadVersions = useRef<Record<number, number>>({})
  const initial = useRef(false)
  const refreshVersions = useRef<Record<string, number>>({})
  activeRef.current = activeId
  sessionsRef.current = sessions

  useEffect(() => {
    if (typeof window.BroadcastChannel === 'undefined') return
    const bus = new BroadcastChannel('banking-agent-upload-invalidations')
    uploadsBus.current = bus
    bus.onmessage = event => {
      const owner: unknown = event.data?.owner
      if (event.data?.type !== 'invalidate_uploads' || typeof owner !== 'number' || !Number.isSafeInteger(owner)) return
      uploadVersions.current[owner] = (uploadVersions.current[owner] || 0) + 1
      // This signal only discards cached display. It grants no data access.
      setSessions(prev => invalidateUploadedOwner(prev, owner))
    }
    return () => { uploadsBus.current = null; bus.close() }
  }, [])

  useEffect(() => {
    try { sessionStorage.setItem(KEY, JSON.stringify(sessions.filter(s => s.valid).slice(0, 8).map(s => ({
      id: s.id, title: s.title, principal: s.principal, expiresAt: s.expiresAt, createdAt: s.createdAt,
    })))) } catch { /* Tab storage unavailable; keep current in-memory session. */ }
  }, [sessions])
  const append = useCallback((sid: string, message: Omit<ChatMessage, 'id'>) => {
    setSessions(prev => prev.map(s => s.id === sid ? { ...s, messages: [...s.messages, { id: uid(), ...message }] } : s))
  }, [])
  const refresh = useCallback(async (sid: string) => {
    const version = (refreshVersions.current[sid] || 0) + 1
    refreshVersions.current[sid] = version
    // New-engine refresh can restore only an empty, server-verified execution
    // binding after worker loss. No preview or authorization is resurrected.
    if(bootstrapDialogue)await request(`/secure/sessions/${encodeURIComponent(sid)}/dialogues`,sid)
    const [state, list] = await Promise.all([api.state(sid), api.tasks(sid)])
    // Standalone queries can clear ui-state.task; retain only the same session's
    // selected task, refreshed from the authenticated task endpoint, not queries.
    const session = sessionsRef.current.find(s => s.id === sid)
    const previousId = session?.state?.task?.task.task.task_id ?? session?.selectedTaskId
    if (!state.task && previousId) {
      const retained = list.tasks.find(t => t.task.task_id === previousId)
      state.task = retained ? { task: retained, preview: null, challenge: null } : null
      state.results = []
    }
    if (refreshVersions.current[sid] !== version) return state
    // TaskStore owns current task state, including changes made in the panel.
    // Keep historical result cards, but refresh every timeline for the same ID.
    const currentTasks = new Map(list.tasks.map(t => [t.task.task_id, timeline(t)]))
    setSessions(prev => prev.map(s => s.id === sid ? { ...s, state, selectedTaskId: state.task?.task.task.task_id,
      messages: s.messages.map(m => m.task && currentTasks.has(m.task.task_id)
        ? { ...m, task: currentTasks.get(m.task.task_id) } : m) } : s))
    if (activeRef.current === sid) setTasks(list.tasks)
    return state
  }, [bootstrapDialogue])
  const run = useCallback(async <T,>(sid: string | null, operation: () => Promise<T>, sync = true, clearError = true, source:'read'|'operation'='operation'): Promise<T | null> => {
    if (lock.current) return null
    lock.current = true
    const protectedError=sid?uncertainErrors.current.get(sid):null
    setLoading(true)
    if (clearError&&!protectedError) setError('')
    if(!protectedError)setActionError(null)
    try {
      const out = await operation()
      if (sid && sync) {
        try { await refresh(sid) } catch {
          if(!protectedError){errorSource.current=source;setError('操作响应已返回，但状态同步失败。请同步任务核对，不要重复提交。')}
        }
      }
      return out
    } catch (e) {
      const message = e instanceof Error ? e.message : '操作未完成，请核对任务状态。'
      const code = e instanceof ApiError ? e.code : ''
      if(sid&&source==='operation'&&['network_error','commit_unknown','commit_outcome_unknown'].includes(code))uncertainErrors.current.set(sid,{code,message})
      if(!protectedError||source==='operation'){errorSource.current=source;setActionError({ code: code || 'unknown', message })}
      console.debug('banking request failed', code)
      if ((!sid || activeRef.current === sid)&&(!protectedError||source==='operation')) setError(message)
      if (sid) {
        const expired = ['unauthorized', 'credential_expired', 'session_revoked', 'session_not_found'].includes(code)
        if (expired) setSessions(prev => prev.map(s => s.id === sid ? { ...s, valid: false, state: null, messages: [] } : s))
        else if (sync) {
          try { await refresh(sid) } catch { /* Preserve the original error. */ }
        }
      }
      return null
    } finally { lock.current = false; setLoading(false) }
  }, [refresh])

  useEffect(() => {
    if (initial.current) return
    initial.current = true
    const sid = activeRef.current
    if (!sid) return
    void run(sid, async () => {
      await refresh(sid)
      return true
    }, false, true, 'read')
  }, [run, refresh])

  const login = useCallback(async (username: string, password: string) => {
    return run(null, async () => {
      const value = await api.login(username, password)
      const s: Session = { id: value.session_id, title: '新会话', principal: value.principal,
        expiresAt: value.credential_expires_at, createdAt: Date.now(), messages: [], state: null, valid: true }
      activeRef.current = s.id
      setSessions(prev => [s, ...prev].slice(0, 8))
      setActiveId(s.id)
      setMode('analysis')
      await refresh(s.id)
      return true
    }, false)
  }, [run, refresh])
  const active = sessions.find(s => s.id === activeId) ?? null
  const send = useCallback(async (text: string) => {
    const sid = activeRef.current
    const t = text.trim()
    if (!sid || !t) return
    if (t.length > 2000) { setError('消息最多 2000 字，请缩短后重发。'); return }
    await run(sid, async () => {
      // Verification codes never go to chat or transcript.
      if (/^[0-9]{6}$|验证码|校验码|动态码|otpauth:\/\/|\bBearer\s|\bs[12]_|密码(?:为|是|[:：=])/i.test(t)) throw new ApiError('independent_verification_required', '验证码请在独立验证框提交；密码或凭据请使用登录入口。')
      const owner = sessionsRef.current.find(s => s.id === sid)?.principal.user_id
      const uploadVersion = owner === undefined ? 0 : uploadVersions.current[owner] || 0
      const response = await api.chat(sid, t, mode)
      const uploadsChanged = owner !== undefined && uploadVersion !== (uploadVersions.current[owner] || 0)
      // The server owns field-aware privacy checks (including pasted JSON).
      // Do not put rejected credentials into a bubble or the stored session title.
      const privateInput = response.error_code === 'independent_verification_required'
      if (!privateInput) append(sid, { role: 'user', text: t })
      const state = await api.state(sid)
      const boundDisplays = response.displays?.filter(d => {
        const v = d.detail
        return !v || typeof v !== 'object' || Array.isArray(v) || v.kind !== 'query_result_v1' || v.turn_id === response.turn_id
      })
      const results = boundDisplays?.length ? boundDisplays :
        (response.result?.status === 'success' && (!response.turn_id || response.turn_id === state.turn_id) ? state.results : [])
      const revoked = uploadsChanged && response.context_source === 'uploaded'
      append(sid, { role: 'agent', text: revoked ? '上传账单已变更或撤销，请重新查询。' : response.message, results: revoked ? [] : results,
        task: response.task ? timeline(response.task.task) : undefined, error: response.action === 'error', source: response.context_source })
      if (!privateInput) setSessions(prev => prev.map(s => s.id === sid && s.title === '新会话' ? { ...s, title: t.slice(0, 12) } : s))
      if (response.action === 'error' && activeRef.current === sid) setError(response.message)
      return true
    })
  }, [run, append, mode])
  const read = useCallback(async (tool: string, args: Record<string, Json> = {}, authorize = false) => {
    const sid = activeRef.current
    if (!sid) return null
    return run(sid, async () => {
      const out = await api.read(sid, tool, args, authorize)
      if (out.error?.code !== 'query_authorization_required') {
        append(sid, { role: 'agent', text: out.message, error: out.status === 'failed',
          results: out.result ? [{ title: out.result.message, step_id: null, business: out.result, detail: null }] : [] })
      }
      return out
    },true,true,'read')
  }, [run, append])
  const analysis = useCallback(async (tool: string, args: Record<string, Json>) => {
    const sid = activeRef.current
    if (!sid) return
    await run(sid, async () => {
      const out = await api.analysis(sid, tool, args)
      if (out.error) throw new ApiError(out.error.code, out.error.message)
      append(sid, { role: 'agent', text: out.business.message,
        results: out.analyses.map(a => ({ title: '金融分析', step_id: null, business: null, detail: a.result })) })
      return true
    },true,true,'read')
  }, [run, append])
  const createTask = useCallback(async (plan: Plan, phoneBinding?:import('../components/phoneResolution').PhoneBinding) => {
    const sid = activeRef.current
    if (!sid) return null
    if(phoneBinding&&phoneBinding.session_id!==sid)throw new ApiError('stale_preview','会话已变化，请重新解析手机号。')
    return run(sid, async () => {
      append(sid, { role: 'user', text: plan.contract.goal })
      const out = await api.createTask(sid, plan, phoneBinding)
      append(sid, { role: 'agent', text: '任务已建立。请查看当前预览；确认前不会执行写操作。', task: timeline(out.task) })
      return out
    })
  }, [run, append])
  const checkBinding = useCallback(async (binding: Binding) => {
    if (activeRef.current !== binding.sessionId) throw new ApiError('stale_preview', '会话已切换，这张操作卡已失效。')
    const state = await api.state(binding.sessionId)
    if (!sameBinding(binding, bindingOf(binding.sessionId, state.task))) throw new ApiError('stale_preview', '操作已变化，请使用最新预览。')
    if (state.status === 'expired') throw new ApiError('pending_action_expired', '预览已过期，请先恢复任务。')
    return state
  }, [])
  const confirm = useCallback(async (binding: Binding, text: string) => {
    return run(binding.sessionId, async () => {
      await checkBinding(binding)
      const out = await api.confirm(binding, text)
      const state = await api.state(binding.sessionId)
      append(binding.sessionId, { role: 'agent', text: out.task.status === 'completed' ? '任务已完成，以下为核验后的结果。' : '当前步骤已处理，请查看后续步骤。',
        task: timeline(out.task), results: state.results })
      return true
    })
  }, [run, checkBinding, append])
  const verify = useCallback(async (binding: Binding, code: string, method: 'totp' | 'mock_otp') => {
    return run(binding.sessionId, async () => {
      await checkBinding(binding)
      const out = await api.verify(binding, code, method)
      if (out && typeof out === 'object' && 'error' in out) {
        const e = (out as { error: { code: string; message: string } | null }).error
        if (e) throw new ApiError(e.code, e.message)
      }
      return true
    })
  }, [run, checkBinding])
  const taskAction = useCallback(async (task: TaskView, action: TaskAction, values: Record<string, unknown> = {}) => {
    const sid = activeRef.current
    if (!sid) return
    return run(sid, async () => {
      const out = await api.act(sid, task, action, values)
      append(sid, { role: 'agent', text: '任务状态已更新。', task: timeline(out.task) })
      return out
    })
  }, [run, append])
  const selectSession = useCallback(async (sid: string) => {
    if (lock.current) return
    activeRef.current = sid
    setActiveId(sid); setTasks([]); setMode('analysis'); setError('')
    await run(sid, () => refresh(sid), false)
  }, [run, refresh])
  const logout = useCallback(async () => {
    const sid = activeRef.current
    if (!sid) return
    await run(sid, async () => {
      await api.logout(sid)
      setSessions(prev => prev.filter(s => s.id !== sid))
      setActiveId(null); activeRef.current = null; setTasks([])
      return true
    }, false)
  }, [run])
  const backgroundPending = useRef(false)
  const sync = useCallback(async (background = false) => {
    const sid=activeRef.current
    if(!sid)return null
    if(!background)return run(sid,()=>refresh(sid),false,true,'read')
    // Polling is a read, not a user operation: it must never disable/recreate
    // the composer or clear the user's latest action error. Responses already
    // use refreshVersions to reject stale updates after a newer request.
    if(lock.current||backgroundPending.current)return null
    backgroundPending.current=true
    try{return await refresh(sid)}catch(e){
      if(e instanceof ApiError&&['unauthorized','credential_expired','session_revoked'].includes(e.code))
        setSessions(prev=>prev.map(s=>s.id===sid?{...s,valid:false,state:null,messages:[]}:s))
      return null
    }finally{backgroundPending.current=false}
  }, [run, refresh])
  const clearMessages = useCallback(() => {
    const sid = activeRef.current
    setSessions(prev => prev.map(s => s.id === sid ? { ...s, messages: [] } : s))
  }, [])
  const invalidateUploaded = useCallback((sid: string) => {
    const owner = sessionsRef.current.find(s => s.id === sid)?.principal.user_id
    if (owner === undefined) return
    uploadVersions.current[owner] = (uploadVersions.current[owner] || 0) + 1
    setSessions(prev => invalidateUploadedOwner(prev, owner))
    uploadsBus.current?.postMessage({ type: 'invalidate_uploads', owner })
  }, [])
  const setDraft = (draft: string) => setSessions(prev=>prev.map(s=>s.id===activeRef.current?{...s,draft}:s))
  const clearReadError=()=>{if(errorSource.current==='read'&&!uncertainErrors.current.has(activeRef.current||'')){setError('');setActionError(null)}}
  return { isBusy: () => lock.current, setDraft, sessions, activeId, active, loading, error, setError, clearReadError, actionError, setActionError, tasks, mode, setMode,
    login, logout, send, read, analysis, createTask, confirm, verify, taskAction, selectSession, sync, run, clearMessages, invalidateUploaded }
}

export function invalidateUploadedOwner(sessions: Session[], owner: number): Session[] {
  return sessions.map(s => s.principal.user_id === owner ? { ...s,
    messages: s.messages.filter(m => m.source !== 'uploaded'),
    state: s.state ? { ...s.state, results: [] } : null } : s)
}
