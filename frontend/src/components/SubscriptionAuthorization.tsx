import type {SubscriptionQuery} from '../store/useChat'
import {ResultCardRenderer} from './cards/ResultCardRenderer'

export function SubscriptionAuthorization({queries,busy,collapsed,answerPending,answerNotice,onAuthorize}:{queries:SubscriptionQuery[];busy:boolean;collapsed:boolean;answerPending:boolean;answerNotice?:string;onAuthorize:(queries:SubscriptionQuery[])=>unknown}){
  const pending=queries.filter(q=>q.state==='pending')
  const labels={pending:'待授权',authorized:'已授权 · 数据已取得',failed:'查询未完成',expired:'授权已过期',revoked:'授权已撤销'}
  const content=<section className="subscription-authorization" aria-label="本条消息的查询授权">
    <strong>本次查询范围</strong>
    <ul>{queries.map(q=><li key={q.call_id}><span>{q.scope}</span><strong>{labels[q.state]}</strong><p>{q.next_step}</p></li>)}</ul>
    {!!pending.length&&<><p>仅同意上列待授权查询，不授权取消代扣、付款或其它查询。</p><button disabled={busy} onClick={()=>void onAuthorize(pending)}>授权并回答原问题{pending.length>1?`（${pending.length}项）`:''}</button></>}
    {answerPending&&<p role="status">数据已取得，正在等待整理原问题的答复；新问题会保留。</p>}
    {answerNotice&&<p role="status">{answerNotice}</p>}
    {queries.filter(q=>q.state==='authorized'&&q.result).map(q=><div key={q.call_id}><ResultCardRenderer result={q.result!}/><p className="query-observed">原查询时间：{String((q.result as unknown as {queried_at?:string}).queried_at||'未记录')} · 历史结果，未重新查询</p></div>)}
  </section>
  return collapsed?<details className="query-history"><summary>原消息的订阅查询 · {pending.length?'待授权':'查看结果及授权记录'}</summary>{content}</details>:content
}
