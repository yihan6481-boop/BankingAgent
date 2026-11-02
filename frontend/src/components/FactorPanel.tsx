import { useEffect, useState } from 'react'
import QRCode from 'qrcode'
import { api } from '../api'
import type { Factor } from '../integrationTypes'
import type { ActionError } from './secureErrors'
import { formatSeconds, secureErrorHint } from './secureErrors'
export function FactorPanel({ sid, factor, busy, run, actionError, recoveryTask, onReturnToTask, onRefreshFactor }: {
  sid: string; factor: Factor | undefined; busy: boolean
  run: <T,>(sid: string | null, fn: () => Promise<T>, sync?: boolean) => Promise<T | null>
  actionError?: ActionError | null
  recoveryTask?: { goal: string | null; taskId: string | null } | null
  onReturnToTask?: () => void
  onRefreshFactor?: () => Promise<unknown> | void
}) {
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [uri, setUri] = useState('')
  const [qr, setQr] = useState('')
  const [qrError, setQrError] = useState(false)
  const [enrolledExpires, setEnrolledExpires] = useState('')
  const [reauthOpen, setReauthOpen] = useState(false)
  const [now, setNow] = useState(Date.now())
  const [notice, setNotice] = useState('')
  const [justConfirmed, setJustConfirmed] = useState(false)
  useEffect(() => { const t = window.setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t) }, [])
  useEffect(() => {
    let live = true
    setQrError(false)
    if (uri) void QRCode.toDataURL(uri, { width: 220, margin: 2 }).then(value => { if (live) setQr(value) }).catch(() => { if (live) setQrError(true) })
    else setQr('')
    return () => { live = false }
  }, [uri])
  useEffect(() => { if (factor?.confirmed) { setUri(''); setQr(''); setEnrolledExpires('') } }, [factor?.confirmed])
  const pending = !!factor && !factor.confirmed && (factor.enrollment_pending ?? factor.bound)
  // Server status survives a refresh; a locally returned enrollment expiry is only a shortcut.
  const serverExpires = factor?.enrollment_expires_at || ''
  const expires = serverExpires || enrolledExpires
  const expiresAt = expires ? Date.parse(expires) : NaN
  const left = Number.isFinite(expiresAt) ? Math.max(0, Math.ceil((expiresAt - now) / 1000)) : null
  const expired = !!factor?.enrollment_expired || left === 0
  const canReenroll = factor?.can_reenroll ?? pending
  // A never-bound account may always start the first enrollment; re-enrollment is
  // only for a pending (unconfirmed) first binding.
  const canEnroll = !factor?.bound || canReenroll
  const enroll = async () => {
    const value = password; setPassword(''); setNotice('')
    const out = await run(sid, () => api.enroll(sid, value))
    if (out) {
      setUri(out.otpauth_uri); setEnrolledExpires(out.expires_at); setReauthOpen(false)
      setNotice('已生成新的绑定信息。旧二维码/旧导入链接立即失效；请用最新二维码或导入链接完成绑定，再输入动态码确认。')
    }
  }
  const confirm = async () => {
    const value = code; setCode(''); setNotice('')
    const out = await run(sid, () => api.confirmFactor(sid, value))
    if (out?.confirmed) { setUri(''); setQr(''); setEnrolledExpires(''); setJustConfirmed(true); setNotice('验证器绑定已确认。请回到原任务重新读取并重新预览；绑定不会自动付款。') }
  }
  const hint = secureErrorHint(actionError)
  return <div className="p-4 space-y-4 text-sm">
    <div className="flex justify-between items-center"><h2 className="font-semibold">独立验证器</h2><button className="secondary" disabled={busy} onClick={() => void onRefreshFactor?.()}>刷新绑定状态</button></div>
    <p className="text-xs text-slate-600">当前后端：{factor?.mode === 'secure' ? 'TOTP 安全模式' : '模拟 OTP 演示模式'}；验证器{factor?.confirmed ? '已绑定并确认' : pending ? '已生成，待确认' : '未绑定'}。</p>
    {actionError && <div role="alert" className="secure-error" data-error-code={actionError.code}><strong>绑定操作未完成</strong><p>{hint}</p></div>}
    {notice && <div role="status" className="task-notice">{notice}</div>}
    {factor?.confirmed ? <section className="space-y-2">
      <p className="rounded-xl bg-emerald-50 p-3 text-emerald-700">高风险操作将在当前操作卡中要求验证器动态码。绑定确认后，办理时可能需等待下一个 30 秒动态码。</p>
      {recoveryTask && <div className="task-notice">
        <p>绑定前停下的任务：{recoveryTask.goal || recoveryTask.taskId || '原任务'}</p>
        <p>绑定不会自动继续该任务。请回读原任务并重新预览，旧验证码和旧确认均已失效。</p>
        {onReturnToTask && <button className="primary" onClick={onReturnToTask}>返回原任务并重新读取</button>}
      </div>}
      <p className="text-xs text-slate-400">已确认的验证器不能被替换；当前版本没有解绑或找回入口。</p>
    </section> : <>
      {justConfirmed && onReturnToTask && <button className="primary w-full" onClick={onReturnToTask}>返回原任务并重新读取</button>}
      {pending && expires && <p className="task-notice">
        当前有未确认的首次绑定{Number.isFinite(expiresAt) ? `，服务器记录有效期至 ${new Date(expiresAt).toLocaleString('zh-CN')}${left != null && !expired ? `（剩余 ${formatSeconds(left)}）` : ''}` : ''}。
        {expired ? '该绑定已过期，未确认绑定不会生效；可用账户密码重新生成绑定信息（旧二维码失效）。' : '刷新或切换页面后二维码不会再次显示；可直接输入验证器动态码完成确认，也可以重新生成绑定信息。'}
      </p>}
      {expired && <p role="alert" className="mobile-warning">首次绑定已超过 10 分钟。可以用账户密码重新生成绑定信息后继续；不会用短信或模拟验证码替代，也不会替换已确认的验证器。</p>}
      {uri && <div className="bg-white p-3 rounded-xl border space-y-2">
        {qr && <img alt="验证器绑定二维码" src={qr} width={220} height={220} className="mx-auto" />}
        {qrError && <p role="status" className="text-xs text-amber-700">二维码生成失败，请使用下方导入链接。</p>}
        <p className="text-xs">最新绑定信息{Number.isFinite(Date.parse(enrolledExpires)) ? `有效至 ${new Date(enrolledExpires).toLocaleString('zh-CN')}` : '已生成'}。二维码只在本次生成响应显示一次，不进入聊天记录。</p>
        {(qr || qrError) && <details><summary className="text-xs text-webank-600 cursor-pointer">无法扫描？查看导入链接</summary><p className="text-[10px] break-all select-all">{uri}</p></details>}
      </div>}
      {pending ? <>
        <p className="text-xs text-slate-500">输入手机验证器的六位动态码确认绑定（首次绑定、未确认时可用）。</p>
        <input aria-label="绑定确认码" value={code} inputMode="numeric" maxLength={6} onChange={e => setCode(e.target.value.replace(/[^0-9]/g, ''))} disabled={busy} autoComplete="one-time-code" className="field w-full" />
        <button className="primary w-full" disabled={busy || !/^[0-9]{6}$/.test(code) || expired} onClick={() => void confirm()}>确认绑定</button>
        <p className="text-xs text-slate-500">同一个动态码不能重复使用；若提示已使用，请等待验证器显示下一个动态码。刷新后仍可在此输入动态码完成确认。</p>
        {!uri && !expired && <button className="secondary w-full" disabled={busy} onClick={() => setReauthOpen(v => !v)}>重新生成绑定信息（旧二维码失效）</button>}
        {(reauthOpen || expired) && <div className="space-y-2">
          <p className="text-xs text-slate-500">重新生成前需要再次核对账户密码；旧绑定材料会立即失效。</p>
          <input aria-label="绑定验证器的账户密码" type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} disabled={busy} placeholder="当前账户密码" className="field w-full" />
          <button className="primary w-full" disabled={busy || !password || !canReenroll} onClick={() => void enroll()}>确认重新生成绑定信息</button>
        </div>}
        <p className="text-xs text-slate-400">该账户已确认的验证器不能被替换；只有在未确认的首次绑定阶段才能重新生成。</p>
      </> : <div className="space-y-2">
        <p className="text-xs text-slate-500">重新核对账户密码，再使用手机验证器扫描或导入绑定信息。二维码只在生成响应中显示一次，不进入聊天记录。</p>
        <input aria-label="绑定验证器的账户密码" type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} disabled={busy} placeholder="当前账户密码" className="field w-full" />
        <button className="primary w-full" disabled={busy || !password || !canEnroll} onClick={() => void enroll()}>生成验证器绑定信息</button>
        <p className="text-xs text-slate-500">首次绑定有 10 分钟有效期。过期后可用账户密码重新生成绑定信息（仅未确认阶段），不会用短信或模拟验证码替代。</p>
      </div>}
    </>}
    <p className="text-xs text-slate-400">页面不生成登录验证码，也不会代替你完成业务验证。已确认的验证器不能被替换。</p>
  </div>
}
