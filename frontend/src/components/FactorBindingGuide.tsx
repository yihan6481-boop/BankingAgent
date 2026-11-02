import type { FactorRecovery } from './factorRecovery'
import type { Factor, SecureTask, TaskView } from '../integrationTypes'

/**
 * Persistent first-binding / recovery banner.
 *
 * The old binding button lived inside SecureActionCard, which only renders when a
 * preview exists. A strong-auth attempt made before binding is rejected with
 * `factor_not_bound` and may leave the task without a preview, so the guide was
 * unreachable. This banner is rendered next to the chat/task panel regardless of
 * preview presence. Binding never resumes or confirms a payment automatically.
 */
export function FactorBindingGuide({ recovery, factor, tasks, task, busy, onOpenSecurity, onSync, onResumeTask }: {
  recovery: FactorRecovery
  factor: Factor | undefined
  tasks: TaskView[]
  task: SecureTask | null
  busy: boolean
  onOpenSecurity: () => void
  onSync: () => Promise<unknown> | void
  onResumeTask: (task: TaskView) => Promise<unknown> | void
}) {
  if (!recovery.needed) return null
  const confirmed = !!factor?.confirmed
  const bound = !!factor?.bound
  const blocked = recovery.taskId ? (tasks.find(t => t.task.task_id === recovery.taskId) || (task?.task.task.task_id === recovery.taskId ? task.task : null)) : null
  return <section className="factor-guide" aria-label="安全验证绑定引导" data-recovery={recovery.reason || (confirmed ? 'confirmed' : bound ? 'unconfirmed' : 'unbound')}>
    <div className="factor-guide-head">
      <strong>{confirmed ? '验证器已绑定' : bound ? '验证器待确认' : '需要绑定独立验证器'}</strong>
      <span className={`status-layer-state ${confirmed ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-800'}`}>
        {confirmed ? '已确认' : bound ? '已生成，未确认' : '未绑定'}
      </span>
    </div>
    <p>{recovery.message || '安全模式下，强验证操作需要独立验证器。'}</p>
    {recovery.goal && <p className="factor-guide-goal">原任务：{recovery.goal}{recovery.taskStatus ? `（服务端状态：${recovery.taskStatus}）` : ''}</p>}
    <div className="factor-guide-actions">
      {!confirmed && <button type="button" className="primary" disabled={busy} onClick={onOpenSecurity}>{bound ? '去确认验证器绑定' : '去绑定独立验证器'}</button>}
      {confirmed && blocked && <button type="button" className="primary" disabled={busy} onClick={() => void onResumeTask(blocked)}>回读并重新预览原任务</button>}
      <button type="button" className="secondary" disabled={busy} onClick={() => void onSync()}>同步原任务状态</button>
    </div>
    <p className="factor-guide-note">{confirmed
      ? '绑定完成不会自动继续或付款。原任务需要重新读取并重新预览；旧验证码和旧确认均已失效。'
      : '绑定完成后回到本页重新读取原任务。当前时间点不会执行任何付款。'}</p>
  </section>
}
