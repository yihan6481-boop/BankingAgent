/**
 * Layered business status model.
 *
 * The UI must never collapse the dialogue turn, saved draft, task, current step,
 * authorization material and canonical receipt into one "success" state:
 * - a completed turn only means the assistant finished answering;
 * - a completed task only means every step finished, the money fact still has to
 *   be proven by a canonical receipt;
 * - a failed turn does not mean the business operation failed.
 *
 * This module is pure so it can be exercised without a browser.
 */
import type { Challenge, Preview, SecureTask, TaskView, UiState } from '../integrationTypes'

export interface DraftItem {
  reference?: unknown
  revision?: unknown
  kind?: unknown
  status?: unknown
  fields?: unknown
  missing_fields?: unknown
  candidates?: unknown
  ready?: unknown
}

export interface TurnFacts {
  id?: string
  status?: string
  error_code?: string | null
  progress?: string | null
}

export interface StatusLayer {
  id: 'turn' | 'draft' | 'task' | 'step' | 'authorization' | 'receipt'
  label: string
  state: string
  detail: string
  tone: 'idle' | 'info' | 'ok' | 'warn' | 'bad'
}

export interface StatusInput {
  turn?: TurnFacts | null
  task?: SecureTask | null
  drafts?: DraftItem[]
  factor?: { confirmed?: boolean; mode?: string } | null
  results?: UiState['results'] | null
  now?: number
}

const TONES: Record<string, StatusLayer['tone']> = {
  idle: 'idle', info: 'info', ok: 'ok', warn: 'warn', bad: 'bad',
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}

/** Only canonical, server-verified step receipts may produce a receipt layer. */
export function verifiedReceipts(task: TaskView | null | undefined): { stepId: string; purpose: string }[] {
  if (!task) return []
  return task.task.steps
    .filter(step => step.status === 'completed' && step.result?.status === 'success')
    .map(step => ({ stepId: step.step_id, purpose: step.purpose }))
}

const TURN_LABEL: Record<string, string> = {
  running: '对话请求进行中',
  completed: '对话请求已完成',
  interrupted: '对话请求已中断',
  failed: '对话请求失败',
  unknown: '对话请求结果未知',
}

export function turnLayer(turn: TurnFacts | null | undefined): StatusLayer {
  const base = { id: 'turn' as const, label: '对话请求' }
  if (!turn) return { ...base, state: '无活动轮次', detail: '查询与办理共用对话入口；本轮没有可读取的对话轮次。', tone: TONES.idle }
  const status = String(turn.status || 'unknown')
  const state = TURN_LABEL[status] || `对话请求状态：${status}`
  if (turn.error_code) return { ...base, state, detail: `本轮返回错误码 ${turn.error_code}。这不代表资金已执行或失败；业务结果以任务/回执为准。`, tone: TONES.bad }
  if (status === 'running') return { ...base, state, detail: `服务端进度：${turn.progress || '等待服务端返回'}。等待期间不要重复提交。`, tone: TONES.info }
  if (['interrupted', 'failed', 'unknown'].includes(status)) return { ...base, state, detail: '本轮未完整结束。请先核实/恢复原轮次，不要换请求重提资金操作。', tone: TONES.warn }
  return { ...base, state, detail: '仅表示本轮对话处理结束，不代表任何资金操作完成。', tone: TONES.ok }
}

export function draftLayer(drafts: DraftItem[] | undefined): StatusLayer {
  const base = { id: 'draft' as const, label: '已保存草稿' }
  const items = (drafts || []).filter(isRecord)
  if (!items.length) return { ...base, state: '无草稿', detail: '本轮没有已保存的业务草稿；任务参数以当前任务与预览为准。', tone: TONES.idle }
  const linked = items.filter(d => d.status === 'linked')
  const preparing = items.filter(d => d.status === 'preparing')
  const open = items.filter(d => !['linked', 'closed', 'preparing'].includes(String(d.status)))
  const format = (d: DraftItem) => {
    const fields = isRecord(d.fields) ? d.fields : {}
    const missing = Array.isArray(d.missing_fields) ? d.missing_fields.filter((x): x is string => typeof x === 'string') : []
    return `草稿 ${String(d.reference ?? '')} · rev ${String(d.revision ?? '?')} · ${String(d.status ?? '未知')}` +
      (missing.length ? ` · 仍需补充：${missing.join('、')}` : '') +
      (Object.keys(fields).length ? ` · 字段 ${Object.keys(fields).join('、')}` : '')
  }
  if (preparing.length) return { ...base, state: '草稿准备中/结果未知', detail: `${preparing.map(format).join('；')}。准备操作不付款；请先核实原任务，不要重复准备。`, tone: TONES.warn }
  if (open.length) return { ...base, state: `未完成草稿 ${open.length} 个`, detail: `${open.map(format).join('；')}。草稿不是授权，也不是交易结果。`, tone: TONES.info }
  return { ...base, state: `已关联 ${linked.length} 个草稿`, detail: `${linked.map(format).join('；')}。关联草稿只保留来源；最新参数与授权以当前任务为准。`, tone: TONES.info }
}

export function taskLayer(task: SecureTask | null | undefined): StatusLayer {
  const base = { id: 'task' as const, label: '办理任务' }
  if (!task) return { ...base, state: '暂无办理任务', detail: '查询结果不会覆盖待确认的办理任务。', tone: TONES.idle }
  const value = task.task
  const status = value.status
  const labels: Record<string, string> = {
    active: '任务进行中', waiting: '任务等待确认或验证', paused: '任务已暂停', scheduled: '任务已预约', manual: '任务已人工接管',
    cancelled: '任务已取消', completed: '任务已结束', failed: '任务未完成',
  }
  const detail = status === 'completed'
    ? '任务结束只说明步骤已处理；是否付款成功必须看下方已核验回执。'
    : status === 'failed'
      ? '任务未完成。已完成的步骤不会撤销；请同步核对，不要重复提交资金操作。'
      : status === 'waiting'
        ? '任务等待当前授权。旧预览、旧验证码不能复用。'
        : '任务状态来自服务端任务视图，不与对话轮次混用。'
  return { ...base, state: `${labels[status] || status}（rev ${value.revision}）`, detail, tone: status === 'completed' ? TONES.ok : status === 'failed' ? TONES.bad : TONES.warn }
}

export function stepLayer(task: TaskView | null | undefined): StatusLayer {
  const base = { id: 'step' as const, label: '当前步骤' }
  if (!task) return { ...base, state: '无步骤', detail: '没有正在执行的步骤。', tone: TONES.idle }
  const steps = task.task.steps
  const currentId = task.task.current_step_id
  const current = steps.find(s => s.step_id === currentId) || steps.find(s => !['completed', 'skipped', 'cancelled'].includes(s.status))
  if (!current) return { ...base, state: '无待执行步骤', detail: '所有步骤均已结束，回执以下方为准。', tone: TONES.idle }
  const labels: Record<string, string> = {
    pending: '待执行', running: '执行中', waiting_verification: '等待强验证', waiting_confirmation: '等待确认',
    waiting_clarification: '等待补充信息', completed: '已完成', failed: '失败', skipped: '已跳过', cancelled: '已取消',
  }
  const detail = current.error_code
    ? `步骤返回错误码 ${current.error_code}；任务整体状态见上，回执不因步骤失败而生成。`
    : current.status === 'waiting_verification'
      ? '正在等待本笔强验证；验证通过后仍需单独确认。'
      : '步骤状态来自任务视图，不根据对话回复推断。'
  return { ...base, state: `${current.purpose} · ${labels[current.status] || current.status}`, detail, tone: current.status === 'failed' ? TONES.bad : current.status === 'completed' ? TONES.ok : TONES.warn }
}

export function authorizationLayer(task: SecureTask | null | undefined, factor: { confirmed?: boolean; mode?: string; bound?: boolean; enrollment_expired?: boolean } | null | undefined, now = Date.now()): StatusLayer {
  const base = { id: 'authorization' as const, label: '当前授权要求' }
  const preview: Preview | null = task?.preview || null
  const challenge: Challenge | null = task?.challenge || null
  if (!preview) {
    if (task?.task.status === 'failed' || task?.task.status === 'waiting') {
      return { ...base, state: '无有效预览', detail: '当前没有可用的操作预览，旧授权不可恢复；请重新读取任务并取得新预览。', tone: TONES.warn }
    }
    return { ...base, state: '无授权要求', detail: '没有待确认的操作预览。', tone: TONES.idle }
  }
  const expires = Date.parse(preview.expires_at)
  const expired = Number.isFinite(expires) && expires <= now
  if (expired) return { ...base, state: '预览已过期', detail: '预览和其上的确认、验证均已失效。请重新预览，不能自动确认。', tone: TONES.bad }
  if (preview.required_authentication === 'totp' && factor && factor.mode === 'secure' && !factor.confirmed) {
    if (factor.enrollment_expired) return { ...base, state: '验证器绑定已过期', detail: '首次绑定已过期且未确认。需要重新生成绑定信息并完成确认；完成后返回原任务重新预览，不会自动付款。', tone: TONES.bad }
    return { ...base, state: factor.bound ? '验证器待确认' : '需要先绑定独立验证器', detail: '该操作要求强验证，但当前账户尚未确认独立验证器；绑定完成后返回原任务重新预览，不会自动付款。', tone: TONES.warn }
  }
  if (challenge && challenge.status === 'pending') {
    const left = Math.max(0, Math.ceil((Date.parse(challenge.expires_at) - now) / 1000))
    return { ...base, state: `等待${challenge.method === 'payment' ? '本笔支付验证' : challenge.method === 'totp' ? '独立验证器动态码' : '模拟验证码'}`, detail: `验证挑战剩余 ${left} 秒，最多还可尝试 ${challenge.attempts_remaining} 次。`, tone: TONES.warn }
  }
  if (challenge && challenge.status === 'expired') return { ...base, state: '验证挑战已过期', detail: '请重新读取任务取得新预览与挑战；不能复用旧验证码。', tone: TONES.bad }
  if (challenge && ['failed', 'cancelled', 'consumed'].includes(challenge.status)) return { ...base, state: `验证挑战${challenge.status === 'consumed' ? '已使用' : challenge.status === 'failed' ? '已失败' : '已取消'}`, detail: '请重新预览后再验证；不要连续重试旧动态码。', tone: TONES.bad }
  if (challenge && challenge.status === 'verified') return { ...base, state: '验证已通过，待确认', detail: '验证已通过但尚未执行；必须再明确确认这一次操作。', tone: TONES.info }
  return { ...base, state: preview.permission_level === 'red' ? '需要强验证' : '需要明确确认', detail: '以最新预览决定授权要求；旧预览不能复用。', tone: TONES.warn }
}

export function receiptLayer(task: TaskView | null | undefined, ui: { results?: UiState['results'] } | null | undefined): StatusLayer {
  const base = { id: 'receipt' as const, label: '回执状态' }
  if (!task) return { ...base, state: '暂无回执', detail: '没有办理任务，因此没有交易回执。', tone: TONES.idle }
  const receipts = verifiedReceipts(task)
  if (!receipts.length) {
    if (task.task.status === 'completed') return { ...base, state: '任务已完成，但回执不可取得', detail: '不能据此显示成功或金额；请从任务详情重新读取回执。', tone: TONES.warn }
    if (task.task.status === 'failed') return { ...base, state: '无成功回执', detail: '任务未完成，未生成成功回执。已有步骤不会撤销。', tone: TONES.bad }
    return { ...base, state: '未生成回执', detail: '尚未执行到可核验步骤。', tone: TONES.idle }
  }
  const projected = receipts.map(r => {
    const display = ui?.results?.find(item => item.step_id === r.stepId)
    return display ? `${r.purpose}（服务端展示结果）` : `${r.purpose}（步骤已核验，展示结果缺失）`
  })
  return { ...base, state: `${receipts.length} 个已核验步骤回执`, detail: `${projected.join('；')}。回执数量=${receipts.length}；未列出的步骤不得视为成功。`, tone: TONES.ok }
}

export function buildStatusLayers(input: StatusInput): StatusLayer[] {
  const uiTask = input.task || null
  return [
    turnLayer(input.turn || null),
    draftLayer(input.drafts),
    taskLayer(uiTask),
    stepLayer(uiTask?.task),
    authorizationLayer(uiTask, input.factor, input.now ?? Date.now()),
    receiptLayer(uiTask?.task, { results: input.results || [] }),
  ]
}

export { TONES }
