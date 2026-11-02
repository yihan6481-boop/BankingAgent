import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import type { ChatMessage } from '../store/useChat'
import { MessageBubble } from './MessageBubble'
import { MobileIcon } from './MobileIcon'
import { BrandMark } from './BrandMark'
import { createVoiceCapture } from './voiceSession'
import {createRecordedCapture,recordingMime,uploadRecording,voiceCapabilities,voiceMessage} from './recordedVoice'
import type {VoiceHandle,VoiceCapabilities} from './recordedVoice'
import {useReadingChrome} from '../hooks/useReadingChrome'
export interface ChatHistoryProps { hasMore: boolean; total: number | null; modelWindow?: number | null; loading: boolean; onLoadEarlier: () => void }
export function ChatPanel({messages,loading,onSend,mode,setMode,children,top,composerTools,draft:input,setDraft:setInput,automatic=false,history,name='你',onShortcut,onServices,onAttach,onSupport,onHistory,onProfile,active=true,paused=false,sid='',voiceScope='',onReadingChange}:{
  draft:string;setDraft:(value:string)=>void;messages:ChatMessage[];loading:boolean;onSend:(text:string,onAccepted?:()=>void)=>Promise<unknown>
  mode:'analysis'|'action';setMode:(mode:'analysis'|'action')=>void;children?:ReactNode;automatic?:boolean;history?:ChatHistoryProps
  top?:ReactNode;composerTools?:ReactNode
  name?:string;onShortcut?:(target:string)=>void;onServices?:()=>void;onAttach?:()=>void;onSupport?:()=>void;active?:boolean;paused?:boolean
  sid?:string;voiceScope?:string;onReadingChange?:(hidden:boolean)=>void
  onHistory?:()=>void;onProfile?:()=>void
}){
  const scroll=useRef<HTMLDivElement>(null),composing=useRef(false),submitting=useRef(false)
  const [voice,setVoice]=useState(false),[recording,setRecording]=useState(false),[voiceNote,setVoiceNote]=useState(''),[voiceBusy,setVoiceBusy]=useState(false)
  const [capabilities,setCapabilities]=useState<VoiceCapabilities|null>(null),[channel,setChannel]=useState<'auto'|'browser'|'recorded'>('auto')
  const [focused,setFocused]=useState(false),[ime,setIme]=useState(false),[pendingVoice,setPendingVoice]=useState('')
  const recognition=useRef<VoiceHandle|null>(null),inputRef=useRef<HTMLInputElement>(null)
  const latest=useRef({input,setInput,sid,voiceScope,active});latest.current={input,setInput,sid,voiceScope,active}
  const last=messages[messages.length-1]?.id,lastText=messages[messages.length-1]?.text
  const follow=useRef(true)
  const chrome=useReadingChrome({feed:scroll,follow,pinned:focused||ime||voiceBusy,active,onChange:onReadingChange,contentKey:messages[0]?.id+':'+last+':'+lastText,historyLoading:history?.loading})
  useEffect(()=>{if(scroll.current&&follow.current){chrome.markProgrammatic();scroll.current.scrollTo({top:scroll.current.scrollHeight,behavior:'auto'})}},[last,lastText])
  useEffect(()=>()=>{recognition.current?.cancel();recognition.current=null},[])
  useEffect(()=>{const e=scroll.current;if(!e||typeof ResizeObserver==='undefined')return
    const observer=new ResizeObserver(()=>{if(follow.current){chrome.markProgrammatic();e.scrollTop=e.scrollHeight}});observer.observe(e);return()=>observer.disconnect()
  },[])
  useEffect(()=>{
    if(!sid)return
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),6000)
    setCapabilities(null)
    void voiceCapabilities(sid,controller.signal).then(v=>{if(!controller.signal.aborted)setCapabilities(v)}).catch(()=>{/* Browser recognition remains independently available. */}).finally(()=>clearTimeout(timer))
    return()=>{controller.abort();clearTimeout(timer)}
  },[sid])
  useEffect(()=>{recognition.current?.cancel();recognition.current=null;setPendingVoice('');setVoiceBusy(false);setRecording(false);setVoiceNote('');setVoice(false)},[sid,voiceScope])
  const applyVoice=(text:string)=>{if(composing.current||latest.current.input.length+text.length>2000){setPendingVoice(text);return}latest.current.setInput(latest.current.input+text);setPendingVoice('');setVoice(false)}
  const submit=async()=>{if(!submitting.current&&!loading&&!paused&&!composing.current&&input.trim()){submitting.current=true;const value=input;try{await onSend(value,()=>{if(latest.current.input===value)setInput('')})}finally{submitting.current=false}}}
  const beginVoice=()=>{
    if(recognition.current)return
    const Speech=(window as any).SpeechRecognition||(window as any).webkitSpeechRecognition
    if(!window.isSecureContext){setVoiceNote('当前页面不是安全连接，麦克风不可用。请使用 HTTPS 或本机地址，或继续文字输入。');return}
    const mime=recordingMime(window.MediaRecorder,capabilities?.accepted_mime_types||[])
    const useRecorded=channel==='recorded'||(channel==='auto'&&!!capabilities?.configured)||!Speech
    if(useRecorded&&(!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder)){setVoiceNote('当前环境不支持语音识别或网页录音，可继续打字，或尝试手机键盘自带的语音输入。网页不能直接启动系统听写。');return}
    if(useRecorded&&!capabilities?.configured){setVoiceNote(capabilities?voiceMessage(capabilities.reason||'voice_not_configured'):'录音转写服务状态尚未取得，可继续打字或稍后再试。');return}
    if(useRecorded&&!mime){setVoiceNote(voiceMessage('voice_format_unsupported'));return}
    const owner={sid,voiceScope};const owns=()=>latest.current.sid===owner.sid&&latest.current.voiceScope===owner.voiceScope&&latest.current.active
    const callbacks={onRecording:setRecording,onActivity:setVoiceBusy,onNote:setVoiceNote,onProcessing:()=>setVoice(false),
      onText:(text:string)=>{if(owns())applyVoice(text)},onDone:()=>{if(recognition.current===session)recognition.current=null}}
    const session:VoiceHandle=useRecorded?createRecordedCapture({...callbacks,capabilities:capabilities!,mime:mime!,transcribe:(blob,duration,signal)=>uploadRecording(sid,blob,duration,crypto.randomUUID(),signal)}):createVoiceCapture({...callbacks,makeRecognition:()=>new Speech()})
    recognition.current=session;session.start()
  }
  const endVoice=(cancel=false)=>{if(cancel)recognition.current?.cancel();else recognition.current?.stop()}
  useEffect(()=>{const stop=()=>{if(document.visibilityState!=='visible')endVoice(true)};document.addEventListener('visibilitychange',stop);return()=>document.removeEventListener('visibilitychange',stop)},[])
  useEffect(()=>{if(!active)endVoice(true)},[active])
  return <div className="modern-chat-panel" data-reading={chrome.hidden} data-composer-pinned={focused||ime||voiceBusy}><div className="chat-top-slot"><div className="chat-chrome-inner" aria-hidden={chrome.hidden} ref={node=>{if(node)node.inert=chrome.hidden}}><header className="assistant-topbar"><button className="mobile-icon-button" aria-label="历史对话" onClick={onHistory}><MobileIcon name="menu" size={23}/></button><span className="assistant-title"><BrandMark size={22}/><strong>银行助手</strong></span><div className="assistant-topbar-actions"><button className="mobile-icon-button" aria-label="我的" onClick={onProfile}><MobileIcon name="user" size={23}/></button><button className="mobile-icon-button" aria-label="联系人工客服" onClick={onSupport}><MobileIcon name="headset" size={23}/></button></div></header></div></div><div ref={scroll} className="modern-chat-feed" tabIndex={0} aria-label="聊天消息">
    {top}
    {!messages.length&&<div className="assistant-landing"><div className="assistant-greeting"><div><h1>你好，{name}</h1><p>今天想办理什么？</p></div></div>
      <div className="assistant-shortcuts" aria-label="快捷业务">{([['transfer','transfer','转账'],['bill','receipt','账单'],['billvision','camera','识图'],['wealth','chart','理财']] as const).map(([target,icon,title])=><button key={target} onClick={()=>onShortcut?.(target)}><MobileIcon name={icon} size={29}/><span>{title}</span></button>)}</div>
      <section className="suggestion-card"><h2>试着问我</h2>{['帮我看看本月支出','给联系人转一笔钱','分析一下我的结余'].map(text=><button key={text} disabled={loading} onClick={()=>{setInput(text)}}>{text}<MobileIcon name="chevron" size={18}/></button>)}</section></div>}
    {messages.length>0&&history?.hasMore&&<div className="chat-history-bar"><button className="secondary" disabled={history.loading} onClick={()=>{chrome.beforeHistory();history.onLoadEarlier()}}>{history.loading?'正在读取…':'加载更早消息'}</button><small>历史金融数据需要重新查询</small></div>}
    {messages.map((m,i)=><MessageBubble key={m.id} message={m} onNext={!loading&&i===messages.length-1?()=>void onSend('下一页'):undefined}/>)}{children}
    </div>
    {chrome.hidden&&<div className="reading-restore" aria-label="恢复聊天操作"><button onClick={()=>{chrome.show();requestAnimationFrame(()=>inputRef.current?.focus())}}>继续输入</button><button onClick={chrome.latest}>回到最新</button></div>}
    <div className="chat-composer-slot"><div className="chat-chrome-inner" aria-hidden={chrome.hidden} ref={node=>{if(node)node.inert=chrome.hidden}}><div className="modern-composer" onFocusCapture={chrome.show}>{composerTools}{!automatic&&<fieldset className="intent-switch" disabled={loading}><label><input type="radio" checked={mode==='analysis'} onChange={()=>setMode('analysis')}/>查询 / 分析</label><label><input type="radio" checked={mode==='action'} onChange={()=>setMode('action')}/>办理业务</label></fieldset>}
    {voice&&capabilities?.configured&&!!((window as any).SpeechRecognition||(window as any).webkitSpeechRecognition)&&<label className="voice-channel">语音通道<select aria-label="语音通道" value={channel} disabled={voiceBusy} onChange={e=>setChannel(e.target.value as 'auto'|'browser'|'recorded')}><option value="auto">自动（优先录音转写）</option><option value="browser">浏览器识别</option><option value="recorded">录音后转写</option></select></label>}
    {!!pendingVoice&&<div className="voice-note"><p>识别文字已保留，正在组词或草稿较长，尚未插入。</p><details><summary>查看待插入文字</summary><p>{pendingVoice}</p></details><button onClick={()=>applyVoice(pendingVoice)}>插入当前草稿</button></div>}
    {voiceNote&&<p className="voice-note" role="status">{voiceNote}</p>}{voiceBusy&&<button type="button" className="secondary" onClick={()=>endVoice(true)}>取消本次语音</button>}<div className={`compose-row ${recording?'recording':''}`}>
      <button type="button" aria-label={voice?'切换文字输入':'切换语音输入'} onClick={()=>{endVoice(true);setVoice(!voice);setVoiceNote('')}}><MobileIcon name="voice" size={27}/></button>
      {voice&&<button type="button" className="hold-to-talk" aria-label={voiceBusy?'结束语音输入':'开始语音输入'} aria-pressed={voiceBusy} onClick={()=>{if(recognition.current)endVoice();else beginVoice()}}>{voiceBusy?'点击结束':'点击说话'}</button>}
      <input ref={inputRef} hidden={voice} aria-label="输入需求" value={input} maxLength={2000} onFocus={()=>setFocused(true)} onBlur={()=>setFocused(false)} onChange={e=>setInput(e.target.value)} onCompositionStart={()=>{composing.current=true;setIme(true)}} onCompositionEnd={()=>{composing.current=false;setIme(false)}} onKeyDown={e=>{if(e.key==='Enter'&&!e.nativeEvent.isComposing)void submit()}} placeholder={paused?'人工服务期间可保留草稿…':loading?'可编辑下一条草稿，回复结束后发送':'输入问题，或点击语音'} autoComplete="off"/>
      {!voice&&input.trim()?<button type="button" className="send-message" aria-label="发送" disabled={loading||paused} onClick={()=>void submit()}><MobileIcon name="send" size={23}/></button>:<button type="button" aria-label="添加图片或文件" onClick={onAttach}><MobileIcon name="plus" size={27}/></button>}
      <button type="button" aria-label="全部服务" onClick={onServices}><MobileIcon name="grid" size={25}/></button></div></div></div></div></div>
}
