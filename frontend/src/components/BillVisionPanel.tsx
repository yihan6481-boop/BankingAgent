import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { bills, cents, stable, yuan } from '../api/bills'
import type { BillDocument, BillResult, BillRow, BillSummary, BillValue } from '../api/bills'

const states: Record<string, string> = {uploaded: '待识别', recognizing: '识别中', recognition_failed: '识别失败，可重试', review: '待核对保存', confirmed: '已确认保存', unsupported: '图片不支持'}
const problems: Record<string, string> = {missing_date: '日期：请填写原图明确年份；无法确认请排除此行', missing_merchant: '商户：请补全商户名称', missing_amount_cents: '金额：请填写图片可确认的金额（元）', missing_currency: '币种：请核实币种', unknown_currency: '币种未知，请核实', unknown_direction: '收支方向：请选择收入或支出', unknown_kind: '交易类型：请选择购买、退款等', unknown_status: '交易状态：请选择原图状态', unknown_precision: '时间精度：请选择日期或具体时间', missing_time: '时间缺失：请填写原图时间或选择仅日期', purchase_direction_conflict: '购买应为支出，请核实', refund_direction_conflict: '退款应为收入，请核实'}
export function BillVisionPanel({sid, onBusy, onExpired, onChanged,initialDocumentId}: {sid: string; onBusy: (v: boolean) => void; onExpired: () => void; onChanged?: () => void;initialDocumentId?:string}) {
  const [list, setList] = useState<BillSummary[]>([]), [more, setMore] = useState(false)
  const [doc, setDoc] = useState<BillDocument | null>(null), [rows, setRows] = useState<BillRow[]>([])
  const [amounts, setAmounts] = useState<Record<string, string>>({}), [platform, setPlatform] = useState('unknown')
  const [image, setImage] = useState(''), [busy, setBusy] = useState(false), [message, setMessage] = useState('选择一张微信或支付宝账单图片后自动识别。')
  const [error, setError] = useState(''), [result, setResult] = useState<BillResult | null>(null), [dirty, setDirty] = useState(false)
  const [merchant, setMerchant] = useState(''), [chat, setChat] = useState('刚上传这张账单一共支出多少'), [answer, setAnswer] = useState('')
  const lock = useRef(false), alive = useRef(true), imageRef = useRef(''), pending = useRef<BillDocument | null>(null)
  const totalsRef = useRef<HTMLDivElement>(null), errorRef = useRef<HTMLParagraphElement>(null)
  useEffect(()=>{if(!dirty)return;const warn=(e:BeforeUnloadEvent)=>{e.preventDefault();e.returnValue=''};window.addEventListener('beforeunload',warn);return()=>window.removeEventListener('beforeunload',warn)},[dirty])
  useEffect(() => { if (result) totalsRef.current?.scrollIntoView({block: 'center'}) }, [result])
  useEffect(() => { if (error) errorRef.current?.scrollIntoView({block: 'center'}) }, [error])
  useEffect(() => { alive.current = true; return () => { alive.current = false; URL.revokeObjectURL(imageRef.current); onBusy(false) } }, [onBusy])
  async function refreshList() { const v = await bills.list(sid); if (alive.current) { setList(v.documents); setMore(v.has_more) } }
  function paint(d: BillDocument) { if (!alive.current) return; setDoc(d); setRows(d.rows); setPlatform(d.platform); setAmounts(Object.fromEntries(d.rows.map(r => [r.row_id, r.value.amount_cents == null ? '' : yuan(r.value.amount_cents)]))); setDirty(false) }
  async function show(d: BillDocument, discardConfirmed = false) {
    if(dirty&&!discardConfirmed&&!window.confirm('当前修改尚未保存，是否放弃并重新打开？'))return
    URL.revokeObjectURL(imageRef.current); imageRef.current = ''; setImage(''); setResult(null); setAnswer('')
    setDoc(null); setRows([]); setAmounts({}); pending.current = null
    const selected = await bills.select(sid, d.document_id)
    paint(selected); pending.current = null; setResult(null); setAnswer(''); setMerchant('')
    const blob = await bills.image(sid, d.document_id)
    if (!alive.current) return
    URL.revokeObjectURL(imageRef.current); imageRef.current = URL.createObjectURL(blob); setImage(imageRef.current)
    if (selected.state === 'confirmed') setResult(await bills.query(sid, selected.document_id))
  }
  async function run(work: () => Promise<void>) {
    if (lock.current) return
    lock.current = true; setBusy(true); onBusy(true); setError('')
    try { await work() } catch (e) {
      if (alive.current) { setResult(null); setAnswer(''); setError((e instanceof Error ? e.message : '操作失败') + ' 请重新打开批次核对；不会自动重试或重复识别。'); setMessage('本次尚未完成，请按提示继续。') }
      if (e && typeof e === 'object' && 'status' in e && e.status === 401) onExpired()
    } finally { lock.current = false; if (alive.current) { setBusy(false); onBusy(false) } }
  }
  useEffect(() => { void run(async()=>{await refreshList();if(initialDocumentId){await show(await bills.select(sid,initialDocumentId),true);setMessage('发票已进入记账核对，付款状态未核实。请核对日期、金额与重复记录后再明确导入；此流程不会扣款。')}}) }, [sid,initialDocumentId])
  function changed() { setDirty(true); pending.current = null; setResult(null); setAnswer(''); setMessage('修改尚未保存；点击确认保存后按新内容重新核对导入。') }
  function update(id: string, field: keyof BillValue, value: unknown) { changed(); setRows(prev => prev.map(r => r.row_id === id ? {...r, reviewed: false, value: {...r.value, [field]: value}} : r)) }
  async function recognize(d: BillDocument) { setMessage('正在识别图片，请稍候…'); const v = await bills.recognize(sid, d); onChanged?.(); paint(v); setMessage(v.rows.length ? '识别完成，请对照原图核对明细。' : '图片未识别出支持的交易，请更换微信或支付宝账单图片。'); await refreshList() }
  async function selectFile(file: File) {
    if (!['image/png', 'image/jpeg'].includes(file.type) || file.size > 10 * 1024 * 1024) throw Error('请选择10MiB以内PNG/JPEG图片。')
    URL.revokeObjectURL(imageRef.current); imageRef.current = ''; setImage(''); setDoc(null); setRows([]); pending.current = null
    setResult(null); setAnswer(''); setMessage('正在上传图片…')
    const d = await bills.upload(sid, file); onChanged?.(); await show(d, true)
    if (d.repeated_upload) { setMessage('这张图片已存在，已打开本人已有结果；未重复调用识别。'); await refreshList(); return }
    await recognize(d)
  }
  function snapshot(review: boolean) {
    return rows.map(r => ({...r, reviewed: review ? r.included && r.duplicate_resolution !== 'exclude' : false, value: {...r.value, amount_cents: amounts[r.row_id] === '' ? null : cents(amounts[r.row_id])}}))
  }
  async function save(confirm: boolean) {
    if (!doc) return
    setResult(null); setAnswer('')
    const frozen = snapshot(confirm)
    if (confirm && !frozen.some(r => r.included && r.duplicate_resolution !== 'exclude')) throw Error('请至少选择一条明细；不需导入可删除该批次。')
    if (platform === 'unknown') throw Error('请选择图片的来源平台，不能自动猜测。')
    setMessage(confirm ? '正在保存核对内容并确认…' : '正在保存更正草稿，旧统计将撤回…')
    let latest = pending.current
    if (latest) {
      latest = await bills.get(sid, latest.document_id)
      if (latest.revision !== pending.current!.revision || latest.digest !== pending.current!.digest) throw Error('批次已被其他操作修改，请重新打开核对。')
    } else {
      const saved = await bills.edit(sid, doc, frozen, platform)
      onChanged?.()
      latest = await bills.get(sid, saved.document_id)
      if (latest.revision !== saved.revision || latest.digest !== saved.digest || latest.platform !== platform || stable(saved.rows) !== stable(frozen) || stable(latest.rows) !== stable(frozen)) throw Error('保存响应或回读与本次核对不一致，未继续确认。')
      pending.current = latest; paint(latest)
    }
    if (!confirm) { pending.current = null; await refreshList(); setMessage('已保存更正草稿，旧统计已撤回；请核对后点击确认保存。'); return }
    setMessage('核对内容已持久保存，正在完成确认；若连接中断可继续确认，无需重新识别。')
    if (latest.state !== 'confirmed') { await bills.confirm(sid, latest); onChanged?.() }
    const fresh = await bills.get(sid, latest.document_id)
    const ids = frozen.filter(r => r.included && r.duplicate_resolution !== 'exclude').map(r => r.row_id).sort()
    if (fresh.state !== 'confirmed' || fresh.revision !== latest.revision || fresh.digest !== latest.digest || stable(fresh.confirmation?.row_ids) !== stable(ids)) throw Error('确认结果尚未核实，请继续确认或重新打开核对。')
    const q = await bills.query(sid, fresh.document_id)
    if (stable(q.records.map(r => r.row_id).sort()) !== stable(ids) || q.records.some(r => r.revision !== fresh.revision)) throw Error('本人查询结果与确认行不一致，请重新打开核对。')
    paint(fresh); setResult(q); pending.current = fresh; await refreshList(); setMessage(`已保存${fresh.confirmation!.imported_count}条，已核对本人查询结果。`)
  }
  function input(row: BillRow, field: keyof BillValue, label: string, type = 'text') {
    return <label className="block">{label}<input className="field w-full" aria-label={`${row.row_id} ${label}`} type={type} value={String(row.value[field] ?? '')} onChange={e => update(row.row_id, field, e.target.value || null)} /></label>
  }
  function select(row: BillRow, field: keyof BillValue, label: string, options: [string, string][]) {
    return <label className="block">{label}<select className="field w-full" aria-label={`${row.row_id} ${label}`} value={String(row.value[field] ?? '')} onChange={e => update(row.row_id, field, e.target.value)}>{options.map(([v, t]) => <option key={v} value={v}>{t}</option>)}</select></label>
  }
  return <section className="p-3 space-y-3 text-sm" aria-label="账单识别面板" aria-busy={busy}>
    <div className="eyebrow">上传账单 · 独立数据来源</div><h2 className="mobile-heading">图片账单</h2><ol className="review-steps" aria-label="账单处理步骤"><li aria-current={!doc?'step':undefined}>1 上传</li><li aria-current={doc&&!result?'step':undefined}>2 核对原图</li><li aria-current={result?'step':undefined}>3 保存结果</li></ol>{dirty&&<p className="unsaved-notice" role="status">有未保存修改。切换页面会保留本次编辑；关闭页面前请先核对并保存。</p>}<p className="text-xs text-slate-500">上传数据独立于银行流水。仅本人已确认记录参与统计，退款单列；不授权付款。</p>
    <p id="bill-status" role="status" className="p-2 bg-sky-50 whitespace-pre-wrap">{message}</p>
    {error && <p ref={errorRef} role="alert" className="p-2 bg-rose-50 text-rose-700 whitespace-pre-wrap">{error}</p>}
    <fieldset disabled={busy} className="space-y-3">
      <label className="block">选择账单图片（自动识别）<input id="bill-file" className="w-full" type="file" accept="image/png,image/jpeg" onChange={e => { if(dirty&&!window.confirm('当前修改尚未保存，是否放弃并上传其他图片？')){e.target.value='';return}; const f = e.target.files?.[0]; e.target.value = ''; if (f) void run(() => selectFile(f)) }} /></label>
      <button className="secondary" onClick={() => void run(refreshList)}>刷新本人批次</button>
      <ul className="space-y-1" aria-label="本人已有批次">{list.map(d => <li key={d.document_id}><button className="secondary w-full text-left" data-bill-id={d.document_id} onClick={() => void run(async () => { await show(await bills.get(sid, d.document_id)); setMessage('已打开所选批次，原图与记录已回读。') })}>{d.created_at.slice(0, 16).replace('T', ' ')} · {states[d.state]} · {d.document_id.slice(-6)}</button></li>)}</ul>
      {more && <p>仅展示最近100个批次，更多批次可由已有API查询。</p>}
      {doc && <>
        {doc.source_current===false&&<p className="mobile-warning" role="alert">原发票来源已修改或撤回，旧记账来源失效，未计入当前统计。请核对来源，明确删除旧导入后重新办理。</p>}
        <p data-bill-state={doc.state}>{states[doc.state]}{dirty ? ' · 页面有未保存修改' : ''}</p>
        {image && <img className="w-full max-h-96 object-contain border" src={image} alt="当前本人上传账单原图" />}
        {['uploaded', 'recognition_failed', 'recognizing'].includes(doc.state) && <button className="primary" onClick={() => void run(() => recognize(doc))}>重试识别（将调用服务）</button>}
        {doc.rows.length > 0 && <>
          <label className="block">来源平台<select className="field w-full" aria-label="来源平台" value={platform} onChange={e => { changed(); setPlatform(e.target.value) }}><option value="unknown">请核实来源</option><option value="wechat">微信</option><option value="alipay">支付宝</option><option value="invoice">发票（不证明已付款）</option></select></label>
          {rows.map((r, i) => <article className="border rounded-xl p-3 space-y-2 bg-white" key={r.row_id} data-bill-row={r.row_id}>
            <h3 className="font-semibold">明细 {i + 1}</h3>
            <label className="flex gap-2 items-center"><input type="checkbox" aria-label={`${r.row_id} 选择此条明细`} checked={r.included && r.duplicate_resolution !== 'exclude'} onChange={e => { changed(); setRows(prev => prev.map(x => x.row_id === r.row_id ? {...x, included: e.target.checked, reviewed: false, duplicate_resolution: e.target.checked ? 'unresolved' : 'exclude'} : x)) }} />选择此条明细</label>
            <div className="grid grid-cols-2 gap-2 text-xs">
              {input(r, 'date', '日期', 'date')}{input(r, 'merchant', '商户')}
              <label>金额（元）<input className="field w-full" inputMode="decimal" aria-label={`${r.row_id} 金额（元）`} value={amounts[r.row_id] || ''} onChange={e => { changed(); setAmounts(v => ({...v, [r.row_id]: e.target.value})) }} /></label>
              {input(r, 'currency', '币种')}
              {select(r, 'direction', '收支方向', [['unknown', '请核实'], ['expense', '支出'], ['income', '收入']])}
              {select(r, 'kind', '交易类型', [['unknown', '请核实'], ['purchase', '购买'], ['refund', '退款'], ['transfer', '转账'], ['other', '其他']])}
              {select(r, 'status', '交易状态', [['unknown', '请核实'], ['success', '成功'], ['pending', '待支付'], ['failed', '失败'], ['closed', '已关闭']])}
              {select(r, 'precision', '时间精度', [['unknown', '请核实'], ['day', '仅日期'], ['minute', '精确到分'], ['second', '精确到秒']])}
              {input(r, 'time', '时间')}
            </div>
            {doc.issues[r.row_id]?.filter(x => !r.value.issues.includes(x)).map(x => <p className="text-amber-800 text-xs" key={x}>{problems[x] || x}</p>)}
            {r.value.issues.map((x, n) => doc.issues[r.row_id]?.includes(x) ? <label className="block text-xs bg-amber-50 p-2" key={x}><input type="checkbox" aria-label={`${r.row_id} 已解决疑点${n + 1}`} onChange={() => { changed(); setRows(prev => prev.map(v => v.row_id === r.row_id ? {...v, reviewed: false, value: {...v.value, issues: v.value.issues.filter((_, j) => j !== n)}} : v)) }} /> 已对照原图核实并解决：{problems[x] || x}。如无法核实，请排除此行。</label> : <p key={x} className="text-xs text-slate-500">识别说明：{x}</p>)}
            {!!doc.duplicates[r.row_id]?.length && <label className="block text-xs text-amber-800">{doc.duplicates[r.row_id].some(x => x.type === 'event') ? '确定重复事件：请取消选择，不可重复导入。' : '疑似同额交易：请核对是否不同交易。'}<select aria-label={`${r.row_id} 重复处理`} className="field w-full" value={r.duplicate_resolution} onChange={e => { changed(); setRows(prev => prev.map(v => v.row_id === r.row_id ? {...v, duplicate_resolution: e.target.value} : v)) }}><option value="unresolved">待核实</option><option value="keep_distinct">已核实为不同交易</option><option value="exclude">排除此行</option></select></label>}
            <details className="text-xs"><summary>原图文字与核对详情</summary><p>{r.value.evidence}</p><p>核对状态：{r.reviewed ? '已核对' : '等待本次明确确认'}</p>{input(r, 'payment_method', '支付方式')}{input(r, 'order_number', '原订单号')}{input(r, 'event_number', '独立事件号')}</details>
          </article>)}
          <p className="text-xs">点击“确认保存”表示已对照原图核对当前选中的记录；保存期间暂不能修改。未选中记录不会导入。</p>
          <button id="bill-confirm" className="primary w-full" onClick={() => void run(() => save(true))}>{pending.current?.state === 'review' && !dirty ? '继续确认保存' : '确认保存'}</button>
          {doc.state === 'confirmed' && dirty && <button className="secondary w-full" onClick={() => void run(() => save(false))}>保存更正草稿（撤回旧统计）</button>}
        </>}
        <button className="secondary" onClick={() => void run(async () => { await show(await bills.get(sid, doc.document_id)); setMessage('已重新读取当前批次。') })}>重新打开当前批次</button>
        <label className="block">商户筛选（留空为当前批次全部）<input className="field w-full" aria-label="查询商户" value={merchant} onChange={e => setMerchant(e.target.value)} /></label>
        <button className="secondary" onClick={() => void run(async () => { if (dirty) throw Error('请先保存或重新打开，避免把旧统计当作更正结果。'); setResult(await bills.query(sid, doc.document_id, merchant)); setMessage('已查询当前批次本人确认记录。') })}>查询当前批次</button>
        <button className="secondary" onClick={() => void run(async () => { await bills.delete(sid, doc); onChanged?.(); setDoc(null); setRows([]); setResult(null); setAnswer(''); setAmounts({}); pending.current = null; URL.revokeObjectURL(imageRef.current); imageRef.current = ''; setImage(''); await refreshList(); setMessage('已删除此批次、原图、修订和当前统计，聊天旧引用已失效。') })}>删除当前批次</button>
        <label className="block">上传账单聊天查询<input className="field w-full" aria-label="上传账单聊天问题" value={chat} onChange={e => setChat(e.target.value)} /></label>
        <button className="secondary" onClick={() => void run(async () => { if (dirty) throw Error('请先处理当前修改再查询。'); const v = await api.chat(sid, chat, 'analysis'); setAnswer(v.message); if (v.error_code) console.debug('bill chat', v.error_code); setMessage(v.error_code ? '查询未完成，请按回答补充条件或稍后重试。' : '已查询本次账单。') })}>查询上传账单</button>
        <details className="text-xs"><summary>版本与原识别详情</summary><pre className="whitespace-pre-wrap break-all">{JSON.stringify({revision: doc.revision, digest: doc.digest, original: doc.original}, null, 2)}</pre></details>
      </>}
      <button className="secondary" onClick={() => void run(async () => { if (dirty) throw Error('请先处理当前修改。'); setResult(await bills.query(sid)); setMessage('已查询本人全部已确认上传批次。') })}>查询全部已确认上传账单</button>
    </fieldset>
    {result && <div ref={totalsRef} id="bill-totals" className="p-3 bg-emerald-50 space-y-1"><p>当前汇总 · {result.records.length}条已确认记录</p><p>购买支出 {yuan(result.totals.purchase_expense_cents)}元</p><p>其他支出 {yuan(result.totals.other_expense_cents)}元</p><p>退款单列 {yuan(result.totals.refund_cents)}元</p><p>其他收入 {yuan(result.totals.income_cents)}元</p><p className="text-xs">未计入 {result.totals.excluded_count}条（非成功或非CNY）。{result.coverage}</p></div>}
    {answer && <p id="bill-chat-answer" className="p-3 bg-sky-50 whitespace-pre-wrap">{answer}</p>}
  </section>
}
