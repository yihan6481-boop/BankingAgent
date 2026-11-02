export function QueryAuthorization({busy,onAuthorize,onCancel}:{busy:boolean;onAuthorize:()=>unknown;onCancel:()=>unknown}){
  return <section className="rounded-xl bg-amber-50 p-3 space-y-2"><p className="text-xs">订阅与代扣查询需要明确授权；授权前不会展示数据。</p><button className="primary" disabled={busy} onClick={()=>void onAuthorize()}>授权本次订阅查询</button><button className="secondary ml-2" onClick={()=>void onCancel()} disabled={busy}>取消查询</button></section>
}
