import { MobileIcon } from './MobileIcon'
import type { MobileIconName } from './MobileIcon'

export function ProfileHub({name,sessionCount,taskCount,busy,onOpen,onLogout}:{name:string;sessionCount:number;taskCount:number;busy:boolean;onOpen:(page:'memory'|'tasks'|'security'|'business'|'sessions'|'payments')=>void;onLogout:()=>void}){
 const items:[MobileIconName,string,string,'memory'|'tasks'|'security'|'business'|'sessions'|'payments'][]=[
   ['receipt','缴费与凭证','核对缴费单据、收款与对账','payments'],
   ['memory','长期记忆','查看、更正、删除本人记忆','memory'],
   ['tasks','历史任务与回执',`${taskCount} 条当前会话任务 · 以服务端为准`,'tasks'],
   ['cards','银行卡和理财','进入完整银行业务管理','business'],
   ['shield','安全验证','独立 TOTP 与权限验证','security'],
   ['user','会话管理',`${sessionCount} 个本标签页会话`,'sessions']
 ]
  return <section className="mobile-page profile-page" aria-label="个人中心"><div className="eyebrow">MY BANKING AGENT</div><h1 className="mobile-heading">我的</h1>
   <div className="profile-hero"><div className="profile-avatar"><MobileIcon name="user" size={28}/></div><div><strong>{name}</strong><p>模拟银行 · 个人测试账户</p></div><MobileIcon name="check" size={18}/></div>
   <h2 className="profile-section-title">账户与智能服务</h2><div className="profile-menu">{items.map(([icon,label,desc,page])=><button key={page} type="button" disabled={busy} onClick={()=>onOpen(page)}><span className="profile-menu-icon"><MobileIcon name={icon}/></span><span className="profile-menu-text"><strong>{label}</strong><small>{desc}</small></span><MobileIcon name="chevron" size={17}/></button>)}</div>
   <button className="logout-button" disabled={busy} onClick={onLogout}><MobileIcon name="logout" size={18}/>退出当前登录</button>
   <p className="mobile-footnote">所有转账、记忆变更、任务操作均由现有后端鉴权和确认流程控制。</p>
  </section>
}
