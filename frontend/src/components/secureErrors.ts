/**
 * Secure-action error semantics shared by the action card, factor panel and the
 * binding recovery banner. Pure data, no request logic.
 */
import type { Challenge } from '../integrationTypes'

export interface ActionError {
  code: string
  message: string
}

export const SECURE_ERROR_HINT: Record<string, string> = {
  code_replayed: '这个动态码已经使用过或不是最新动态码。请等待验证器显示下一个 30 秒动态码再试；系统不会自动连续重试。',
  invalid_code: '动态码不正确。请核对验证器当前显示的六位码后再试。',
  challenge_expired: '本次验证挑战已过期，旧授权失效。请重新读取任务并重新预览后再验证。',
  challenge_already_used: '这张验证挑战已结束，不能复用。请重新预览后再次验证。',
  challenge_attempts_exhausted: '验证尝试次数已用尽。请重新读取任务、重新预览后再验证。',
  challenge_parameter_changed: '操作参数已变化，旧验证挑战与旧预览一起失效。请重新核对并重新预览。',
  challenge_not_found: '当前操作没有可用的验证挑战，请重新读取任务预览。',
  stale_preview: '预览已变化或失效，旧授权不能执行。请回读任务并重新核对预览。',
  stale_revision: '当前任务版本已变化。请回读任务后再操作，旧预览与旧验证码均失效。',
  document_changed: '单据内容已变化，旧核对与授权失效。请重新核对单据后重新预览。',
  recipient_changed: '收款信息已变化，旧授权失效。请重新核对收款人后再预览。',
  factor_not_bound: '该操作需要独立验证器，但当前账户尚未绑定。请先绑定并确认验证器；完成后返回原任务重新预览，不会自动付款。',
  factor_not_confirmed: '独立验证器已生成但尚未确认。请先输入验证器动态码完成确认。',
  enrollment_expired: '首次绑定的 10 分钟有效期已过，未确认的绑定不能再完成。当前版本没有在线重绑入口，需管理员线下核查后重新绑定。',
  factor_already_bound: '该账户已有独立验证器记录，当前版本不支持在线重绑或找回。若尚未确认，请直接完成确认；已确认的验证器不能被替换。',
  factor_session_mismatch: '绑定流程属于另一个登录会话。请在开始绑定的同一会话内完成确认。',
  factor_key_unavailable: '验证器主密钥不可用，绑定/验证无法完成。请联系运维核查，不能改用短信或模拟验证码。',
  strong_auth_locked: '强验证已被临时锁定。请稍后再试，不要连续提交动态码。',
  unsupported_authentication_method: '当前安全模式不支持这个验证方式，请使用独立验证器入口。',
  session_not_found: '会话已失效，请重新登录后再继续。',
  credential_expired: '登录凭据已过期，请重新登录；未完成任务需从服务端恢复。',
}

export function secureErrorHint(error: ActionError | null | undefined): string {
  if (!error) return ''
  return SECURE_ERROR_HINT[error.code] || error.message || '操作未完成，请回读当前任务状态后再试。'
}

/** Errors that invalidate the current preview/challenge and require a fresh read. */
export const RE_READ_ERRORS = new Set([
  'stale_preview', 'stale_revision', 'challenge_expired', 'challenge_already_used',
  'challenge_attempts_exhausted', 'challenge_parameter_changed', 'challenge_not_found',
  'document_changed', 'recipient_changed', 'pending_action_expired', 'interaction_expired',
])

export const FACTOR_ERRORS = new Set(['factor_not_bound', 'factor_not_confirmed', 'enrollment_expired', 'factor_already_bound'])

export interface ChallengeView {
  /** none = preview has no challenge; unsupported = method not usable here */
  state: 'none' | 'pending' | 'verified' | 'expired' | 'ended' | 'unsupported'
  needsCode: boolean
  canVerify: boolean
  secondsLeft: number | null
  hint: string
}

export function challengeView(challenge: Challenge | null | undefined, now = Date.now()): ChallengeView {
  if (!challenge) return { state: 'none', needsCode: false, canVerify: false, secondsLeft: null, hint: '' }
  if (!['totp', 'mock_otp'].includes(challenge.method)) {
    return { state: 'unsupported', needsCode: false, canVerify: false, secondsLeft: null, hint: '该验证方式在本页不可用；请使用独立安全入口，不能改用其他验证方式绕过。' }
  }
  const expires = Date.parse(challenge.expires_at)
  const secondsLeft = Number.isFinite(expires) ? Math.max(0, Math.ceil((expires - now) / 1000)) : null
  if (challenge.status === 'pending') {
    if (secondsLeft === 0) return { state: 'expired', needsCode: false, canVerify: false, secondsLeft: 0, hint: SECURE_ERROR_HINT.challenge_expired }
    return { state: 'pending', needsCode: true, canVerify: true, secondsLeft, hint: challenge.method === 'totp' ? '从手机验证器读取当前六位动态码；验证通过后仍需单独确认。' : '本次为模拟 OTP，不代表真实短信。' }
  }
  if (challenge.status === 'verified') return { state: 'verified', needsCode: false, canVerify: false, secondsLeft, hint: '验证已通过但尚未执行，请明确确认这一次操作。' }
  if (challenge.status === 'expired') return { state: 'expired', needsCode: false, canVerify: false, secondsLeft: 0, hint: SECURE_ERROR_HINT.challenge_expired }
  return { state: 'ended', needsCode: false, canVerify: false, secondsLeft, hint: `验证挑战状态：${challenge.status}。请重新预览后再次验证。` }
}

export function formatSeconds(seconds: number | null): string {
  if (seconds == null) return ''
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return m > 0 ? `${m}分${String(s).padStart(2, '0')}秒` : `${s}秒`
}
