import { useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import type { ChatMessage } from '../store/useChat'
import { MessageBubble } from './MessageBubble'
import { MobileIcon } from './MobileIcon'
export interface ChatHistoryProps { hasMore: boolean; total: number | null; modelWindow?: number | null; loading: boolean; onLoadEarlier: () => void }
export function ChatPanel({messages,loading,onSend,mode,setMode,children,draft:input,setDraft:setInput,automatic=false,history}:{
  draft:string;setDraft:(value:string)=>void;messages:ChatMessage[];loading:boolean;onSend:(text:string)=>Promise<unknown>
  mode:'analysis'|'action';setMode:(mode:'analysis'|'action')=>void;children?:ReactNode;automatic?:boolean;history?:ChatHistoryProps
}){
  const scroll=useRef<HTMLDivElement>(null)
  useEffect(()=>{scroll.current?.scrollTo({top:scroll.current.scrollHeight,behavior:'smooth'})},[messages,loading])
  const submit=()=>{if(!loading&&input.trim()){const value=input;setInput('');void onSend(value)}}
  return <div className="modern-chat-panel">
    <div ref={scroll} className="modern-chat-feed">
      {history && (history.hasMore || history.total != null || history.loading) && <div className="chat-history-bar" aria-label="对话历史分页">
        {history.hasMore
          ? <button type="button" className="secondary" disabled={loading||history.loading} onClick={()=>history.onLoadEarlier()}>{history.loading ? '正在读取更早消息…' : history.total != null ? `加载更早消息（共 ${history.total} 轮）` : '加载更早消息'}</button>
          : <span className="muted-copy">已显示服务端提供的全部对话轮次。</span>}
        <p>页面历史可独立翻页{history.modelWindow ? `；模型每轮只读取最近 ${history.modelWindow} 轮的近期上下文` : ''}，不等于把全部历史发给模型。旧轮次中的金额与状态只是历史，不是当前余额或授权。</p>
      </div>}
      {!messages.length&&<div className="modern-welcome"><span className="welcome-icon"><MobileIcon name="spark" size={28}/></span><h2>你好，今天想了解什么？</h2><p>查询了解情况；办理业务须核对预览并明确确认。</p><div className="modern-suggestions"><button disabled={loading} onClick={()=>void onSend('帮我查询本月消费情况')}>分析本月消费 <MobileIcon name="arrow" size={15}/></button><button disabled={loading} onClick={()=>void onSend('查看我的银行卡')}>查看我的银行卡 <MobileIcon name="arrow" size={15}/></button></div></div>}
      {messages.map((m,i)=><MessageBubble key={m.id} message={m} onNext={!loading&&i===messages.length-1?()=>void onSend('下一页'):undefined}/>)}
      {children}
      {loading&&<div role="status" className="chat-loading"><span className="loading-dot"/>正在处理，请稍候…</div>}
    </div>
    <div className="modern-composer">
      {!automatic&&<fieldset className="intent-switch" disabled={loading} aria-label="聊天模式">
        <label className={mode==='analysis'?'current':''}><input type="radio" name="intent-mode" checked={mode==='analysis'} onChange={()=>setMode('analysis')}/>查询 / 分析</label>
        <label className={mode==='action'?'current':''}><input type="radio" name="intent-mode" checked={mode==='action'} onChange={()=>setMode('action')}/>办理业务</label>
      </fieldset>}
      <p className="mode-explanation">{automatic?'描述你的需求，办理前仍需预览、验证和明确确认。':mode==='analysis'?'查询模式仅查询和分析。办理业务请主动切换模式。':'办理模式可建立任务；预览、验证和明确确认后才会执行。'}</p><div className="compose-row"><input aria-label="输入需求" value={input} disabled={loading} maxLength={2000} onChange={e=>setInput(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.nativeEvent.isComposing)submit()}} placeholder="说说你想办理的业务…"/><button type="button" aria-label="发送" disabled={loading||!input.trim()} onClick={submit}><MobileIcon name="send" size={20}/><span className="sr-only">发送</span></button></div>
      <p>安全操作须经预览、确认和必要的身份验证</p>
    </div>
  </div>
}
