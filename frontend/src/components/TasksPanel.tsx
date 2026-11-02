import { useState } from 'react'
import { api } from '../api'
import type { Json, TaskAction, TaskView } from '../integrationTypes'
import { timeline } from '../integrationTypes'
import { TaskTimeline } from './cards/InteractionCards'
import { StructuredData } from './StructuredData'
const STATUS: Record<string, string> = { active: '执行中', waiting: '待确认 / 验证', paused: '已暂停', scheduled: '已预约', manual: '已接管', completed: '已完成', cancelled: '已取消', failed: '失败' }
const PRIORITY:Record<string,number>={waiting:0,active:1,paused:2,manual:3,scheduled:4,failed:5,completed:6,cancelled:7}
const FIELDS: Record<string, string> = { amount: '金额（元）', daily_limit: '每日限额（元）', recipient_name: '收款人姓名',
  recipient_id: '收款人编号', from_card_id: '付款卡编号', card_id: '银行卡编号', product_id: '理财产品编号', holding_id: '持仓编号',
  subscription: '订阅名称', subscription_id: '订阅编号', month: '月份', remark: '备注' }
export function TasksPanel({ sid, tasks, busy, onAction, run, onSync }: {
  sid: string; tasks: TaskView[]; busy: boolean
  onAction: (task: TaskView, action: TaskAction, values?: Record<string, unknown>) => Promise<unknown>
  run: <T,>(sid: string | null, fn: () => Promise<T>, sync?: boolean) => Promise<T | null>; onSync: () => Promise<unknown>
}) {
  const [filter,setFilter]=useState('all')
  const groups:Record<string,string[]>={pending:['waiting','paused','scheduled','manual'],active:['active'],completed:['completed'],closed:['failed','cancelled']}
  const [selected, setSelected] = useState('')
  const task = tasks.find(t => t.task.task_id === selected)
  const [stepId, setStepId] = useState('')
  const [field, setField] = useState('')
  const [value, setValue] = useState('')
  const [at, setAt] = useState('')
  const [reason, setReason] = useState('')
  const [audit, setAudit] = useState<Json | null>(null)
  const [modifyOpen, setModifyOpen] = useState(false)
  const [cancelCheck, setCancelCheck] = useState(false)
  const step = task?.task.steps.find(s => s.step_id === stepId)
  const available = Object.keys(step?.arguments ?? {}).filter(k => k in FIELDS)
  const terminal = task && ['completed', 'cancelled'].includes(task.status)
  const act = async (action: TaskAction, values: Record<string, unknown> = {}) => {
    if (!task) return
    setCancelCheck(false)
    await onAction(task, action, { ...values, ...(reason ? { reason } : {}) })
  }
  const modify = () => {
    if (!step || !field || !value.trim()) return
    const parsed = ['recipient_id', 'from_card_id', 'card_id', 'product_id', 'holding_id', 'subscription_id'].includes(field) ? Number(value) : value.trim()
    void act('modify', { step_id: step.step_id, changes: { [field]: parsed } })
  }
  return <div className="mobile-page tasks-page space-y-3">
    <div className="flex justify-between"><h2 className="font-semibold">正式任务与回执</h2><button className="secondary" disabled={busy} onClick={() => void onSync()}>同步任务</button></div>
    <p className="text-xs text-slate-500">任务保存在后端。重启后重新登录，再明确恢复；旧确认和验证码不会恢复。</p>
    {!tasks.length && <p className="text-slate-400">暂无任务。</p>}
    <div className="task-filters" aria-label="任务分类">{[['all','全部'],['pending','待处理'],['active','进行中'],['completed','已完成'],['closed','失败 / 取消']].map(([key,label])=><button className="secondary" aria-pressed={filter===key} key={key} onClick={()=>setFilter(key)}>{label}</button>)}</div>
    {tasks.length>0&&!tasks.some(t=>filter==='all'||groups[filter]?.includes(t.status))&&<p className="task-notice">此分类暂无任务。</p>}
    {tasks.filter(t=>filter==='all'||groups[filter]?.includes(t.status)).sort((a,b)=>(PRIORITY[a.status]??8)-(PRIORITY[b.status]??8)).map(t => <button data-task-id={t.task.task_id} key={t.task.task_id} disabled={busy} onClick={() => { setSelected(t.task.task_id); setStepId(''); setField(''); setValue(''); setAudit(null); setModifyOpen(false); setCancelCheck(false) }}
      className={`w-full text-left p-3 rounded-xl border ${selected === t.task.task_id ? 'bg-webank-50 border-webank-200' : 'bg-white border-slate-100'}`}>
      <div className="font-medium break-words">{t.task.contract.goal || t.task.steps.map(s => s.purpose).join(' → ')}</div><div className="text-xs text-slate-500 mt-1">{STATUS[t.status]} · {t.scheduled_at ? new Date(t.scheduled_at).toLocaleString('zh-CN') : `${t.task.steps.length} 个步骤`}</div>
    </button>)}
    {task && <section className="space-y-3 border-t pt-3">
      <TaskTimeline task={timeline(task)} />
      <p role="status" className="text-xs text-slate-600">{STATUS[task.status]}。已完成业务保留；预览只表示待办理，资金成功以核验回执为准。</p>
      {task.reason && <p className="text-xs text-slate-500">原因：{task.reason}</p>}
      {task.notes.map((n, i) => <p key={i} className="text-xs text-slate-500">接管备注：{n}</p>)}
      {!terminal && <div className="space-y-3">
        <div className="flex flex-wrap gap-2">
          {task.status !== 'manual' && task.status !== 'scheduled' && <button className="primary" disabled={busy} onClick={() => void act('resume')}>恢复并重新预览</button>}
          </div><details className="task-advanced"><summary>更多操作：暂停、接管与参数修改</summary><div className="flex flex-wrap gap-2 mt-3">
          {task.status !== 'paused' && task.status !== 'manual' && <button className="secondary" disabled={busy} onClick={() => void act('pause')}>暂停</button>}
          {task.status !== 'manual' && <button className="secondary" disabled={busy} onClick={() => void act('takeover')}>手动接管</button>}
          {task.status === 'manual' && <button className="primary" disabled={busy} onClick={() => void act('return')}>交回 Agent</button>}
          <button className="secondary" disabled={busy} onClick={() => setModifyOpen(v => !v)}>修改未执行参数</button>
          <button className="secondary text-rose-600" disabled={busy} onClick={() => setCancelCheck(true)}>取消任务</button>
        </div></details>
        {cancelCheck && <div className="bg-rose-50 p-3 rounded-xl space-y-2"><p className="text-xs">取消仅停止未完成步骤，已提交业务会保留。</p><button className="primary" disabled={busy} onClick={() => void act('cancel')}>明确取消这个任务</button><button className="secondary ml-2" onClick={() => setCancelCheck(false)}>返回</button></div>}
        <input aria-label="任务操作原因" className="field w-full" placeholder="操作原因 / 备注（可选）" maxLength={300} value={reason} onChange={e => setReason(e.target.value)} disabled={busy} />
        {task.status === 'manual' && <button className="secondary" disabled={busy || !reason.trim()} onClick={() => void act('note', { note: reason.trim() })}>保存接管备注</button>}
        {modifyOpen && <div className="bg-white rounded-xl border p-3 space-y-2">
          <select aria-label="修改步骤" className="field w-full" value={stepId} onChange={e => { setStepId(e.target.value); setField(''); setValue('') }}>
            <option value="">选择未执行步骤</option>{task.task.steps.filter(s => s.status !== 'completed').map(s => <option key={s.step_id} value={s.step_id}>{s.purpose}</option>)}
          </select>
          <select aria-label="修改字段" className="field w-full" value={field} onChange={e => { setField(e.target.value); setValue(String(step?.arguments[e.target.value] ?? '')) }}>
            <option value="">选择参数</option>{available.map(f => <option key={f} value={f}>{FIELDS[f]}</option>)}
          </select>
          <input aria-label="新参数值" className="field w-full" value={value} onChange={e => setValue(e.target.value)} placeholder="新参数值" disabled={busy} />
          <button className="primary" disabled={busy || !step || !field || !value.trim()} onClick={modify}>保存修改并使旧预览失效</button>
          <p className="text-xs text-slate-500">修改会使旧预览失效；请明确恢复并重新预览，再确认。原有条件、依赖及金额上限由后端保留。</p>
        </div>}
        <details className="rounded-xl bg-white border p-3"><summary className="cursor-pointer text-xs">预约 / 回退未执行步骤</summary><div className="mt-3 space-y-2">
          <input aria-label="预约时间" type="datetime-local" className="field w-full" value={at} onChange={e => setAt(e.target.value)} disabled={busy} />
          <button className="secondary" disabled={busy || !at || task.status === 'manual'} onClick={() => void act('schedule', { at: new Date(at).toISOString() })}>预约一次</button>
          <p className="text-xs text-slate-500">到期需后端到期处理器；写业务仍需恢复、确认和必要验证。</p>
          <button className="secondary" disabled={busy || !reason.trim()} onClick={() => void act('rollback', { reason: reason.trim() })}>回退未执行步骤</button>
          <p className="text-xs text-slate-500">回退不会撤销已完成扣款；请先填写原因。</p>
        </div></details>
      </div>}
      <button className="secondary" disabled={busy} onClick={async () => {
        const out = await run(sid, () => api.audit(sid, task.task.task_id), false)
        if (out) setAudit(out as unknown as Json)
      }}>查看脱敏审计</button>
      {audit && <StructuredData title="任务审计" data={audit} />}
      {task.task.steps.filter(s => s.status === 'completed' && s.result?.status === 'success').map(s => <StructuredData key={s.step_id} title={s.purpose + ' · 已核验结果'} data={s.result!.data} />)}
    </section>}
  </div>
}
