"""Deterministic Chinese presentation of typed query relations, no routing."""
from datetime import date
from decimal import Decimal
from .projection import safe_text


def cash(cents):return '无法计算（无匹配记录或指标未定义）' if cents is None else f'{Decimal(str(cents))/100:.2f}元'
def quote(value):return '“'+safe_text(str(value))+'”'
def span(value):
 if not value:return '当前已记录范围'
 start=date.fromisoformat(value['start']);end=date.fromisoformat(value['end_exclusive'])
 if start.day==1 and end==date(start.year+start.month//12,start.month%12+1,1):return f'{start.year}年{start.month}月'
 return f'{start.year}年{start.month}月{start.day}日至{end.year}年{end.month}月{end.day}日（不含结束日）'


def scope(s):
 bits=[span(s.get('period'))]
 bits.append('尾号'+s['card_tail']+'的付款卡' if s.get('card_tail') else '本次指定的付款卡' if s.get('card_id') else '本人全部卡片')
 if s.get('merchants'):bits.append('商户'+ '、'.join(quote(v) for v in s['merchants']))
 if s.get('exclude_merchants'):bits.append('排除'+ '、'.join(quote(v) for v in s['exclude_merchants']))
 if s.get('categories'):bits.append('分类'+ '、'.join(quote(v) for v in s['categories']))
 if s.get('direction'):bits.append({'income':'收入方向','expense':'支出方向'}[s['direction']])
 for a in s.get('amounts',[]):bits.append('单笔金额'+{'gt':'大于','gte':'不低于','lt':'小于','lte':'不高于','eq':'等于'}[a['operator']]+cash(a['cents']))
 if s.get('statuses'):bits.append('状态'+ '、'.join({'success':'成功','pending':'待处理','failed':'失败','closed':'关闭'}[v] for v in s['statuses']))
 return '，'.join(bits)


def accounting(s):
 from data_query import KINDS,Selection,selected_kind
 kinds=selected_kind(Selection.model_validate(s))
 return '、'.join(KINDS.get(k,k) for k in kinds)


def query(entry,*,outer_truncated=False,compact=True):
 cur=entry['current'];s=cur['scope'];plan=entry['plan'];a=cur['summary'];metrics=plan['metrics']
 text=scope(s)+'的'+accounting(s)
 if plan.get('time_mode','period')!='period':text='截至'+str(cur.get('as_of') or '查询时')+'，最近记录（'+('秒级事件时间' if cur.get('time_precision')=='second' else '部分记录仅有日期，日内先后可能未知')+'）中，'+text
 if 'sum' in metrics:text+='合计'+cash(a['amount_cents'])
 if 'average' in metrics:text+='，平均每笔'+cash(a['average_cents'])
 if 'net' in metrics:text+='，购买额减退款额'+cash(a['net_cents'])+'（不是逐单结清的净消费）'
 recent=plan.get('time_mode','period')!='period'
 shown_count=len(cur.get('details',[])) if recent else a['count']
 text+='，共'+str(shown_count)+'笔。'
 if not shown_count:text+='未查到匹配记录，不代表实际没有交易。'
 if not recent:
  if s.get('kinds') and 'refund' not in s['kinds'] or s.get('direction')=='expense':text+='本次未核实退款。'
  else:text+='本筛选内退款单列'+cash(a['refund_cents'])+'。'
 comparison=entry.get('comparison')
 if comparison:
  text+='比较基期为'+scope(comparison['base']['scope'])+'。'
  for metric,v in comparison.get('metrics',{}).items():
   label={'sum':'合计','count':'笔数','average':'平均每笔','net':'购买额减退款额'}[metric]
   value=lambda x: (str(x)+'笔' if metric=='count' else cash(x)) if x is not None else '无法计算'
   text+=label+'基期'+value(v['base'])+'，差额'+value(v['delta'])+'，变化率'+(v['change_percent']+'%' if v['change_percent'] is not None else '未定义')+'。'
  if comparison['status']!='recorded_comparison':text+='比较存在空记录、零基数或口径限制，不能推断完整实际变化。'
 details=cur.get('details',[])
 if cur.get('date_tie'):text+='存在同日记录，不能判断日内先后。'
 if 'details' in metrics:
  from data_query import KINDS
  # The full typed rows remain in the expandable result. Keep the first ranked
  # relation and necessary scope in the answer instead of repeating six rows.
  for row in details[:1 if compact else 6]:
   stamp=str(row.get('event_at') or (str(row['day'])+'（仅日期）')).replace('T',' ')
   provenance='（演示规则生成时间）' if row.get('time_source')=='demo_rule_v2' else ''
   text+=stamp+provenance+'，'+quote(row['merchant'])+'，'+('尾号'+str(row['card_tail'])+'，' if row.get('card_tail') else '')+{'income':'收入','expense':'支出'}[row['direction']]+KINDS[row['kind']]+cash(row['amount_cents'])+'，已入账；'
 groups=cur.get('groups',[])
 if plan.get('group_by'):
  for row in groups[:6]:
   values=[]
   if 'sum' in metrics:values.append('合计'+cash(row['amount_cents']))
   if 'count' in metrics:values.append(str(row['count'])+'笔')
   if 'average' in metrics:values.append('平均每笔'+cash(row['average_cents']))
   text+=quote(row['label'])+'：'+'，'.join(values)+'；'
 if 'details' in metrics or plan.get('group_by'):text+='按'+{'amount':'金额','count':'笔数','average':'平均金额','date':'日期'}[plan.get('sort_by','amount')]+('从高到低' if plan.get('order','desc')=='desc' else '从低到高')+'展示。'
 if compact and len(details)>1:text+='正文仅列排序首条，其余明细见下方详情。'
 if cur.get('truncated') or cur.get('next_offset') is not None or outer_truncated or len(groups)>6 or (not compact and len(details)>6):text+='明细或分组仅部分展示，可继续翻页。'
 text+='仅统计已记录且成功的人民币流水，实际覆盖未知。'
 return text


def task(value):
 """Typed compact task snapshot, never infer funds success from turn status."""
 if isinstance(value,list):return '；'.join(task(v) for v in value[:8])
 if not isinstance(value,dict):return '任务详情暂无法核实。'
 states={'active':'处理中','waiting':'等待处理','waiting_confirmation':'等待独立确认','pending':'待处理','completed':'完成','failed':'失败','cancelled':'已取消','paused':'已暂停','scheduled':'已预约','manual':'人工处理中','not_run':'未核验','passed':'核验通过','unknown':'结果未知'}
 text='任务状态：'+states.get(value.get('status'),'请查看任务详情')+'。'
 for step in value.get('steps',[]):
  a=step.get('arguments') or {};name=step.get('tool_name');label={'transfer_money':'转账','set_daily_limit':'调整限额','report_card_lost':'挂失','cancel_subscription':'取消订阅','buy_wealth_product':'理财申购','redeem_wealth_product':'理财赎回'}.get(name,'业务步骤')
  text+=label+'：'
  if a.get('from_card_id'):text+='从已选付款卡，'
  if a.get('recipient_name') or a.get('recipient_id'):text+='向'+(quote(a['recipient_name']) if a.get('recipient_name') else '已选收款人')+'，'
  if a.get('amount') is not None:text+='金额'+f"{Decimal(str(a['amount'])):.2f}元，"
  text+=states.get(step.get('status'),'状态待核实')+'，'+states.get(step.get('verification_status'),'核验状态待核实')+'。'
 return text
