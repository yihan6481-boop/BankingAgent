const CAPS = [{ id: 'bill', name: '账单分析' }, { id: 'card', name: '银行卡' }, { id: 'subscription', name: '订阅代扣' },
  { id: 'transfer', name: '智能转账' }, { id: 'wealth', name: '模拟理财' }, { id: 'workflow', name: '跨场景任务' }]
export function CapabilityBar({ onSelect, busy }: { onSelect: (domain: string) => void; busy: boolean }) {
  return <div className="grid grid-cols-3 gap-2 p-3">{CAPS.map(c => <button key={c.id} disabled={busy} onClick={() => onSelect(c.id)}
    className="bg-gradient-to-br from-webank-400 to-webank-600 text-white text-xs font-medium py-2.5 rounded-xl shadow-sm disabled:opacity-40">{c.name}</button>)}</div>
}
