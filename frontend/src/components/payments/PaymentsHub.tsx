import { useState } from 'react'
import type { RecipientResolution } from '../../api/payments'
import { RecipientResolutionPanel } from './RecipientResolutionPanel'
import { PaymentDocumentPanel } from './PaymentDocumentPanel'
import { ContactsPanel } from './ContactsPanel'

type Section = 'documents' | 'recipient' | 'contacts'
const TABS: [Section, string][] = [['documents', '缴费单据与对账'], ['recipient', '收款人解析'], ['contacts', '联系人']]

/**
 * Payment-document workspace. Backed by A's payment-1.0 contract; while an endpoint
 * is absent the page shows the real server error instead of inventing results.
 * Prepared payments go back to the existing task/preview/confirm flow and
 * reconciliation links never claim money movement.
 */
export function PaymentsHub({ sid, userId, busy, onOpenTasks, onChat, onPrepared,onInvoice }: {
  sid: string
  userId: number
  busy: boolean
  onOpenTasks: () => void
  onChat: (prompt?: string) => void
  onPrepared?: (taskId: string | null) => void
  onInvoice?: (documentId:string)=>void
}) {
  const [section, setSection] = useState<Section>('documents')
  const [resolution, setResolution] = useState<RecipientResolution | null>(null)
  const recipientPanel = <RecipientResolutionPanel sid={sid} busy={busy} resolution={resolution} onResolution={setResolution} />
  return <section className="mobile-page payments-page" aria-label="缴费与凭证">
    <h1 className="mobile-heading">识图办理</h1>
    <p className="muted-copy">上传图片，识别后逐项核对。新单据进入转账预览；已有付款先核实原任务；发票只用于记账核对，不证明已支付。</p>
    <div className="payment-tabs" role="tablist" aria-label="缴费功能">{TABS.map(([key, label]) => <button key={key} type="button" role="tab" aria-selected={section === key} className={section === key ? 'current' : ''} onClick={() => setSection(key)}>{label}</button>)}</div>
    {section === 'contacts' && <ContactsPanel sid={sid} busy={busy} />}
    {section === 'recipient' && recipientPanel}
    {section === 'documents' && <>
      <PaymentDocumentPanel sid={sid} userId={userId} busy={busy} resolution={resolution} recipientPanel={recipientPanel} onOpenTasks={onOpenTasks} onPrepared={onPrepared} onInvoice={onInvoice}/>
    </>}
    <details className="white-tile"><summary>识别或网络失败怎么办</summary>
      <p>请核对图片清晰度和单据状态。未收到结果时先查询原单据与任务，不要重复发起付款。</p>
      <button type="button" className="secondary" onClick={() => onChat('查看我的任务和当前办理状态')}>让 AI 协助核实当前状态</button>
    </details>
    <p className="mobile-footnote">发票与用户确认都不等于已付款。金额以整数分核对；同一缴费义务换图或换请求键都不能重复扣款。付款准备在发送前保存原请求身份，未知结果先核实原准备/任务，不会换键重提。对账关联只保存凭证关系，不新增支出、不改余额。</p>
  </section>
}
