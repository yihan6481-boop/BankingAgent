import type {Dialogue} from '../store/useDialogue'
import {StructuredData} from './StructuredData'
import {TaskTimeline} from './cards/InteractionCards'
import {timeline} from '../integrationTypes'
import type {Json,TaskView} from '../integrationTypes'

const DRAFT_STATE: Record<string,string> = {open:'未关联任务',preparing:'准备中（结果未知）',linked:'已关联任务',closed:'已关闭'}
const FIELD_LABEL:Record<string,string>={from_card_id:'付款卡',amount:'金额',recipient_label:'收款人称呼',recipient_id:'收款人编号',recipient_label_or_id:'收款人',remark:'附言'}
export function DialogueState({dialogue,busy,onResume,onAuthorize,onApplyOperation,includeTasks=true}:{dialogue:Dialogue|null;busy:boolean;onResume:(t:TaskView)=>unknown;onAuthorize:(tid:string,call:string)=>unknown;onApplyOperation?:(tid:string,call:string)=>unknown;includeTasks?:boolean}){
  void onAuthorize // Per-message consent/result cards live beside that answer.
  const turns=dialogue?.turns||[]
  const items=dialogue?.state.items||[]
  const groups=[{title:'正式任务',items:includeTasks?items.filter(x=>x.kind==='task'):[]},{title:'当前对话草稿',items:items.filter(x=>['transfer','query'].includes(x.kind)&&x.status!=='linked')},{title:'已关联任务的来源',items:items.filter(x=>['transfer','query'].includes(x.kind)&&x.status==='linked')},{title:'历史查询结果',items:items.filter(x=>x.kind==='result')}]
  return <>
  {turns.flatMap(t=>t.calls.filter(c=>c.result?.code==='explicit_action_required').map(c=><section key={'operation'+t.id+c.call_id} className="task-notice" aria-label="待核对操作">
    <strong>待核对操作：{({draft_close:'关闭草稿',transfer_patch:'修改转账草稿',query_patch:'修改查询草稿',task_modify:'修改任务参数',task_cancel:'取消任务',task_pause:'暂停任务',task_resume:'恢复任务',task_schedule:'预约任务',task_return:'交还任务',task_note:'添加任务备注',task_takeover:'人工接管',task_rollback:'回到未提交步骤',memory_replace:'更正记忆',memory_delete:'删除记忆',memory_save_business:'保存历史业务摘要到记忆'} as Record<string,string>)[c.name]||c.name}</strong>
    <p>请核对具体目标、版本与修改内容。点击仅应用下列操作，资金执行仍需独立确认。</p>
    <StructuredData title="目标与参数" data={c.arguments as Json}/>
    {c.resolution?<StructuredData title="操作核实记录" data={c.resolution as Json}/>:onApplyOperation&&<button disabled={busy} onClick={()=>void onApplyOperation(t.id,c.call_id)}>应用此操作</button>}
  </section>))}
  {groups.filter(g=>g.items.length>0).map(group=><section className="dialogue-state-group" key={group.title}><h3>{group.title}</h3>{group.items.map(x=>x.kind==='task'?<section key={x.reference} className="task-notice" aria-label="任务状态">
    <TaskTimeline task={timeline(x as TaskView)}/>
    <StructuredData title="当前任务参数" data={x.task.steps.map((s:any)=>({业务:s.purpose,状态:s.status,参数:s.arguments})) as Json}/>
    {['waiting','paused','active'].includes(x.status)&&<button className="mobile-secondary" disabled={busy} onClick={()=>void onResume(x as TaskView)}>恢复并重新预览当前任务</button>}
  </section>:['transfer','query','result'].includes(x.kind)?<section key={x.reference} className="task-notice" aria-label="已保存目标">
    <strong>{x.kind==='transfer'?'转账草稿':x.kind==='query'?'查询草稿':'历史查询结果'}</strong>
    <details className="text-xs text-slate-500"><summary>查看记录标识</summary><p className="saved-reference">引用 {String(x.reference||'')} · rev {String(x.revision??'?')} · 状态：{DRAFT_STATE[String(x.status)]||String(x.status||'未知')}</p></details>
    {x.status==='linked'?<p>已关联任务。草稿只是来源记录，当前参数、授权与结果以任务状态为准，不以草稿显示为付款成功。</p>:<>
      <StructuredData title="已保存字段" data={x.fields as Json}/>
      {!!x.missing_fields?.length&&<p>仍需补充：{x.missing_fields.map((f:string)=>FIELD_LABEL[f]||f).join('、')}</p>}
      {x.status==='preparing'&&<p role="alert">准备操作结果未知：请先核实原任务（task.verify / task.recover），不要重新准备或重复付款。</p>}
      {x.ready===false&&!!x.field_errors?.length&&<p>校验未通过：{x.field_errors.map((e:any)=>FIELD_LABEL[e?.field]||String(e?.field||'字段')).join('、')}</p>}
    </>}
  </section>:null)}</section>)}</>
}
