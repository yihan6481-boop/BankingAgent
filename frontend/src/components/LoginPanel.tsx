import { useState } from 'react'
export function LoginPanel({ busy, onLogin, onClose }: {
  busy: boolean; onLogin: (username: string, password: string) => Promise<unknown>; onClose?: () => void
}) {
  const [username, setUsername] = useState(()=>{try{return localStorage.getItem('banking-last-username')||''}catch{return ''}})
  const [password, setPassword] = useState('')
  return <form className="p-5 space-y-4" onSubmit={async e => {
    e.preventDefault(); const value = password; setPassword('')
    if (await onLogin(username, value)) {try{localStorage.setItem('banking-last-username',username)}catch{/* optional */}onClose?.()}
  }}>
    <h2 className="font-semibold text-lg text-slate-800">登录你的账户</h2>
    <p className="login-helper">使用你的账户登录，继续查询和办理。</p>
    <label className="block text-sm">账号<input aria-label="账号" className="field mt-1 w-full" value={username} disabled={busy} onChange={e => setUsername(e.target.value)} autoComplete="username" /></label>
    <label className="block text-sm">密码<input aria-label="密码" className="field mt-1 w-full" type="password" value={password} disabled={busy} onChange={e => setPassword(e.target.value)} autoComplete="current-password" /></label>
    <button className="primary w-full" disabled={busy || !username || !password}>{busy ? '正在登录…' : '登录'}</button>
    {onClose && <button type="button" className="secondary w-full" onClick={onClose} disabled={busy}>返回当前会话</button>}
    <p className="login-helper">请使用独立发放的演示账号。忘记密码时，请联系测试负责人协助重置。</p>
  </form>
}
