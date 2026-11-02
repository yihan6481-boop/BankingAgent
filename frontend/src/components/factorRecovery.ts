/**
 * First-binding recovery logic.
 *
 * Background: a strong-auth operation attempted before the independent
 * authenticator is bound is rejected (`factor_not_bound`) and the target task
 * may be saved as failed without a preview. The binding guide used to live only
 * inside the action card, which renders only when a preview exists - so the user
 * could never reach it. This module detects that situation and tells the UI what
 * to offer. It never decides that a payment succeeded and never auto-confirms.
 */
import type { Factor, SecureTask, TaskView } from '../integrationTypes'

export interface RecoveryTurn {
  status?: string
  error_code?: string | null
  calls?: { result?: { code?: string | null } | null; resolution?: unknown }[]
}

export interface RecoveryInput {
  factor?: Factor | null
  tasks?: TaskView[]
  task?: SecureTask | null
  turns?: RecoveryTurn[]
}

export interface FactorRecovery {
  /** true when the UI must show the binding guide and a path back to the task. */
  needed: boolean
  /** factor_not_bound / factor_not_confirmed when detected from server state. */
  reason: 'factor_not_bound' | 'factor_not_confirmed' | null
  /** Task whose target should be resumed after binding; null when unknown. */
  taskId: string | null
  goal: string | null
  /** Task status as reported by the server, never inferred. */
  taskStatus: string | null
  /** Whether a fresh preview already exists (then the normal action card owns the flow). */
  hasPreview: boolean
  message: string
}

const FACTOR_CODES = new Set(['factor_not_bound', 'factor_not_confirmed'])

function taskBlockedCode(task: TaskView): 'factor_not_bound' | 'factor_not_confirmed' | null {
  for (const step of task.task.steps) {
    const code = step.error_code || ''
    if (FACTOR_CODES.has(code)) return code as 'factor_not_bound' | 'factor_not_confirmed'
    // A step result envelope can carry the same code even when step.error_code is absent.
    if (step.result?.status === 'error') {
      const data = step.result.data as { code?: unknown } | null
      const resultCode = data && typeof data === 'object' && typeof data.code === 'string' ? data.code : ''
      if (FACTOR_CODES.has(resultCode)) return resultCode as 'factor_not_bound' | 'factor_not_confirmed'
    }
  }
  return null
}

export function factorRecovery(input: RecoveryInput): FactorRecovery {
  const factor = input.factor || null
  const confirmed = !!factor?.confirmed
  const secureMode = factor?.mode === 'secure'
  const empty: FactorRecovery = {
    needed: false, reason: null, taskId: null, goal: null, taskStatus: null, hasPreview: false,
    message: '',
  }

  // A failure caused by an unbound factor is only actionable while the factor is
  // still unconfirmed; once confirmed the user should simply re-read the task.
  let detected: { reason: 'factor_not_bound' | 'factor_not_confirmed'; task: TaskView | null } | null = null
  const liveTask = input.task?.task || null
  if (liveTask) {
    const code = taskBlockedCode(liveTask)
    if (code) detected = { reason: code, task: liveTask }
  }
  if (!detected) {
    const candidates = (input.tasks || []).filter(t => ['waiting', 'failed', 'paused'].includes(t.status))
    for (const task of candidates) {
      const code = taskBlockedCode(task)
      if (code) { detected = { reason: code, task }; break }
    }
  }
  if (!detected) {
    for (const turn of input.turns || []) {
      if (FACTOR_CODES.has(turn.error_code || '')) {
        detected = { reason: turn.error_code as 'factor_not_bound' | 'factor_not_confirmed', task: liveTask }
        break
      }
      const call = (turn.calls || []).find(c => FACTOR_CODES.has(c?.result?.code || ''))
      if (call) {
        detected = { reason: call.result!.code as 'factor_not_bound' | 'factor_not_confirmed', task: liveTask }
        break
      }
    }
  }
  if (!detected && !secureMode) return empty
  if (!detected) {
    // No blocker detected; without an unconfirmed factor there is nothing to recover.
    if (confirmed || !secureMode) return empty
    return { ...empty, needed: true, message: '当前为安全模式；绑定独立验证器后才能完成强验证操作。' }
  }
  if (confirmed) {
    // The factor exists now. The remaining path is re-reading and re-previewing
    // the original task; the UI shows the normal task recovery buttons.
    return {
      ...empty,
      needed: true,
      reason: detected.reason,
      taskId: detected.task?.task.task_id ?? null,
      goal: detected.task?.task.contract.goal ?? null,
      taskStatus: detected.task?.status ?? null,
      message: '验证器已绑定。原任务不会自动继续；请回读原任务并重新预览，确认前不会执行。',
    }
  }
  const reason = detected.reason
  return {
    ...empty,
    needed: true,
    reason,
    taskId: detected.task?.task.task_id ?? null,
    goal: detected.task?.task.contract.goal ?? null,
    taskStatus: detected.task?.status ?? null,
    hasPreview: !!input.task?.preview,
    message: reason === 'factor_not_bound'
      ? '这笔业务需要独立验证器，但尚未绑定。请先完成绑定；绑定不会自动付款，完成后回到原任务重新预览。'
      : '独立验证器已生成但尚未确认。请用验证器动态码完成确认；完成后回到原任务重新预览。',
  }
}
