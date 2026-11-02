import type { ChatMessage } from '../store/useChat'
import { cashFlowReport } from './CashFlowReport'

export function readableReply(message: ChatMessage): string {
  if (message.error) return message.text
  // Replace only known verbose tool formatters backed by this message's
  // structured result. Do not strip arbitrary JSON, warnings or confirmations.
  for (const result of message.results ?? []) {
    const b = result.business
    if (b?.kind === 'transaction_list' && /^\d{4}-\d{2} 共查询到 \d+ 条流水(?:：|$)/.test(message.text)) {
      return b.data.length ? '已整理这次查询的流水明细，可以点开查看。' : '这个月份未查到已记录流水。'
    }
    if (b?.kind === 'card_list' && /^共查询到 \d+ 张银行卡：/.test(message.text)) return `查到 ${b.data.length} 张银行卡，余额和状态见下方。`
    const report = cashFlowReport(result.detail)
    if (report && (message.text === report.explanation || message.text.startsWith(report.explanation + '：{'))) return '收支概览已整理好，明细可以按需展开。'
  }
  // Restored transcripts from the previous UI have no attached result cards.
  // Keep their historical period/count, but do not re-display serialized rows
  // or treat history as current account data. The original memory is unchanged.
  if (!message.results?.length) {
    const legacy = message.text.match(/^(\d{4}-\d{2} 共查询到 \d+ 条流水)：\[/)
    if (legacy) return `${legacy[1]}。这是历史查询，重新查询可查看当前明细。`
    const marker = message.text.indexOf('：{')
    if (marker > 0) {
      try {
        const report = cashFlowReport(JSON.parse(message.text.slice(marker + 1)))
        if (report && message.text.slice(0, marker) === report.explanation) return `${report.explanation}\n这是历史查询，重新查询可展开当前报告。`
      } catch { /* Preserve ordinary replies that are not known reports. */ }
    }
  }
  return message.text
}
