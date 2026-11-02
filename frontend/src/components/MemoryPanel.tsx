import {useCallback,useEffect,useRef,useState} from 'react'
import {request} from '../api/client'

type Settings={available?:boolean;reason?:string;automatic:boolean;profile:boolean;version:number;pending_operations:number;cleanup_state:string}
type Memory={reference:string;version:number;text?:string;origin_ref?:string;origin_kind?:string;updated_at?:string}
type Operation={operation_ref:string;operation_state:string;effect_state:string;availability:string;source_ref:string;error_code?:string}
const states:Record<string,string>={queued:'已排队',running:'执行中',reconciling:'核实中',succeeded:'已完成',failed:'未完成',cancelled:'已停止'}
const effects:Record<string,string>={none:'无',partial:'部分',committed:'已提交',unknown:'未知'}
const availability:Record<string,string>={pending:'整理中',available:'可召回',blocked:'受阻或已删除',unknown:'待核实'}
const origins:Record<string,string>={user:'用户陈述',verified_tool:'业务结果摘要',inference:'模型推断'}
const pending=(o:Operation)=>['queued','running','reconciling'].includes(o.operation_state)

export function MemoryPanel({sid}:{sid:string}){
  const base=`/secure/sessions/${encodeURIComponent(sid)}/recall`
  const [settings,setSettings]=useState<Settings|null>(null)
  const [items,setItems]=useState<Memory[]>([]),[ops,setOps]=useState<Operation[]>([])
  const [offset,setOffset]=useState(0),[limit,setLimit]=useState(20),[total,setTotal]=useState(0)
  const [search,setSearch]=useState(''),[query,setQuery]=useState('')
  const [editing,setEditing]=useState<Memory|null>(null),[text,setText]=useState(''),[deleting,setDeleting]=useState<Memory|null>(null)
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[status,setStatus]=useState('')
  const [health,setHealth]=useState('正在检查服务…')
  const [preferenceStatus,setPreferenceStatus]=useState('正在读取偏好设置…')
  const live=useRef(true),loading=useRef(false),queued=useRef(false)
  const savedVersion=useRef<number|null>(null)
  const latest=useRef<()=>Promise<void>>(async()=>{})
  const scopeKey=useRef('');scopeKey.current=`${sid}:${offset}:${limit}:${query}`
  useEffect(()=>{live.current=true;return()=>{live.current=false}},[])
  const load=useCallback(async()=>{
    if(loading.current){queued.current=true;return}
    loading.current=true
    const token=scopeKey.current
    try{
      const cfg=await request<Settings>(`${base}/settings`,sid)
      if(!live.current||token!==scopeKey.current)return
      setSettings(cfg)
      setPreferenceStatus(savedVersion.current===cfg.version?`偏好设置已保存（版本 ${cfg.version}）；服务连通和记忆操作结果单独核实。`:`偏好已读取（版本 ${cfg.version}）；不代表记忆已写入或可召回。`)
      if(cfg.available===false){setItems([]);setHealth('当前启动模式未连接记忆服务，请使用候选体验入口。');setStatus('');return}
      try{const h=await request<{availability:string;reason?:string}>(`${base}/health`,sid);if(live.current&&token===scopeKey.current)setHealth(h.availability==='available'?'记忆文档接口可连接；写入与召回链路尚需分别核实。':`记忆服务暂时中断（${h.reason||'连接未完成'}），请重试；已有设置保持原值。`)}catch{if(live.current)setHealth('无法检查记忆服务连接，请重试。')}
      const results=await Promise.allSettled([
        request<{items:Memory[];total?:number;availability:string}>(`${base}/sources?offset=${offset}&limit=${limit}&q=${encodeURIComponent(query)}`,sid),
        request<{operations:Operation[]}>(`${base}/operations?limit=20`,sid),
      ])
      if(!live.current||token!==scopeKey.current)return
      const [memories,operations]=results
      if(memories.status==='fulfilled'){
        setItems(memories.value.items);setTotal(memories.value.total??0)
        setStatus(memories.value.availability==='available'?'当前有效资料':'有记忆正在整理或清理，暂不使用受影响资料。')
      }else{setItems([]);setStatus('资料暂时无法核实，未使用旧缓存。');setError(memories.reason instanceof Error?memories.reason.message:'查询未完成')}
      if(operations.status==='fulfilled')setOps(operations.value.operations)
      else setError(operations.reason instanceof Error?operations.reason.message:'操作状态暂不可用')
    }catch(e){if(live.current){setSettings(null);setHealth('设置加载失败，请重试。');setError(e instanceof Error?e.message:'记忆服务暂不可用')}}
    finally{
      loading.current=false
      if(queued.current&&live.current){queued.current=false;queueMicrotask(()=>{void latest.current()})}
    }
  },[base,sid,offset,limit,query])
  latest.current=load
  useEffect(()=>{void load()},[load])
  const hasPending=ops.some(pending)||!!settings?.pending_operations
  useEffect(()=>{
    if(!hasPending)return
    const timer=window.setInterval(()=>{void load()},2000)
    return()=>window.clearInterval(timer)
  },[hasPending,load])
  const act=async(fn:()=>Promise<unknown>)=>{
    setBusy(true);setError('')
    try{await fn();if(live.current){setEditing(null);setDeleting(null);setItems([])};await load()}
    catch(e){if(live.current)setError(e instanceof Error?e.message:'请求未完成，请查看原操作状态');await load()}
    finally{if(live.current)setBusy(false)}
  }
  const configure=(key:'automatic'|'profile',value:boolean)=>{
    if(!settings)return
    void act(async()=>{
      const cfg=await request<Settings>(`${base}/settings`,sid,'PUT',{
        version:settings.version,automatic:key==='automatic'?value:settings.automatic,profile:key==='profile'?value:settings.profile,
      });if(live.current){savedVersion.current=cfg.version;setSettings(cfg);setPreferenceStatus(`偏好设置已保存（版本 ${cfg.version}）；服务连通和记忆操作结果单独核实。`)}
    })
  }
  return <section className="mobile-page memory-page" aria-label="记忆与画像管理">
    <div className="eyebrow">MEMORY</div><h1 className="mobile-heading">记忆与偏好</h1>
    <p className="mobile-footnote">资料用于相关对话；余额、交易和精确财务统计以银行工具核实为准。关闭开关不会删除已有内容。</p>
    {error&&<p role="alert" className="text-red-700">{error}</p>}
    <div className="result-card p-4 rounded-xl bg-white border space-y-3">
      <p role="status">{preferenceStatus}</p>
      <p role="status">{health}</p><button type="button" disabled={busy} onClick={()=>{setError('');void load()}}>重试连接与设置</button>
      <label className="block"><input type="checkbox" aria-label="自动长期记忆" checked={!!settings?.automatic} disabled={busy||!settings||settings.available===false} onChange={e=>configure('automatic',e.target.checked)}/> 自动长期记忆</label>
      <p className="text-sm text-slate-500">首次默认关闭。启用后整理有价值的自述；明确要求“记住”仍可单独保存。</p>
      <label className="block"><input type="checkbox" aria-label="画像用途" checked={!!settings?.profile} disabled={busy||!settings||settings.available===false} onChange={e=>configure('profile',e.target.checked)}/> 将有效背景与真实财务查询结合用于画像分析</label>
      <p className="text-sm text-slate-500">画像中的综合判断属于解释。可在新 LLM 对话中询问“结合我的目标，这个月能否多存一点”。</p>
      {settings?.cleanup_state==='pending'?<p role="status">仍有 {settings.pending_operations} 项记忆操作待核实或清理，尚不能视为已完成。</p>:settings&&<p role="status">当前没有待完成的记忆操作；不代表召回链路已验证。</p>}
    </div>
    <form className="flex gap-2 my-4" onSubmit={e=>{e.preventDefault();setOffset(0);setQuery(search)}}>
      <input aria-label="搜索记忆" value={search} maxLength={200} onChange={e=>setSearch(e.target.value)} placeholder="搜索自己的资料" className="min-w-0 flex-1 border rounded-lg p-2"/>
      <button type="submit" disabled={busy}>搜索</button>
    </form>
    <p role="status">{status}</p>
    <div className="space-y-3">{items.map(item=><article key={item.reference} className="result-card p-4 rounded-xl bg-white border">
      <p className="whitespace-pre-wrap">{item.text}</p>
      <small className="block text-slate-500">来源：{origins[item.origin_kind||'']||'待核实'} · 版本 {item.version} · {item.updated_at?new Date(item.updated_at).toLocaleString('zh-CN'):''}</small>
      <small className="block text-slate-500 break-all">来源消息 {item.origin_ref}</small>
      <div className="flex gap-4 mt-2"><button disabled={busy} onClick={()=>{setEditing(item);setText(item.text||'');setDeleting(null)}}>更正</button><button disabled={busy} onClick={()=>{setDeleting(item);setEditing(null)}}>删除此项</button></div>
      {editing?.reference===item.reference&&<form onSubmit={e=>{e.preventDefault();void act(()=>request(`${base}/sources/${item.reference}`,sid,'PATCH',{version:item.version,content:text}))}}>
        <label className="block mt-3">修改这一项完整内容，保留未改变的安排<textarea aria-label="更正记忆内容" value={text} maxLength={4000} onChange={e=>setText(e.target.value)} className="block w-full border rounded-lg p-2"/></label>
        <button type="submit" disabled={busy||!text.trim()}>提交更正</button> <button type="button" onClick={()=>setEditing(null)}>返回</button>
      </form>}
      {deleting?.reference===item.reference&&<div role="dialog" aria-label="确认删除单项记忆" className="mt-3"><p>仅删除这一项记忆及相关派生内容，不删除聊天或银行记录。处理中不会继续使用旧资料。</p><button disabled={busy} onClick={()=>void act(()=>request(`${base}/sources/${item.reference}/delete`,sid,'POST',{version:item.version}))}>确认删除</button> <button onClick={()=>setDeleting(null)}>返回</button></div>}
    </article>)}</div>
    <div className="flex gap-4 my-4"><button disabled={busy||offset===0} onClick={()=>setOffset(Math.max(0,offset-limit))}>上一页</button><span>{total?offset+1:0} 起 / {total} 项</span><button disabled={busy||offset+limit>=total} onClick={()=>setOffset(offset+limit)}>下一页</button><select aria-label="每页记忆条数" value={limit} onChange={e=>{setLimit(Number(e.target.value));setOffset(0)}}><option value={20}>20条</option><option value={50}>50条</option></select></div>
    <h2 className="profile-section-title">最近操作</h2>
    {ops.map(op=><div key={op.operation_ref} className="result-card p-3 rounded-xl border bg-white mb-2"><p>{states[op.operation_state]||op.operation_state} · 资料更新：{effects[op.effect_state]||op.effect_state} · {availability[op.availability]||op.availability}</p><small className="block break-all">操作引用 {op.operation_ref}</small>{op.error_code&&<small className="text-red-700">{op.error_code}</small>}{op.error_code==='paid_budget_exhausted'&&<button disabled={busy} onClick={()=>void act(()=>request(`${base}/operations/${op.operation_ref}/recover`,sid,'POST'))}>额度恢复后核实原操作（最多一次）</button>}</div>)}
    <button disabled={busy} onClick={()=>{setError('');void load()}}>刷新权威状态</button>
  </section>
}
