import type { ChatMessage } from '../store/useChat'
import { ResultCardRenderer } from './cards/ResultCardRenderer'
import { TaskTimeline } from './cards/InteractionCards'
import { StructuredData } from './StructuredData'
import { readableReply } from './replyPresentation'
import { SafeMarkdown } from './SafeMarkdown'
import {SubscriptionAuthorization} from './SubscriptionAuthorization'
export function MessageBubble({ message, onNext }: { message: ChatMessage; onNext?: () => void }) {
  if (message.role === 'user') return <div className="chat-user-row" data-message-id={message.id}><div className="chat-user-bubble">{message.text}</div>{(message.canInspect||message.delivery==='uncertain'||message.delivery==='rejected'||message.onResume)&&<div className="chat-delivery" role="status">
    <p>{message.delivery==='rejected'?(message.deliveryMessage||'本次消息未受理，可以重新发送。'):message.delivery==='uncertain'?'回复连接未完成，请先读取本条消息状态，避免重复提交。':'可读取本条消息的处理结果。'}</p>
    <div className="chat-delivery-actions">{message.delivery==='rejected'?<><button onClick={message.onResume}>重新发送</button><button onClick={message.onEdit}>编辑消息</button></>:<><button onClick={message.onRetry}>读取本条状态</button>{(message.onResume||message.delivery==='uncertain')&&<details><summary>更多操作</summary><p>先核实原轮次；继续恢复沿用原身份，不会新建付款。</p>{message.onResume&&<button onClick={message.onResume}>继续恢复原轮次</button>}{message.delivery==='uncertain'&&<button onClick={message.onEdit}>取回消息草稿</button>}</details>}</>}</div>
  </div>}</div>
  if(message.pending)return <div className="chat-processing" role="status" data-message-id={message.id}>正在处理…</div>
  const text = readableReply(message)
  return <div className="chat-agent-message flex flex-col gap-2 items-start max-w-[95%] min-w-0" data-message-id={message.id}>
    {message.historical && <p className="text-[11px] text-slate-400">历史对话 · 金融数据需要重新查询</p>}
    {text && <div className={`chat-assistant-bubble ${message.error&&!message.answerNotice?'chat-answer-error':''}`} data-publication={message.animate?'reviewed_segments':undefined}><SafeMarkdown text={text} animate={!!message.animate&&!message.historical}/></div>}
    {message.answerNotice&&<p className="chat-answer-notice" role="status">{message.answerNotice}</p>}
    {message.error&&['available','partial'].includes(message.queryStatus||'')&&<p className="chat-publication-note" role="status">{message.queryStatus==='partial'?'部分业务结果已取得':'业务结果已取得'}；自然语言答复尚未完成，数据仍保留。</p>}
    {message.queryAuthorization&&<SubscriptionAuthorization {...message.queryAuthorization}/>}
    {message.onRephrase&&<div className="chat-answer-retry"><button onClick={message.onRephrase}>重新组织答复</button><small>仅使用本轮已有证据，不重新执行业务。</small></div>}
    {message.streaming&&<small className="chat-publication-note">核验后分段显示 · 接收尚未结束</small>}
    {message.interrupted&&<small className="chat-publication-note" role="status">回复接收已中断，已有内容保留；请核实上一轮。</small>}
    {!!message.results?.length&&<section className="chat-result-details" aria-label="明细与统计口径">{message.results.map((r, i) => r.business ? <ResultCardRenderer key={i} result={r.business} /> :
      <StructuredData key={i} title={r.title} data={r.detail} onNext={onNext} />)}
    </section>}
    {message.task && <details className="w-full text-xs text-slate-500"><summary className="cursor-pointer p-2">查看任务进度</summary><TaskTimeline task={message.task} /></details>}
  </div>
}
