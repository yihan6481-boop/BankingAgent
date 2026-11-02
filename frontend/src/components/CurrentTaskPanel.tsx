import { useEffect, useState, type ReactNode } from 'react'
import type { UiState } from '../integrationTypes'
import { StructuredData } from './StructuredData'
import { ResultCardRenderer } from './cards/ResultCardRenderer'
import { StatusLedger } from './StatusLedger'
import type { StatusLayer } from './statusModel'

// Task status comes from the task envelope, never from an unrelated query turn.
export function CurrentTaskPanel({state, busy, onTasks, children, layers}: {state: UiState | null; busy: boolean; onTasks: () => void; children?: ReactNode; layers?: StatusLayer[]}) {
  const [now,setNow]=useState(Date.now())
  useEffect(()=>{const t=window.setInterval(()=>setNow(Date.now()),1000);return()=>clearInterval(t)},[])
  const task = state?.task?.task
  const preview = state?.task?.preview
  const challenge = state?.task?.challenge
  const current = task?.task.steps.find(s => s.step_id === task.task.current_step_id)
  const partial = task?.task.steps.some(s => s.status === 'completed') && task.status !== 'completed'
  const expired = preview && Date.parse(preview.expires_at) <= now
  const label = !task ? (state?.status === 'need_info' ? '待补充' : '暂无办理任务')
    : task.status === 'completed' ? '已完成'
    : task.status === 'failed' ? (partial ? '部分完成' : '办理未完成')
    : task.status === 'cancelled' ? '已取消'
    : task.status === 'paused' ? '已暂停'
    : task.status === 'scheduled' ? '已预约'
    : task.status === 'manual' ? '已接管'
    : expired ? '预览已过期'
    : preview ? (challenge && challenge.status !== 'verified' ? '待验证' : '待确认')
    : current?.status === 'waiting_clarification' ? '待补充' : '待核实状态'
  return <aside className="current-task-panel" aria-label="当前任务详情">
    <div className="task-panel-heading"><span className="eyebrow">当前任务</span><span className="task-status" role="status">{label}</span></div>
    <h2>{task?.task.contract.goal || '从一个需求开始'}</h2>
    <p className="muted-copy">{busy ? '正在等待服务返回。尚未收到核验结果，请勿重复提交。' : task?.status === 'completed' ? '任务状态已完成，业务结果以各步骤回执为准。' : preview ? '核对下方参数，完成必要验证后，再明确确认本次操作。' : task ? '执行响应或结果待核实，请同步状态，不要重复执行。' : '查询结果显示在对话中；办理任务会在这里单独保留。'}</p>
    {partial && <p className="task-notice">已有步骤完成。未完成步骤不能视为成功，已完成业务不会因取消而撤销。</p>}
    {children}
    {layers && layers.length > 0 && <details className="task-state-details"><summary>查看办理状态详情</summary><StatusLedger layers={layers} /></details>}
    {task && <div aria-label="当前任务回执" className="space-y-3 min-w-0">
      {task.task.steps.map(step => {
        const result = step.status === 'completed' && step.result?.status === 'success' ? step.result : null
        const display = result && state?.results.find(r => r.step_id === step.step_id)
        return <section key={step.step_id} className="task-step-receipt">
          <h3>{step.purpose}</h3>
          <p>{step.status === 'completed' ? display ? '步骤已完成 · 服务端回执' : '服务端步骤已完成，但回执暂不可取得' : step.status === 'failed' ? '步骤失败' : step.status === 'skipped' ? '步骤未执行（已跳过）' : step.status === 'cancelled' ? '步骤已取消' : '步骤未完成 · ' + step.status}</p>
          {display && <><details><summary>查看本步骤回执详情</summary>
            {display.business ? <ResultCardRenderer result={display.business} /> : <StructuredData title="服务端步骤结果" data={display.detail} />}
          </details></>}
        </section>
      })}
    </div>}
    <button className="secondary w-full" onClick={onTasks}>查看任务与回执</button>
  </aside>
}
