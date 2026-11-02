import { useState } from 'react'
import type { UiInteraction, UiTask, UiError, StepStatus } from '../../types'
import { ERROR_HINT } from '../uiMaps'

const FIELD_LABEL: Record<string, string> = {
  month: '查询月份',
  card_id: '银行卡',
  recipient_id: '收款人',
  amount: '金额',
  product_id: '理财产品',
  holding_id: '持仓',
  time_range: '查询时间范围',
  query_type: '想了解的内容',
  from_card_id: '付款银行卡',
  recipient_name: '收款人姓名',
  subscription: '订阅名称',
  daily_limit: '每日限额',
}

function ConfirmationCard({ interaction, onConfirm, onCancel }: {
  interaction: UiInteraction
  onConfirm: (text: string) => void
  onCancel: () => void
}) {
  const [phrase, setPhrase] = useState('')
  const isDynamic = interaction.confirmation_mode === 'dynamic_phrase'

  if (isDynamic) {
    // 动态短语：仅展示，禁止前端自动代填或自动提交
    return (
      <div className="rounded-2xl bg-webank-50 border border-webank-100 px-4 py-3">
        <div className="text-sm text-webank-700 font-medium mb-1">需要动态确认</div>
        <p className="text-xs text-slate-500 mb-2">请手动输入下方确认短语以完成验证：</p>
        <div className="font-mono text-base font-bold text-webank-700 tracking-wide bg-white rounded-lg px-3 py-2 mb-2">
          {interaction.confirmation_phrase}
        </div>
        <input
          value={phrase}
          onChange={(e) => setPhrase(e.target.value)}
          placeholder="在此输入确认短语"
          className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm outline-none focus:border-webank-400"
        />
        <div className="flex gap-2 mt-3">
          <button
            onClick={() => onConfirm(phrase)}
            disabled={!phrase}
            className="flex-1 rounded-lg bg-webank-500 text-white text-sm py-2 font-medium disabled:opacity-40"
          >
            确认执行
          </button>
          <button onClick={onCancel} className="flex-1 rounded-lg bg-white text-slate-600 border border-slate-200 text-sm py-2 font-medium">
            取消
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="rounded-2xl bg-webank-50 border border-webank-100 px-4 py-3">
      <div className="text-sm text-webank-700 font-medium mb-3">请确认是否执行该操作</div>
      <div className="flex gap-2">
        <button onClick={() => onConfirm('确认')} className="flex-1 rounded-lg bg-webank-500 text-white text-sm py-2 font-medium">
          确认
        </button>
        <button onClick={onCancel} className="flex-1 rounded-lg bg-white text-slate-600 border border-slate-200 text-sm py-2 font-medium">
          取消
        </button>
      </div>
    </div>
  )
}

function ClarificationCard({ missingFields, onSubmit }: {
  missingFields: string[]
  onSubmit: (text: string) => void
}) {
  const [values, setValues] = useState<Record<string, string>>({})

  const renderControl = (field: string) => {
    const common = 'w-full rounded-lg border border-slate-200 px-3 py-2 text-sm outline-none focus:border-webank-400'
    if (field === 'month') return <input type="month" value={values[field] ?? ''} onChange={(e) => setValues((v) => ({ ...v, [field]: e.target.value }))} className={common} />
    if (field === 'amount') return <input type="number" step="0.01" placeholder="0.00" value={values[field] ?? ''} onChange={(e) => setValues((v) => ({ ...v, [field]: e.target.value }))} className={common} />
    if (['card_id', 'recipient_id', 'product_id', 'holding_id'].includes(field)) return <input type="number" placeholder="编号" value={values[field] ?? ''} onChange={(e) => setValues((v) => ({ ...v, [field]: e.target.value }))} className={common} />
    return <input placeholder="请输入" value={values[field] ?? ''} onChange={(e) => setValues((v) => ({ ...v, [field]: e.target.value }))} className={common} />
  }

  const submit = () => {
    const parts = missingFields.filter(f => values[f]?.trim()).map(f => `${FIELD_LABEL[f] || '补充信息'}是${values[f]}`)
    if (parts.length === 0) return
    onSubmit(parts.join(' '))
  }

  return (
    <div className="rounded-2xl bg-amber-50 border border-amber-100 px-4 py-3">
      <div className="text-sm text-amber-700 font-medium mb-2">需要补充信息</div>
      <div className="space-y-2">
        {missingFields.map((f) => (
          <div key={f}>
            <label className="text-xs text-slate-500">{FIELD_LABEL[f] ?? '需要补充的内容'}</label>
            {renderControl(f)}
          </div>
        ))}
      </div>
      <button onClick={submit} className="w-full mt-3 rounded-lg bg-webank-500 text-white text-sm py-2 font-medium">
        提交
      </button>
    </div>
  )
}

const STEP_STYLE: Record<StepStatus, { dot: string; text: string; label: string }> = {
  completed: { dot: 'bg-emerald-500', text: 'text-emerald-700', label: '已完成' },
  running: { dot: 'bg-sky-500 animate-pulse', text: 'text-sky-700', label: '执行中' },
  waiting_confirmation: { dot: 'bg-amber-500', text: 'text-amber-700', label: '待确认' },
  waiting_clarification: { dot: 'bg-amber-500', text: 'text-amber-700', label: '待澄清' },
  pending: { dot: 'bg-slate-300', text: 'text-slate-400', label: '待执行' },
  skipped: { dot: 'bg-slate-300', text: 'text-slate-400', label: '条件不满足，已跳过' },
  failed: { dot: 'bg-rose-500', text: 'text-rose-700', label: '失败' },
  cancelled: { dot: 'bg-slate-300', text: 'text-slate-400', label: '已取消' },
}

function TaskTimeline({ task }: { task: UiTask }) {
  return (
    <div aria-label="任务执行时间线" className="rounded-2xl bg-white shadow-card px-4 py-3 border border-slate-100">
      <div className="flex items-center justify-between mb-3">
        <h4 className="text-sm font-semibold text-slate-800">任务进度</h4>
        <span className="text-[11px] text-slate-500">{task.steps.filter(s => s.status === 'completed').length}/{task.steps.length} 步完成</span>
      </div>
      <div className="space-y-3">
        {task.steps.map((step, i) => {
          const s = STEP_STYLE[step.status]
          const last = i === task.steps.length - 1
          return (
            <div key={step.step_id} className="flex gap-3">
              <div className="flex flex-col items-center">
                <div className={`w-3 h-3 rounded-full ${s.dot} mt-1.5`} />
                {!last && <div className="w-px flex-1 bg-slate-200" />}
              </div>
              <div className="pb-1">
                <div className={`text-sm ${s.text} font-medium`}>{step.title}</div>
                <div className="text-[11px] text-slate-400">{s.label}</div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function ErrorCard({ error }: { error: UiError }) {
  return (
    <div className="rounded-2xl bg-rose-50 border border-rose-100 px-4 py-3">
      <div className="flex items-center gap-2 mb-1">
        <span className="text-rose-500 font-semibold text-sm">操作未完成</span>
        <span className="text-[11px] px-2 py-0.5 rounded-full bg-rose-100 text-rose-600 font-mono">{error.code}</span>
      </div>
      <p className="text-sm text-rose-700">{error.message || ERROR_HINT[error.code]}</p>
    </div>
  )
}

export { ConfirmationCard, ClarificationCard, TaskTimeline, ErrorCard }
