import type { UiStatus, UiErrorCode } from '../types'

export const STATUS_META: Record<UiStatus, { label: string; tone: string }> = {
  ready: { label: '就绪', tone: 'text-gray-600 bg-gray-100' },
  need_info: { label: '需补充信息', tone: 'text-amber-700 bg-amber-100' },
  need_confirmation: { label: '待确认', tone: 'text-webank-700 bg-webank-100' },
  executing: { label: '执行中', tone: 'text-sky-700 bg-sky-100' },
  success: { label: '成功', tone: 'text-emerald-700 bg-emerald-100' },
  cancelled: { label: '已取消', tone: 'text-gray-600 bg-gray-100' },
  expired: { label: '已过期', tone: 'text-rose-700 bg-rose-100' },
  failed: { label: '失败', tone: 'text-rose-700 bg-rose-100' },
}

export const ERROR_HINT: Record<UiErrorCode, string> = {
  interaction_expired: '该操作确认已过期，请重新发起。',
  not_found: '未找到对应数据。',
  risk_rejected: '风控未通过，已拒绝执行。',
  insufficient_funds: '付款卡余额不足，未执行任何扣款。',
  limit_exceeded: '超出该卡当日交易限额，未执行任何扣款。',
  validation_failed: '请求字段校验失败。',
  verification_failed: '操作可能已经提交，需要人工核对；系统不会自动重试。',
  duplicate_operation: '该请求已处理过，未重复执行。',
  tool_execution_failed: '系统繁忙，请稍后重试。',
  service_unavailable: '服务暂不可用，请稍后再试。',
  operation_failed: '操作执行失败。',
  agent_error: '智能助理出现错误。',
  contract_payload_mismatch: '返回结构异常，请联系技术支持。',
}
