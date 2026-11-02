import { useEffect,useState } from 'react'
import { request } from '../api/client'
import { MobileIcon } from './MobileIcon'
import { BottomSheet } from './BottomSheet'
type Review={state:string;version?:string;revision?:number;choice:string|null;signals:{code:string;label:string;count?:number;sample_size?:number;threshold_cents?:number}[];semantic_clues:{kind:string;label:string;quote:string}[];limitations:string[];sample_size?:number;scope?:string}
export function RiskReview({sid,taskId,revision,onSupport,onModify}:{sid:string;taskId:string;revision:number;onSupport?:()=>void;onModify?:()=>void}){
 const [review,setReview]=useState<Review|null>(null),[closed,setClosed]=useState(false),[manual,setManual]=useState(false),[note,setNote]=useState('')
 const path=`/secure/sessions/${encodeURIComponent(sid)}/mobile/risk/${taskId}`
 useEffect(()=>{let live=true;setReview(null);setClosed(false);setManual(false);setNote('');void request<Review>(path,sid).then(v=>{if(live)setReview(v)}).catch(()=>{if(live)setReview({state:'unavailable',choice:null,signals:[],semantic_clues:[],limitations:[]})});return()=>{live=false}},[path,sid,revision])
 const choose=async(choice:'continue'|'modify'|'dismiss'|'support')=>{
  setClosed(true);setManual(false)
  if(review?.version)try{await request(path+'/choice',sid,'POST',{version:review.version,choice})}catch{setNote('本次提醒选择暂未保存，支付验证仍按原流程进行。')}
  if(choice==='modify')onModify?.();if(choice==='support')onSupport?.()
 }
 if(!review)return <p className="risk-status" role="status">正在读取辅助核查信息…</p>
 return <><p className="risk-status" role="status">{review.state==='unavailable'?'辅助提醒暂不可用，请自行核实收款信息。':review.state==='attention'?'这笔转账有待核实的线索。':'现有数据未触发提醒，这不代表交易已被判定安全。'}{note&&<span>{note}</span>}{review.state==='attention'&&<button onClick={()=>{setManual(true);setClosed(false)}}>查看依据</button>}</p>
 {review.state==='attention'&&(manual||!review.choice)&&!closed&&<BottomSheet title="转账前，再核实一下" onClose={()=>void choose('dismiss')}><div className="risk-symbol"><MobileIcon name="shield" size={36}/></div><h2>转账前，再核实一下</h2><p className="sheet-lead">以下是本次实际发现的线索：</p><ul className="risk-reasons">{review.signals.map(s=><li key={s.code}><MobileIcon name={s.code==='unusual_time'?'clock':'user'} size={22}/><span>{s.label}{s.count!==undefined&&<small>本次统计：{s.count} 笔</small>}{s.threshold_cents!==undefined&&<small>参考上界 ¥{(s.threshold_cents/100).toLocaleString('zh-CN')} · 样本 {s.sample_size} 笔</small>}</span></li>)}{review.semantic_clues.map((s,i)=><li key={s.kind+i}><MobileIcon name="shield" size={22}/><span>{s.label}<small>你提到：“{s.quote}”</small></span></li>)}</ul><p className="risk-disclaimer">提醒不代表交易一定有风险，也不代表已确认诈骗。</p>{review.limitations.length>0&&<details className="risk-coverage"><summary>本次统计覆盖与限制</summary>{review.limitations.map(x=><p key={x}>{x}</p>)}<p>{review.scope}</p></details>}<button className="primary full-width" onClick={()=>void choose('continue')}>继续核对</button><button className="secondary full-width" onClick={()=>void choose('modify')}>返回修改</button><button className="text-button full-width" onClick={()=>void choose('support')}>联系人工客服</button><button className="text-button full-width quiet-action" onClick={()=>void choose('dismiss')}>不采纳建议，继续核对</button><p className="sheet-footnote">付款仍需完成支付验证与最终确认</p></BottomSheet>}</>
}
