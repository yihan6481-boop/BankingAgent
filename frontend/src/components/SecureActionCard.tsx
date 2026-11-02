import { useEffect, useState } from 'react'
import type { Binding, SecureTask, TaskView, UiState } from '../integrationTypes'
import { bindingOf } from '../integrationTypes'
import { PreviewIdentity } from './PreviewIdentity'
import type { ActionError } from './secureErrors'
import { RE_READ_ERRORS, FACTOR_ERRORS, challengeView, formatSeconds, secureErrorHint } from './secureErrors'
const LABEL: Record<string, string> = { from_card_id: '付款卡编号', card_id: '银行卡编号', recipient_name: '收款人',
  recipient_id: '收款人编号', amount: '金额（元）', daily_limit: '新限额（元）', product_id: '产品编号',
  holding_id: '持仓编号', subscription: '订阅名称', subscription_id: '订阅编号', remark: '备注' }
export function SecureActionCard({ sid, state, busy, actionError, onConfirm, onVerify, onCancel, onResume, onSecurity }: {
  sid: string; state: UiState; busy: boolean
  actionError?: ActionError | null
  onConfirm: (b: Binding, text: string) => Promise<unknown>
  onVerify: (b: Binding, code: string, method: 'totp' | 'mock_otp') => Promise<unknown>
  onCancel: (task: TaskView) => Promise<unknown>; onResume: (task: TaskView) => Promise<unknown>; onSecurity: () => void
}) {
  const [phrase, setPhrase] = useState('')
  const [code, setCode] = useState('')
  const [now, setNow] = useState(Date.now())
  useEffect(() => { const t = window.setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t) }, [])
  const secure: SecureTask = state.task!
  const preview = secure?.preview
  const binding = bindingOf(sid, secure)
  if (!preview || !binding || !secure) return null
  const previewExpired = Date.parse(preview.expires_at) <= now
  const challenge = secure.challenge
  const view = challengeView(challenge, now)
  const needsCode = view.needsCode
  const unsupported = view.state === 'unsupported'
  const previewSeconds = Math.max(0, Math.ceil((Date.parse(preview.expires_at) - now) / 1000))
  const hint = secureErrorHint(actionError)
  const errorNeedsReRead = !!actionError && (RE_READ_ERRORS.has(actionError.code) || actionError.code === 'pending_action_expired')
  const errorNeedsFactor = !!actionError && FACTOR_ERRORS.has(actionError.code)
  const canConfirm = !busy && !previewExpired && !needsCode && ['none', 'verified'].includes(view.state) && !unsupported
  const submitCode = () => {
    if (busy || !/^[0-9]{6}$/.test(code) || !challenge || unsupported || !view.canVerify) return
    const value = code; setCode('')
    void onVerify(binding, value, challenge.method as 'totp' | 'mock_otp')
  }
  return <section aria-label="当前操作" className="secure-action-card text-sm space-y-3">
    <div className="flex justify-between items-center"><h3 className="font-semibold text-webank-800">当前操作预览</h3>
      <span className={`badge ${preview.permission_level === 'red' ? 'bg-rose-100 text-rose-700' : 'bg-amber-100 text-amber-700'}`}>
        {preview.permission_level === 'red' ? '需要强验证' : '需要明确确认'}</span></div>
    <p className="text-slate-600 whitespace-pre-wrap">{preview.confirmation_message}</p>
    <PreviewIdentity sid={sid} taskId={secure.task.task.task_id} preview={preview} />
    <dl className="secure-parameters text-sm">{['amount', 'daily_limit', 'remark'].filter(k => preview.requested_arguments[k] != null).map(k => <div key={k}><dt>{LABEL[k]}</dt><dd className="break-all font-medium">{String(preview.requested_arguments[k])}</dd></div>)}</dl>
    <details><summary>参数与内部编号详情</summary>
    <dl className="secure-parameters text-xs space-y-1">{Object.entries(preview.requested_arguments).filter(([k, v]) =>
      !['confirmed', 'idempotency_key', 'user_id'].includes(k) && v !== null && typeof v !== 'object'
    ).map(([k, v]) => <div key={k} className="flex justify-between gap-3"><dt className="text-slate-500">{LABEL[k] ?? k}</dt><dd className="font-medium break-all">{String(v)}</dd></div>)}</dl>
    </details>
    <p className="text-xs text-slate-500">映射不可用时请按编号核对目标。修改参数请进入任务详情，旧授权将失效。</p>
    <p className="text-xs text-slate-500">{previewExpired ? '预览已过期，请重新预览。' : `预览剩余 ${formatSeconds(previewSeconds)}`}</p>
    {challenge && view.state !== 'none' && <p className="text-xs text-slate-500" data-challenge-state={view.state}>
      {view.state === 'pending' && view.secondsLeft != null ? `验证挑战剩余 ${formatSeconds(view.secondsLeft)}，最多还可尝试 ${challenge.attempts_remaining} 次。` : view.hint}
    </p>}
    {actionError && <div role="alert" className="secure-error" data-error-code={actionError.code}>
      <strong>{errorNeedsFactor ? '需要先完成验证器绑定' : errorNeedsReRead ? '本次授权已失效' : '操作未完成'}</strong>
      <p>{hint}</p>
      <div className="flex gap-2 flex-wrap">
        {errorNeedsFactor && <button className="secondary" disabled={busy} onClick={onSecurity}>去绑定 / 确认验证器</button>}
        {errorNeedsReRead && <button className="secondary" disabled={busy} onClick={() => void onResume(secure.task)}>重新读取并预览</button>}
      </div>
    </div>}
    {!state.factor.confirmed && state.factor.mode === 'secure' && <button className="secondary w-full" disabled={busy} onClick={onSecurity}>{state.factor.bound ? '确认独立验证器绑定' : '绑定独立验证器'}</button>}
    {needsCode && view.state === 'pending' && <div className="bg-white rounded-xl p-3 space-y-2">
      <p className="font-medium">{challenge!.method === 'totp' ? '独立验证器校验' : '模拟验证码校验'}</p>
      <p className="text-xs text-slate-500">{view.hint} 剩余尝试 {challenge!.attempts_remaining} 次。</p>
      <input aria-label="验证码" autoComplete="one-time-code" inputMode="numeric" maxLength={6} value={code} disabled={busy}
        onChange={e => setCode(e.target.value.replace(/[^0-9]/g, ''))} placeholder="六位验证码" className="field w-full" />
      <button disabled={busy || !/^[0-9]{6}$/.test(code) || !view.canVerify} onClick={submitCode} className="primary w-full">验证本次操作</button>
      <p className="text-xs text-slate-500">动态码被使用过或已过期时，请等待下一个动态码；系统不会自动连续重试。</p>
    </div>}
    {(view.state === 'expired' || view.state === 'ended' || unsupported) && !previewExpired && <div className="secure-error" role="status">
      <strong>当前验证挑战不能继续</strong>
      <p>{unsupported ? view.hint : view.state === 'expired' ? '验证挑战已过期，旧验证码和旧授权失效。请重新读取任务并取得新预览。' : view.hint}</p>
      <button className="secondary" disabled={busy} onClick={() => void onResume(secure.task)}>重新读取并预览</button>
    </div>}
    {view.state === 'verified' && <p role="status" className="text-xs text-emerald-700">本次操作验证已通过；尚未执行，请确认。</p>}
    {canConfirm && <div className="space-y-2">
      {preview.confirmation_phrase ? <>
        <p className="text-xs text-slate-600">请手动输入确认短语：</p><p className="font-mono font-semibold text-webank-700">{preview.confirmation_phrase}</p>
        <input aria-label="确认短语" value={phrase} disabled={busy || previewExpired} onChange={e => setPhrase(e.target.value)} placeholder="手动输入上方短语" autoComplete="off" className="field w-full" />
      </> : <p className="text-xs text-slate-600">点击确认即明确授权上方这一次操作。</p>}
      <div className="flex gap-2">
        <button className="primary flex-1" disabled={busy || previewExpired || (!!preview.confirmation_phrase && phrase.trim() !== preview.confirmation_phrase)}
          onClick={() => { const text = preview.confirmation_phrase ? phrase.trim() : '确认'; setPhrase(''); void onConfirm(binding, text) }}>确认执行当前操作</button>
        <button className="secondary" disabled={busy} onClick={() => void onCancel(secure.task)}>取消任务</button>
      </div>
    </div>}
    {!canConfirm && !previewExpired && needsCode && <button className="secondary w-full" disabled={busy} onClick={() => void onCancel(secure.task)}>取消任务</button>}
    {previewExpired && <button className="primary w-full" disabled={busy} onClick={() => void onResume(secure.task)}>重新预览当前任务</button>}
  </section>
}
