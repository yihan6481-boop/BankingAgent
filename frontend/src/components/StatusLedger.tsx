import type { StatusLayer } from './statusModel'

const TONE_CLASS: Record<StatusLayer['tone'], string> = {
  idle: 'bg-slate-100 text-slate-500',
  info: 'bg-sky-50 text-sky-700',
  ok: 'bg-emerald-50 text-emerald-700',
  warn: 'bg-amber-50 text-amber-800',
  bad: 'bg-rose-50 text-rose-700',
}

/**
 * Layered status board. Each row keeps its own source of truth; the UI never
 * derives payment success from a dialogue turn or a completed task alone.
 */
export function StatusLedger({ layers }: { layers: StatusLayer[] }) {
  return <section aria-label="分层状态" className="status-ledger">
    <div className="status-ledger-head">
      <h3>状态分层</h3>
      <span>对话 ≠ 草稿 ≠ 任务 ≠ 授权 ≠ 回执</span>
    </div>
    <ul>
      {layers.map(layer => <li key={layer.id} data-layer={layer.id} data-state={layer.state}>
        <div className="status-layer-line">
          <span className="status-layer-label">{layer.label}</span>
          <span className={`status-layer-state ${TONE_CLASS[layer.tone]}`}>{layer.state}</span>
        </div>
        <p>{layer.detail}</p>
      </li>)}
    </ul>
  </section>
}
