// 前端展示用的格式化工具

/** 金额格式化：¥1,234.50（人民币，千分位，2 位小数） */
export function formatYuan(n: number): string {
  return '¥' + Number(n).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

/** 百分比：17.71% */
export function formatPct(n: number): string {
  return n.toFixed(2) + '%'
}

/** 日期：2026-09-01 → 09-01 */
export function formatDate(iso: string): string {
  return iso.slice(5)
}

/** 风险等级中文 */
export function riskName(level: string): string {
  const map: Record<string, string> = {
    R1: '保守型', R2: '稳健型', R3: '平衡型', R4: '进取型', R5: '激进型',
  }
  return map[level] ?? level
}

export function riskColor(level: string): string {
  const map: Record<string, string> = {
    R1: 'bg-emerald-100 text-emerald-700',
    R2: 'bg-teal-100 text-teal-700',
    R3: 'bg-sky-100 text-sky-700',
    R4: 'bg-amber-100 text-amber-700',
    R5: 'bg-rose-100 text-rose-700',
  }
  return map[level] ?? 'bg-gray-100 text-gray-700'
}

export function transferRiskColor(level: string): string {
  const map: Record<string, string> = {
    low: 'bg-emerald-100 text-emerald-700',
    medium: 'bg-amber-100 text-amber-700',
    high: 'bg-rose-100 text-rose-700',
  }
  return map[level] ?? 'bg-gray-100 text-gray-700'
}
