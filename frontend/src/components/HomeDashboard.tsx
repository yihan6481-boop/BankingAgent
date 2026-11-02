import { useEffect, useState } from 'react'
import { api } from '../api'
import type { TaskView } from '../integrationTypes'
import type { CardItem } from '../types'
import { MobileIcon } from './MobileIcon'

type Shortcut = 'transfer' | 'bill' | 'billvision' | 'wealth' | 'payments'
const yuan = (v: number) => '¥ ' + v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
export function HomeDashboard({ sid, name, disabled, onShortcut, onChat, onBills, tasks, onTasks }: {
  tasks:TaskView[];onTasks:()=>void;sid: string; name: string; disabled:boolean; onShortcut: (target:Shortcut) => void; onChat: (prompt?:string) => void; onBills:()=>void
}) {
  const [cards, setCards] = useState<CardItem[] | null>(null)
  const [hide, setHide] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [retry,setRetry]=useState(0)
  useEffect(() => {
    let alive = true
    setCards(null); setError(''); setBusy(true)
    void api.read(sid, 'get_cards', {}).then(out => {
      if (!alive) return
      if (out.status === 'failed' || out.result?.kind !== 'card_list') { setError(out.message || '账户数据暂不可用'); return }
      setCards(out.result.data)
    }).catch(() => { if (alive) setError('无法读取账户数据，请检查连接后重试。') }).finally(() => { if (alive) setBusy(false) })
    return () => { alive = false }
  }, [sid,retry])
  const sum = cards?.reduce((value, card) => value + card.balance, 0)
  return <section className="mobile-page home-page" aria-label="银行首页">
    <div className="eyebrow">WELCOME BACK</div>
    <h1 className="mobile-heading">你好，{name} <span className="welcome-wave">✦</span></h1>
    <div className="asset-card">
      <div className="asset-label">银行卡余额合计 <MobileIcon name="cards" size={16}/></div>
      <div className="asset-value" aria-label="银行卡余额">{hide ? '¥ ••••••' : cards ? yuan(sum || 0) : busy ? '正在读取...' : '暂无数据'}</div>
      <div className="asset-bottom"><span>{cards ? `${cards.length} 张银行卡 · 仅已读取卡片余额` : error ? '账户查询未完成' : '模拟银行账户'}</span><button type="button" className="asset-hide" onClick={()=>setHide(v=>!v)} aria-label={hide?'显示金额':'隐藏金额'}><MobileIcon name={hide?'eye-off':'eye'} size={17}/>{hide?'显示':'隐藏'}</button></div>
      {error && <div role="alert"><p className="asset-error">{error}</p><button className="secondary mt-2" disabled={busy} onClick={()=>setRetry(v=>v+1)}>重试账户查询</button></div>}
    </div>
    <div className="quick-grid" aria-label="快捷业务">
      {([['transfer','transfer','转账'],['bill','receipt','账单'],['billvision','camera','识图'],['wealth','wallet','理财'],['payments','receipt','缴费']] as const).map(([target,icon,title]) =>
        <button key={target} type="button" disabled={disabled} onClick={()=>target==='bill'?onBills():onShortcut(target)} className="quick-item"><span className="quick-graphic"><MobileIcon name={icon} size={23}/></span><span>{title}</span></button>)}
    </div>
    <div className="ai-entry">
      <div className="promo-title"><span className="spark-square"><MobileIcon name="spark" size={20}/></span> 你的 AI 银行助手</div>
      <p className="muted-copy">不用找菜单，说出需求就能开始办理。</p>
      <button type="button" disabled={disabled} className="prompt-tile" onClick={()=>onChat('帮我分析本月的消费')}>帮我分析本月消费 <MobileIcon name="arrow" size={17}/></button>
      <button type="button" disabled={disabled} className="prompt-tile" onClick={()=>onChat()}>输入自己的银行业务需求 <MobileIcon name="arrow" size={17}/></button>
    </div>
    <div className="section-head"><h2>待处理任务</h2><button onClick={onTasks}>查看全部任务</button></div><div className="white-tile">{tasks.filter(t=>!['completed','cancelled'].includes(t.status)).slice(0,3).map(t=><button key={t.task.task_id} className="prompt-tile" onClick={onTasks}>{t.task.contract.goal}<MobileIcon name="chevron" size={16}/></button>)}{!tasks.some(t=>!['completed','cancelled'].includes(t.status))&&<p className="muted-copy">当前没有待处理任务。</p>}</div><div className="section-head"><h2>交易查询</h2><button onClick={onBills}>查看账单 <MobileIcon name="chevron" size={15}/></button></div>
    <div className="empty-ledger"><MobileIcon name="receipt" size={23}/><div><strong>真实流水请在账单页查看</strong><p>选择月份查询银行流水；上传图片账单单独管理。</p></div></div>
    <p className="mobile-footnote">当前为模拟银行测试环境。账户数据按当前登录身份从后端读取。</p>
  </section>
}
