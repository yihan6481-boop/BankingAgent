import { MobileIcon } from './MobileIcon'
export function FaultNotice({kind,message,detail,onRetry,onTasks,onSupport}:{kind:'offline'|'service'|'model'|'memory'|'tool'|'unknown';message?:string;detail?:string;onRetry?:()=>void;onTasks?:()=>void;onSupport?:()=>void}){
 const text={offline:'网络已断开，输入与表单已保留。连接恢复后可继续。',service:'暂时无法连接服务，请稍后再试。',model:'AI 暂时无法回答，手动查询和办理仍可使用。',memory:'长期记忆暂时不可用，其他业务可继续使用。',tool:'本次查询未完成，请核实后重试。',unknown:'结果确认中。请核查原任务，避免重复付款。'}[kind]
 return <section className={`fault-notice fault-${kind}`} role="status"><div><MobileIcon name={kind==='unknown'?'clock':'shield'} size={20}/><p>{message||text}</p></div><div className="fault-actions">{onRetry&&<button onClick={onRetry}>{kind==='unknown'?'继续查询':'重新连接'}</button>}{onTasks&&<button onClick={onTasks}>查看原任务</button>}{onSupport&&<button onClick={onSupport}><MobileIcon name="headset" size={16}/>联系人工</button>}</div>{detail&&<details><summary>查看详情</summary><p>{detail}</p></details>}</section>
}
