"""Bounded financial query compiler. No model SQL, owner or executable expressions.

All source selection, catalog resolution and calculations share a single verified
REPEATABLE READ READ ONLY snapshot. Aggregates never use a paginated detail set.
"""
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import time
from typing import Literal

from pydantic import Field, model_validator
from agent.schemas import StrictModel
from billvision.provider import VisionError


class Period(StrictModel):
    start: date
    end_exclusive: date

    @model_validator(mode='after')
    def bounded(self):
        if not 0 < (self.end_exclusive-self.start).days <= 1096:
            raise ValueError('date_range_must_be_1_to_1096_days')
        return self


class Amount(StrictModel):
    operator: Literal['gt','gte','lt','lte','eq']
    cents: int = Field(ge=0,le=10**14,strict=True)


class Selection(StrictModel):
    source: Literal['bank','uploaded'] = 'bank'
    document_id: str | None = Field(None,pattern=r'^bv_[0-9a-f]{32}$')
    current_document: bool = False
    period: Period | None = None
    card_tail: str | None = Field(None,pattern=r'^[0-9]{4}$')
    card_id: int | None = Field(None,ge=1,strict=True)
    merchants: list[str] = Field(default_factory=list,max_length=8)
    exclude_merchants: list[str] = Field(default_factory=list,max_length=8)
    categories: list[str] = Field(default_factory=list,max_length=8)
    amounts: list[Amount] = Field(default_factory=list,max_length=2)
    direction: Literal['income','expense'] | None = None
    kinds: list[Literal['purchase','refund','transfer','income','wealth_buy','wealth_redeem','unknown']] = Field(default_factory=list,max_length=7)
    statuses: list[Literal['success','pending','failed','closed']] = Field(default_factory=list,max_length=4)

    @model_validator(mode='after')
    def valid(self):
        for values in (self.merchants,self.exclude_merchants,self.categories):
            if any(not v.strip() or len(v)>120 for v in values) or len(set(values))!=len(values):
                raise ValueError('invalid_catalog_values')
        if self.source=='bank' and (self.document_id or self.current_document):
            raise ValueError('bank_cannot_have_upload_reference')
        if any(getattr(self,k) for k in ['card_id','card_tail','categories'] if k not in SOURCES[self.source]['fields']):
            raise ValueError('uploaded_has_no_verified_bank_card_or_category')
        if self.card_id and self.card_tail:
            raise ValueError('choose_card_id_or_tail')
        return self


class QueryPlan(StrictModel):
    selection: Selection
    time_mode: Literal['period','latest','recent_records'] = 'period'
    refunds_separate: bool = False
    metrics: list[Literal['sum','count','average','details','net']] = Field(default_factory=lambda:['sum','count'],min_length=1,max_length=5)
    group_by: Literal['merchant','category','day','month'] | None = None
    sort_by: Literal['amount','count','average','date'] = 'amount'
    order: Literal['asc','desc'] = 'desc'
    limit: int = Field(default=20,ge=1,le=100,strict=True)
    offset: int = Field(default=0,ge=0,le=10000,strict=True)
    comparison: Selection | None = None

    @model_validator(mode='after')
    def supported(self):
        if self.time_mode!='period' and not self.selection.kinds:
            self.selection.kinds=(['purchase','transfer','wealth_buy','unknown'] if self.selection.direction=='expense' else
              ['income','refund','wealth_redeem','unknown'] if self.selection.direction=='income' else
              ['purchase','refund','transfer','income','wealth_buy','wealth_redeem','unknown'])
        if len(set(self.metrics))!=len(self.metrics): raise ValueError('duplicate_metric')
        if self.offset and (self.time_mode!='period' or self.group_by or 'details' not in self.metrics):
            raise ValueError('offset_requires_period_detail_query')
        if self.group_by=='category' and self.selection.source=='uploaded':
            raise ValueError('uploaded_category_unavailable')
        if self.group_by and 'details' in self.metrics:
            raise ValueError('group_and_details_are_separate_goals')
        if self.group_by and 'net' in self.metrics and self.metrics!=['net']:
            raise ValueError('grouped_net_requires_net_only; query_other_metrics_as_separate_goal')
        if self.time_mode!='period' and (self.group_by or self.comparison or self.metrics!=['details'] or self.sort_by not in {'amount','date'} or self.order!='desc'):
            raise ValueError('recent_records_requires_descending_details_only')
        if self.group_by and self.sort_by=='date' and self.group_by not in {'day','month'}:
            raise ValueError('date_sort_requires_temporal_group')
        if self.comparison and (self.group_by or 'details' in self.metrics):
            raise ValueError('comparison_requires_scalar_metrics')
        if 'net' in self.metrics and (self.selection.kinds or self.selection.direction or
                self.comparison and (self.comparison.kinds or self.comparison.direction)):
            raise ValueError('net_requires_both_purchase_and_refund')
        return self


class QueryBatch(StrictModel):
    plans: list[QueryPlan] = Field(min_length=1,max_length=4)


class KindTotal(StrictModel):
    kind: Literal['purchase','refund','transfer','income','wealth_buy','wealth_redeem','unknown']
    status: Literal['success','pending','failed','closed']
    currency: str = Field(min_length=3,max_length=3)
    count: int = Field(ge=0,strict=True)
    amount_cents: int = Field(ge=0,strict=True)


class Summary(StrictModel):
    recorded_count: int = Field(ge=0,strict=True)
    matched_count: int = Field(ge=0,strict=True)
    excluded_count: int = Field(ge=0,strict=True)
    purchase_cents: int = Field(ge=0,strict=True)
    refund_cents: int = Field(ge=0,strict=True)
    amount_cents: int = Field(ge=0,strict=True)
    count: int = Field(ge=0,strict=True)
    average_cents: str | None
    net_cents: int | None
    kinds: list[KindTotal] = Field(max_length=64)


class GroupRow(StrictModel):
    label: str = Field(max_length=120)
    amount_cents: int = Field(strict=True)
    count: int = Field(ge=1,strict=True)
    average_cents: str


class DetailRow(StrictModel):
    record_id: str = Field(pattern=r'^(?:[0-9]{1,19}|bv_[0-9a-f]{32}:r[0-9]{1,3})$')
    day: date
    merchant: str = Field(max_length=120)
    category: str | None = Field(max_length=120)
    amount_cents: int = Field(gt=0,strict=True)
    direction: Literal['expense','income']
    kind: Literal['purchase','refund','transfer','income','wealth_buy','wealth_redeem','unknown']
    status: Literal['success','pending','failed','closed']
    currency: str = Field(min_length=3,max_length=3)
    card_id: int | None
    card_tail: str | None = Field(default=None,pattern=r'^\d{4}$')
    document_id: str | None = Field(pattern=r'^bv_[0-9a-f]{32}$')
    revision: int = Field(ge=0,strict=True)
    event_at: str | None = None
    time_source: Literal['transfer_receipt','wealth_receipt','demo_rule_v2','date_only'] = 'date_only'

    @model_validator(mode='after')
    def event_precision(self):
        if self.event_at is None:
            if self.time_source!='date_only':raise ValueError('missing_event_time')
        else:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            stamp=datetime.fromisoformat(self.event_at)
            if stamp.tzinfo is None or stamp.astimezone(ZoneInfo('Asia/Shanghai')).date()!=self.day or self.time_source=='date_only':
                raise ValueError('event_time_provenance_mismatch')
        return self


class PeriodResult(StrictModel):
    scope: Selection
    summary: Summary
    groups: list[GroupRow] = Field(max_length=100)
    details: list[DetailRow] = Field(max_length=100)
    truncated: bool
    date_tie: bool = False
    time_precision: Literal['day','second','mixed'] = 'day'
    as_of: date | None = None
    page_offset: int = Field(default=0,ge=0,le=10000,strict=True)
    next_offset: int | None = Field(default=None,ge=0,le=10000,strict=True)
    coverage: Literal['仅为范围内已记录项目，无法保证完整覆盖；空记录不代表没有消费。']
    accounting: Literal['购买和退款按各自发生日单列；转账、理财资金变动、其他收入及未知项目分开；非成功/非CNY不计入金额。']
    net_label: Literal['该范围购买额减退款额；不是逐单结清的净消费'] | None

    @model_validator(mode='after')
    def totals_match_classifications(self):
        a=self.summary;rows=a.kinds
        eligible=[r for r in rows if r.currency=='CNY' and r.status=='success' and r.kind!='unknown']
        selected=[r for r in eligible if r.kind in selected_kind(self.scope)]
        if (a.recorded_count!=sum(r.count for r in rows) or a.matched_count!=sum(r.count for r in eligible)
                or a.excluded_count!=a.recorded_count-a.matched_count or a.count!=sum(r.count for r in selected)
                or a.amount_cents!=sum(r.amount_cents for r in selected)
                or a.refund_cents!=sum(r.amount_cents for r in eligible if r.kind=='refund')
                or a.purchase_cents!=sum(r.amount_cents for r in eligible if r.kind=='purchase')):
            raise ValueError('aggregate_classification_mismatch')
        avg=str((Decimal(a.amount_cents)/a.count).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)) if a.count else None
        if a.average_cents!=avg or a.net_cents is not None and a.net_cents!=a.purchase_cents-a.refund_cents:
            raise ValueError('computed_metric_mismatch')
        for row in self.details:
            if self.scope.period and not self.scope.period.start<=row.day<self.scope.period.end_exclusive:
                raise ValueError('detail_period_mismatch')
            if self.scope.card_id and row.card_id!=self.scope.card_id:raise ValueError('detail_card_mismatch')
            if self.scope.document_id and row.document_id!=self.scope.document_id:raise ValueError('detail_document_mismatch')
            if row.currency!='CNY' or row.status!='success' or row.kind not in selected_kind(self.scope):
                raise ValueError('detail_metric_mismatch')
        return self


class MetricComparison(StrictModel):
    unit: Literal['分','笔']
    current: str | None
    base: str | None
    delta: str | None
    change_percent: str | None


class ComparisonResult(StrictModel):
    base: PeriodResult
    change_cents: int | str | None
    change_percent: str | None
    metrics: dict[Literal['sum','count','average','net'],MetricComparison] = Field(default_factory=dict)
    status: Literal['different_sources_no_merge','missing_records','undefined_metric','zero_base','recorded_comparison']


class EntryResult(StrictModel):
    plan: QueryPlan
    current: PeriodResult
    comparison: ComparisonResult | None

    @model_validator(mode='after')
    def checked(self):
        if self.current.page_offset!=self.plan.offset:raise ValueError('page_offset_mismatch')
        next_offset=self.plan.offset+len(self.current.details) if self.plan.time_mode=='period' and 'details' in self.plan.metrics and self.current.truncated else None
        if next_offset is not None and next_offset>10000:next_offset=None
        if self.current.next_offset!=next_offset:raise ValueError('page_cursor_mismatch')
        for planned,actual in [(self.plan.selection,self.current.scope),
                (self.plan.comparison,self.comparison.base.scope if self.comparison else None)]:
            if planned and actual:
                for key in Selection.model_fields:
                    if key in {'card_tail','card_id','current_document','document_id'}:continue
                    if getattr(planned,key)!=getattr(actual,key):raise ValueError('plan_scope_mismatch')
                if planned.document_id and planned.document_id!=actual.document_id:raise ValueError('plan_document_mismatch')
                if planned.current_document and not actual.document_id:raise ValueError('unresolved_document')
                if planned.card_id and planned.card_id!=actual.card_id:raise ValueError('plan_card_mismatch')
        if ('net' in self.plan.metrics)!=(self.current.summary.net_cents is not None):raise ValueError('net_metric_mismatch')
        if bool(self.comparison)!=bool(self.plan.comparison):raise ValueError('comparison_missing')
        if self.comparison:
            a=self.current.summary;b=self.comparison.base.summary
            comparable=self.current.scope.source==self.comparison.base.scope.source
            metric=primary_metric(self.plan)
            av=metric_value(a.model_dump(),metric)
            bv=metric_value(b.model_dump(),metric)
            missing=not a.recorded_count or not b.recorded_count
            delta=av-bv if comparable and not missing and av is not None and bv is not None else None
            pct=str((Decimal(delta)*100/Decimal(bv)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)) if delta is not None and bv else None
            legacy_delta=delta if metric in {'sum','net'} else None
            if (self.comparison.change_cents,self.comparison.change_percent)!=(legacy_delta,pct):raise ValueError('comparison_number_mismatch')
            expected=compare_metrics(self.plan,a.model_dump(),b.model_dump(),comparable)
            if {k:v.model_dump() for k,v in self.comparison.metrics.items()}!=expected:raise ValueError('comparison_metric_mismatch')
            status='different_sources_no_merge' if not comparable else 'missing_records' if missing else 'undefined_metric' if av is None or bv is None else 'zero_base' if not bv else 'recorded_comparison'
            if self.comparison.status!=status:raise ValueError('comparison_status_mismatch')
        if not self.plan.group_by and self.current.groups:raise ValueError('unrequested_groups')
        if 'details' not in self.plan.metrics and self.current.details:raise ValueError('unrequested_details')
        if self.plan.group_by and 'net' in self.plan.metrics:
            eligible=[r for r in self.current.summary.kinds if r.kind in {'purchase','refund'} and r.currency=='CNY' and r.status=='success']
            if not self.current.truncated and (sum(g.amount_cents for g in self.current.groups)!=self.current.summary.net_cents or sum(g.count for g in self.current.groups)!=sum(r.count for r in eligible)):
                raise ValueError('net_group_totals_mismatch')
        elif sum(g.amount_cents for g in self.current.groups)>self.current.summary.amount_cents or sum(g.count for g in self.current.groups)>self.current.summary.count or any(g.amount_cents<0 for g in self.current.groups):
            raise ValueError('group_totals_exceed_scope')
        for g in self.current.groups:
            if g.average_cents!=str((Decimal(g.amount_cents)/g.count).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)):
                raise ValueError('group_average_mismatch')
        return self


class DataResult(StrictModel):
    version: Literal['data-query-1']
    result_id: str = Field(pattern=r'^dq_[0-9a-f]{24}$')
    results: list[EntryResult] = Field(min_length=1,max_length=4)
    source_note: Literal['银行与上传账单分别展示，未做跨来源去重，不能相加作为完整消费。']


def verify_output(data):
    """Schema, independent aggregate identities, fingerprint and sensitive scan."""
    checked=DataResult.model_validate(data)
    expected='dq_'+hashlib.sha256(json.dumps(data['results'],sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:24]
    if data['result_id']!=expected:raise ValueError('result_binding_mismatch')
    safe=checked.model_dump(mode='json')
    # Exact validated paths only, not arbitrary keys with these names.
    safe['result_id']='verified result'
    for entry in safe['results']:
        for s in [entry['plan']['selection'],entry['plan']['comparison']]:
            if s and s['document_id']:s['document_id']='verified upload'
        for period in [entry['current'],entry['comparison']['base'] if entry['comparison'] else None]:
            if not period:continue
            if period['scope']['document_id']:period['scope']['document_id']='verified upload'
            for row in period['details']:
                row['record_id']='verified record'
                if row['document_id']:row['document_id']='verified upload'
    from agent.output_guard import contains_sensitive_result
    if contains_sensitive_result(safe):raise ValueError('sensitive_query_result')
    return checked


def problem(code, message):
    raise VisionError(code, message, 422)


def catalogs(c,requested=None,text=None):
    if requested is not None:
        candidates=sorted(set(n for s in requested for n in [*s.merchants,*s.exclude_merchants]))
        records=c.execute('''SELECT merchant FROM pg02.transactions WHERE merchant=ANY(%s) OR (%s::text IS NOT NULL AND position(merchant in %s::text)>0)
            UNION SELECT value->>'merchant' FROM billvision.records WHERE value->>'merchant'=ANY(%s) OR (%s::text IS NOT NULL AND position(value->>'merchant' in %s::text)>0) LIMIT 65''',
            (candidates,text,text,candidates,text,text)).fetchall()
        names=sorted({r['merchant'] for r in records if r['merchant']})
        if len(names)>64:problem('catalog_limit','本次涉及商户过多，请缩小目标。')
        alias_names=sorted(set(names+candidates))
        aliases=c.execute('SELECT alias,merchant_key FROM pg02.merchant_aliases WHERE merchant_key IN (SELECT merchant_key FROM pg02.merchant_aliases WHERE alias=ANY(%s)) ORDER BY alias',(alias_names,)).fetchall() if alias_names else []
        return dict(merchants=names,aliases=[dict(r) for r in aliases],
            categories=[r['category'] for r in c.execute('SELECT DISTINCT category FROM pg02.transactions ORDER BY category LIMIT 129')])
    names = c.execute('''SELECT merchant FROM pg02.transactions
        UNION SELECT value->>'merchant' FROM billvision.records LIMIT 513''').fetchall()
    if len(names)>512: problem('catalog_limit','商户目录过大，请提供明确商户名称后重试。')
    aliases = c.execute('SELECT alias,merchant_key FROM pg02.merchant_aliases ORDER BY alias LIMIT 513').fetchall()
    if len(aliases)>512: problem('catalog_limit','商户别名目录超出本轮限制。')
    # Alias catalog is server-maintained; exact owner-scoped values are also valid.
    return dict(merchants=sorted({r['merchant'] for r in names if r['merchant']}),
        aliases=[dict(r) for r in aliases],
        categories=[r['category'] for r in c.execute('SELECT DISTINCT category FROM pg02.transactions ORDER BY category LIMIT 129')])


def merchant_values(names, catalog):
    values=[]
    for name in names:
        keys={r['merchant_key'] for r in catalog['aliases'] if r['alias']==name}
        choices=({r['alias'] for r in catalog['aliases'] if r['merchant_key'] in keys} if keys else {name})
        if not keys and name not in catalog['merchants']:
            problem('merchant_unknown','没有找到这个确切商户，请核对名称；不会扩大到所有商户。')
        values += sorted(choices)
    return sorted(set(values))


def resolve(c, s, principal, session, catalog, time_mode='period'):
    s=s.model_copy(deep=True)
    if 'unknown' in s.kinds and time_mode=='period':
        problem('query_metric_unsupported','未知类型不能推断为购买或收入；当前金融问数不支持将未知类型作为统计目标，请在原流水或账单核对页查看这些原始记录。本次未查询其他类型代替。')
    if s.source=='uploaded' and any(status!='success' for status in s.statuses):
        problem('query_metric_unsupported','失败、待处理或关闭记录不能作为已成功消费统计；当前金融问数不支持这些状态的原始明细，请在账单核对页查看。本次未返回零消费或改查成功记录。')
    if s.source=='bank' and s.period is None and time_mode=='period':
        problem('query_time_required','想看哪段时间的记录？可以直接说月份或日期范围。')
    if s.current_document:
        ids=getattr(session,'billvision_documents',[]) if session else []
        if len(ids)!=1:
            problem('document_choice','当前会话没有唯一账单引用，请在账单识别面板选择一份。')
        if s.document_id and s.document_id!=ids[0]:
            problem('document_mismatch','账单引用已变化，请重新选择。')
        s.document_id=ids[0];s.current_document=False
    if s.document_id:
        d=c.execute('SELECT revision,state FROM billvision.documents WHERE document_id=%s',(s.document_id,)).fetchone()
        if not d: problem('document_unavailable','该账单已删除、引用失效或不属于当前账户，请重新选择。')
        if d['state']!='confirmed':
            problem('review_required','这份账单仍待核对确认，不能当作已保存记录查询。')
    if s.card_id or s.card_tail:
        sql='SELECT public_id FROM core.cards WHERE owner_id=core.require_owner() AND '+('right(masked_number,4)=%s' if s.card_tail else 'public_id=%s')
        cards=c.execute(sql,(s.card_tail or s.card_id,)).fetchall()
        if len(cards)!=1: problem('card_choice','这张卡不能唯一对应本人的银行卡，请核对卡片尾号或编号。')
        s.card_id=cards[0]['public_id'];s.card_tail=None
    if s.source=='uploaded':
        if any(k in s.kinds for k in ['income','wealth_buy','wealth_redeem']):
            problem('source_field_unavailable','上传账单没有可核实的该交易分类，不能忽略这个条件。')
    elif s.statuses and s.statuses!=['success']:
        problem('source_field_unavailable','银行流水仅记录已入账项目，不能据此查询失败或待处理的交易。')
    for value in s.categories:
        if value not in catalog['categories']:problem('category_unknown','该分类没有可核实的记录，请核对分类名称。')
    include=merchant_values(s.merchants,catalog);exclude=merchant_values(s.exclude_merchants,catalog)
    if set(include)&set(exclude):problem('conflicting_filter','商户包含和排除条件互相冲突，请明确保留哪个。')
    return s,include,exclude


BANK = '''SELECT t.transaction_id::text AS record_id,t.date AS day,t.merchant,t.category,
    CASE WHEN t.event_at IS NOT NULL THEN to_char(t.event_at AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD"T"HH24:MI:SS')||'+08:00' END AS event_at,
    coalesce(t.time_source,'date_only') AS time_source,
    (SELECT min(a2.alias) FROM pg02.merchant_aliases a1 JOIN pg02.merchant_aliases a2
      ON a1.merchant_key=a2.merchant_key WHERE a1.alias=t.merchant) AS merchant_group,
    t.amount_cents,t.direction,t.transaction_type AS kind,'success'::text AS status,
    a.currency,t.card_id,right(k.masked_number,4) AS card_tail,NULL::text AS document_id,0 AS revision
    FROM pg02.visible_transactions t JOIN core.cards k ON k.public_id=t.card_id AND k.owner_id=t.owner_uuid
    JOIN core.accounts a ON a.id=k.account_id AND a.owner_id=k.owner_id
    WHERE t.owner_uuid=core.require_owner()'''
UPLOAD = '''SELECT r.document_id||':'||r.row_id AS record_id,r.value->>'date' AS day,
    NULL::text AS event_at,'date_only'::text AS time_source,
    r.value->>'merchant' AS merchant,NULL::text AS category,
    (SELECT min(a2.alias) FROM pg02.merchant_aliases a1 JOIN pg02.merchant_aliases a2
      ON a1.merchant_key=a2.merchant_key WHERE a1.alias=r.value->>'merchant') AS merchant_group,
    (r.value->>'amount_cents')::bigint AS amount_cents,r.value->>'direction' AS direction,
    CASE WHEN r.value->>'kind'='other' THEN 'unknown' ELSE r.value->>'kind' END AS kind,
    r.value->>'status' AS status,r.value->>'currency' AS currency,NULL::integer AS card_id,NULL::text AS card_tail,
    r.document_id,r.revision
    FROM billvision.records r JOIN billvision.documents d
    ON d.owner_uuid=r.owner_uuid AND d.document_id=r.document_id AND d.state='confirmed'
    AND d.revision=r.revision WHERE r.owner_uuid=core.require_owner()'''
GROUP={'merchant':'coalesce(merchant_group,merchant)','category':'category','day':'day','month':'left(day,7)'}
OPS={'gt':'>','gte':'>=','lt':'<','lte':'<=','eq':'='}


def compiled(s, include, exclude, as_of=None):
    filters=[];params=[]
    def add(sql,value): filters.append(sql);params.append(value)
    if s.period:
        add('day>=%s',s.period.start.isoformat());add('day<%s',s.period.end_exclusive.isoformat())
    if as_of:add('day<=%s',as_of.isoformat())
    for field,value in [('card_id',s.card_id),('document_id',s.document_id),('direction',s.direction)]:
        if value is not None:add(field+'=%s',value)
    for field,values in [('merchant',include),('category',s.categories),('kind',s.kinds),('status',s.statuses)]:
        if values:add(field+'=ANY(%s)',values)
    if exclude:add('NOT (merchant=ANY(%s))',exclude)
    for a in s.amounts:add('amount_cents'+OPS[a.operator]+'%s',a.cents)
    return 'WITH source AS ('+(BANK if s.source=='bank' else UPLOAD)+'), scoped AS (SELECT * FROM source'+(' WHERE '+' AND '.join(filters) if filters else '')+') ',params


AGG = '''count(*) AS recorded_count,
    count(*) FILTER (WHERE currency='CNY' AND status='success' AND kind!='unknown') AS matched_count,
    count(*) FILTER (WHERE currency!='CNY' OR status!='success' OR kind='unknown') AS excluded_count,
    coalesce(sum(amount_cents) FILTER (WHERE currency='CNY' AND status='success' AND kind='purchase' AND direction='expense'),0) AS purchase_cents,
    coalesce(sum(amount_cents) FILTER (WHERE currency='CNY' AND status='success' AND kind='refund'),0) AS refund_cents'''


def integer_row(row):
    return {k:int(v) if isinstance(v,Decimal) else v for k,v in row.items()}


def selected_kind(s):
    # Default spending is purchase, never transfers/wealth/unknown. Refunds stay separate.
    return s.kinds or (['income','refund','wealth_redeem'] if s.direction=='income' else ['purchase'])


def summary_sql(s):
    kinds=selected_kind(s)
    return (AGG+''',coalesce(sum(amount_cents) FILTER (WHERE currency='CNY' AND status='success' AND kind=ANY(%s) AND kind!='unknown'),0) AS amount_cents,
        count(*) FILTER (WHERE currency='CNY' AND status='success' AND kind=ANY(%s) AND kind!='unknown') AS count''',[kinds,kinds])


def aggregate(c,p,s,include,exclude,as_of=None):
    cte,params=compiled(s,include,exclude,as_of)
    agg,metric_params=summary_sql(s)
    summary=integer_row(c.execute(cte+'SELECT '+agg+' FROM scoped',params+metric_params).fetchone())
    summary['average_cents']=(str((Decimal(summary['amount_cents'])/summary['count']).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)) if summary['count'] else None)
    summary['net_cents']=summary['purchase_cents']-summary['refund_cents'] if 'net' in p.metrics else None
    summary['kinds']=[integer_row(r) for r in c.execute(cte+'''SELECT kind,status,currency,count(*) AS count,sum(amount_cents) AS amount_cents
        FROM scoped GROUP BY kind,status,currency ORDER BY kind,status,currency LIMIT 65''',params)]
    if len(summary['kinds'])>64:problem('query_complexity','交易分类过多，请缩小范围。')
    groups=[];details=[];truncated=False
    if p.group_by:
        group=GROUP[p.group_by];sort={'amount':'amount_cents','count':'count','average':'average_cents::numeric','date':'label'}[p.sort_by]
        eligible="currency='CNY' AND status='success' AND kind=ANY(%s) AND kind!='unknown'"
        signed="CASE WHEN kind='refund' THEN -amount_cents ELSE amount_cents END" if 'net' in p.metrics else 'amount_cents'
        groups=[integer_row(r) for r in c.execute(cte+f'''SELECT * FROM (SELECT {group} AS label,sum({signed}) AS amount_cents,count(*) AS count,
            round(avg({signed}),2)::text AS average_cents FROM scoped WHERE {eligible} GROUP BY {group}) grouped
            ORDER BY {sort} {p.order.upper()},label ASC LIMIT %s''',params+[(['purchase','refund'] if 'net' in p.metrics else selected_kind(s)),p.limit+1])]
        truncated=len(groups)>p.limit;groups=groups[:p.limit]
    date_tie=False
    if 'details' in p.metrics:
        sort='day' if p.sort_by=='date' or p.time_mode!='period' else 'amount_cents'
        columns='record_id,day,event_at,time_source,merchant,category,amount_cents,direction,kind,status,currency,card_id,card_tail,document_id,revision'
        details=[integer_row(r) for r in c.execute(cte+f'''SELECT {columns} FROM scoped
            WHERE currency='CNY' AND status='success' AND kind=ANY(%s)
            ORDER BY {sort} {p.order.upper()},event_at {p.order.upper()} NULLS LAST,
            length(record_id) DESC,record_id DESC LIMIT %s OFFSET %s''',params+[selected_kind(s),p.limit+1,p.offset])]
        cutoff=details[min(len(details),p.limit)-1]['day'] if details else None
        unknown_boundary=bool(cutoff and p.time_mode!='period' and c.execute(cte+'''SELECT 1 FROM scoped
            WHERE currency='CNY' AND status='success' AND kind=ANY(%s) AND day>=%s AND event_at IS NULL LIMIT 1''',params+[selected_kind(s),cutoff]).fetchone())
        if p.time_mode!='period' and unknown_boundary and (len(details)>p.limit or any(r['event_at'] for r in details)):
            date_tie=True
            details=[integer_row(r) for r in c.execute(cte+f'''SELECT {columns} FROM scoped
                WHERE currency='CNY' AND status='success' AND kind=ANY(%s) AND day>=%s
                ORDER BY day DESC,record_id ASC LIMIT 101''',params+[selected_kind(s),cutoff])]
            if len(details)>100:problem('recent_date_tie_limit','边界日期有超过可展示数量的并列记录，日期精度无法判断先后，请补充商户或卡片。')
            return dict(scope=s.model_dump(mode='json'),summary=summary,groups=groups,details=details,
                truncated=True,date_tie=True,as_of=as_of.isoformat() if as_of else None,time_precision='day',page_offset=0,next_offset=None,
                coverage=COVERAGE,accounting=ACCOUNTING,net_label=None)
        truncated=len(details)>p.limit;details=details[:p.limit]
    next_offset=p.offset+len(details) if p.time_mode=='period' and 'details' in p.metrics and truncated else None
    if next_offset is not None and next_offset>10000:next_offset=None
    precision='second' if details and all(r['event_at'] for r in details) else 'mixed' if any(r['event_at'] for r in details) else 'day'
    return dict(scope=s.model_dump(mode='json'),summary=summary,groups=groups,details=details,page_offset=p.offset,next_offset=next_offset,
        truncated=truncated,date_tie=date_tie,as_of=as_of.isoformat() if as_of else None,time_precision=precision,coverage=COVERAGE,
        accounting=ACCOUNTING,
        net_label='该范围购买额减退款额；不是逐单结清的净消费' if 'net' in p.metrics else None)


COVERAGE='仅为范围内已记录项目，无法保证完整覆盖；空记录不代表没有消费。'
ACCOUNTING='购买和退款按各自发生日单列；转账、理财资金变动、其他收入及未知项目分开；非成功/非CNY不计入金额。'
# One semantic definition used by comparisons, verification and presentation.
METRICS={'sum':dict(field='amount_cents',unit='分',label='金额'),
    'count':dict(field='count',unit='笔',label='笔数'),
    'average':dict(field='average_cents',unit='分',label='平均单笔'),
    'net':dict(field='net_cents',unit='分',label='购买额减退款额')}
KINDS={'purchase':'购买消费','refund':'退款','transfer':'转账','income':'其他收入',
    'wealth_buy':'理财申购','wealth_redeem':'理财赎回','unknown':'未知'}
SOURCES={'bank':dict(precision='day',fields=['merchants','categories','card_id','card_tail','day']),
    'uploaded':dict(precision='day',fields=['merchant','document_id','revision','day'])}


def primary_metric(p):
    return next(k for k in ['net','sum','average','count'] if k in p.metrics)


def metric_value(summary,metric):
    value=summary[METRICS[metric]['field']]
    return Decimal(str(value)) if value is not None else None


def compare_metrics(p,a,b,comparable):
    out={}
    for metric in p.metrics:
        if metric not in METRICS:continue
        av=metric_value(a,metric);bv=metric_value(b,metric)
        delta=av-bv if comparable and a['recorded_count'] and b['recorded_count'] and av is not None and bv is not None else None
        percent=str((delta*100/bv).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)) if delta is not None and bv else None
        out[metric]=dict(unit=METRICS[metric]['unit'],current=str(av) if av is not None else None,
            base=str(bv) if bv is not None else None,delta=str(delta) if delta is not None else None,change_percent=percent)
    return out


class ReadBudget:
    def __init__(self,deadline=None):
        self.deadline=deadline or time.monotonic()+10
        self.statements=0

    def wrap(self,raw):
        budget=self
        class Bounded:
            def execute(self,query,params=None):
                remaining=budget.deadline-time.monotonic()
                if remaining<=0 or budget.statements>=64:
                    problem('query_budget_exceeded','本次查询超过时限或复杂度限制，请缩小范围后重试。')
                budget.statements+=1
                raw.execute("SELECT set_config('statement_timeout',%s,true)",(str(max(1,min(3000,int(remaining*1000)))),))
                return raw.execute(query,params)
        return Bounded()


def execute(principal,batch,session=None,*,budget=None):
    from billvision.service import transaction
    batch=QueryBatch.model_validate(batch) if isinstance(batch,dict) else batch
    with transaction(principal) as raw:
        c=(budget or ReadBudget()).wrap(raw)
        selections=[s for p in batch.plans for s in [p.selection,p.comparison] if s]
        catalog=catalogs(c,selections);results=[]
        for plan in batch.plans:
            from datetime import datetime
            from security.config import BUSINESS_TIMEZONE
            # Natural-language period queries share the fixed business cutoff;
            # future-dated fixture/import rows are never reported as occurred.
            # Structured API calls without dialogue context keep their contract.
            as_of=getattr(session,'conversation_business_date',None)
            if as_of is None and plan.time_mode!='period':as_of=datetime.now(BUSINESS_TIMEZONE).date()
            s,inc,exc=resolve(c,plan.selection,principal,session,catalog,plan.time_mode)
            current=aggregate(c,plan,s,inc,exc,as_of);comparison=None
            if plan.comparison:
                base,bi,be=resolve(c,plan.comparison,principal,session,catalog)
                previous=aggregate(c,plan,base,bi,be,as_of)
                comparable=s.source==base.source
                a=current['summary'];b=previous['summary'];metric=primary_metric(plan)
                base_amount=metric_value(b,metric);current_amount=metric_value(a,metric)
                delta=current_amount-base_amount if comparable and current_amount is not None and base_amount is not None else None
                ratio=(str((Decimal(delta)*100/Decimal(base_amount)).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)) if comparable and base_amount and b['recorded_count'] and a['recorded_count'] else None)
                comparison=dict(base=previous,metrics=compare_metrics(plan,a,b,comparable),change_cents=int(delta) if metric in {'sum','net'} and delta is not None and a['recorded_count'] and b['recorded_count'] else None,
                    change_percent=ratio,status='different_sources_no_merge' if not comparable else 'missing_records' if not a['recorded_count'] or not b['recorded_count'] else 'undefined_metric' if current_amount is None or base_amount is None else 'zero_base' if not base_amount else 'recorded_comparison')
            results.append(dict(plan=plan.model_dump(mode='json'),current=current,comparison=comparison))
        fingerprint=hashlib.sha256(json.dumps(results,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        data=dict(version='data-query-1',result_id='dq_'+fingerprint[:24],results=results,
            source_note='银行与上传账单分别展示，未做跨来源去重，不能相加作为完整消费。')
        verify_output(data)
        return data


def verified_execute(principal,batch,session=None,*,deadline=None):
    from billvision.service import transaction
    with transaction(principal):
        budget=ReadBudget(deadline)
        data=execute(principal,batch,session,budget=budget)
        canonical=execute(principal,batch,session,budget=budget)
        if data!=canonical:raise ValueError('canonical_query_mismatch')
        if session:
            if not hasattr(session,'conversation_metrics'):session.conversation_metrics={}
            session.conversation_metrics['sql_statements']=budget.statements
            session.conversation_metrics['query_sql_transport_statements']=budget.statements*2
            session.conversation_metrics['sql_count_scope']='query compiler including canonical; transport also includes one timeout-setting SELECT per statement; transaction/RLS setup separate'
        return data


def read_tool(batch,principal=None):
    try:return dict(success=True,data=verified_execute(principal,batch),message='已核实本次只读查询。')
    except VisionError as exc:return dict(success=False,error_code=exc.code,message=exc.message)


def relative_period(kind,today):
    if kind in {'last7','last30'}:
        return Period(start=today-timedelta(days=6 if kind=='last7' else 29),end_exclusive=today+timedelta(days=1))
    end=today.replace(day=1)
    if kind=='previous_month':return Period(start=(end-timedelta(days=1)).replace(day=1),end_exclusive=end)
    return Period(start=end,end_exclusive=(end.replace(day=28)+timedelta(days=4)).replace(day=1))
