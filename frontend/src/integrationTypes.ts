import type { UiResult, UiTask, UiStatus } from './types'
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json }
export interface Login {
  session_id: string
  principal: { user_id: number; display_name: string; principal_id: string }
  credential_expires_at: string
}
export interface ToolResult { status: 'success' | 'error'; data: Json; message: string; error_code: string | null }
export interface TaskStep {
  step_id: string; tool_name: string; purpose: string; arguments: Record<string, Json>
  depends_on: string[]; status: string; result: ToolResult | null; error_code: string | null
}
export interface TaskView {
  revision: number; mode: 'single' | 'workflow'
  status: 'active' | 'waiting' | 'paused' | 'scheduled' | 'manual' | 'cancelled' | 'completed' | 'failed'
  scheduled_at: string | null; reason: string | null; notes: string[]
  task: { task_id: string; contract: { goal: string }; status: string; current_step_id: string | null; steps: TaskStep[] }
}
export interface Preview {
  preview_id: string; step_id: string; operation_id: string; action: string
  issued_revision: number; generation: number; execution_digest: string
  permission_level: 'green' | 'yellow' | 'red'
  required_authentication: 'confirmation' | 'totp' | 'payment'
  confirmation_phrase: string | null; confirmation_message: string
  requested_arguments: Record<string, Json>; expires_at: string
}
export interface Challenge {
  challenge_id: string; method: 'none' | 'confirmation' | 'mock_otp' | 'totp' | 'payment'
  status: 'pending' | 'verified' | 'consumed' | 'expired' | 'cancelled' | 'failed'
  attempts_remaining: number; expires_at: string
}
export interface SecureTask { task: TaskView; preview: Preview | null; challenge: Challenge | null }
export interface Factor {
  mode: 'demo' | 'secure'
  bound: boolean
  confirmed: boolean
  /** C line additive recovery view for a first-time, unconfirmed enrollment. */
  enrollment_pending?: boolean
  enrollment_expires_at?: string | null
  enrollment_expired?: boolean
  can_reenroll?: boolean
}
export interface DisplayResult { step_id: string | null; title: string; business: UiResult | null; detail: Json }
export interface UiState {
  contract_version: 'ui-integration-1.0'; display_name: string
  status: UiStatus | 'need_verification' | 'paused' | 'scheduled' | 'manual'
  message: string; missing_fields: string[]; task: SecureTask | null; results: DisplayResult[]
  factor: Factor; conversation_id: string | null; memory_status: string
  turn_id?: string | null
}
export interface NLResponse {
  contract_version: 'nl-1.0'; action: string; message: string
  tool_name: string | null; result: ToolResult | null; error_code: string | null; task: SecureTask | null
  turn_id?: string | null; displays?: DisplayResult[]
  context_source?: 'uploaded' | 'bank' | null
}
export interface Binding { sessionId: string; taskId: string; revision: number; previewId: string; challengeId: string | null }
export interface MemoryMessage {
  id: string; conversation_id: string; role: 'user' | 'assistant'; content: string; created_at: string; task_refs?: string[]
}
export type TaskAction = 'pause' | 'resume' | 'cancel' | 'takeover' | 'return' | 'modify' | 'schedule' | 'note' | 'rollback'
export interface ReadEnvelope {
  status: string; message: string; result: UiResult | null
  error: { code: string; message: string } | null
}
export interface Plan {
  contract: { contract_version: '1.0'; goal: string; domains: string[]; requested_tools: string[]; constraints: string[] }
  steps: { step_id: string; purpose: string; tool_name: string; arguments: Record<string, Json>; depends_on: string[];
      condition?: { source_step_id: string; field_path: string; operator: 'lt' | 'lte' | 'gt' | 'gte' | 'eq' | 'ne'; value: number | string | boolean } }[]
}
export function bindingOf(sessionId: string, task: SecureTask | null): Binding | null {
  if (!task?.preview) return null
  return { sessionId, taskId: task.task.task.task_id, revision: task.task.revision,
    previewId: task.preview.preview_id, challengeId: task.challenge?.challenge_id ?? null }
}
export function sameBinding(a: Binding | null, b: Binding | null): boolean {
  return !!a && !!b && a.sessionId === b.sessionId && a.taskId === b.taskId &&
    a.revision === b.revision && a.previewId === b.previewId && a.challengeId === b.challengeId
}
export function timeline(task: TaskView): UiTask {
  const steps = task.task.steps.map(s => ({ step_id: s.step_id, title: s.purpose,
    tool_name: s.tool_name, status: (s.status === 'waiting_verification' ? 'waiting_confirmation' : s.status) as UiTask['steps'][number]['status'],
    error_code: s.error_code }))
  const status = task.task.status === 'waiting_verification' ? 'waiting_confirmation' : task.task.status
  return { task_id: task.task.task_id, status: status as UiTask['status'], current_step_id: task.task.current_step_id, steps }
}
