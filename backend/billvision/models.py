from datetime import date as calendar_date, time as clock_time
from typing import Literal
import re
from pydantic import BaseModel, ConfigDict, Field, model_validator

class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)

def informational_issue(text):
    # Only positively stated excluded non-transaction headings/coverage notes.
    # Unknown, missing, conflicting and ambiguous fields always remain blocking.
    if re.search(r'未知|缺|不明|无法|冲突|疑|不确定|可能|错误|不符|不清|金额|日期|年份|币种|方向|状态|商户|订单|时间',text):return False
    if re.fullmatch(r'(?:页头(?:汇总|合计)|[“「]仅显示部分记录，不是完整月份[”」]为说明文字).{0,80}(?:未作为交易行|未作交易行|不作为交易行)[。.]?',text):return True
    clauses=[x for x in re.split(r'[，,；;。]',re.sub(r'[“”「」\s]','',text)) if x]
    note_clause=(r'(?:(?:图片|图中)?(?:标注为|标注|注明为|注明|为)?(?:虚构|模拟)(?:测试|演示)?(?:数据|账单|图片|样本)'
                 r'|(?:仅|只)(?:显示|展示|包含)(?:部分|本页)记录'
                 r'|(?:不是|非|并非|不代表)(?:完整月份|整月完整账单|完整月度账单|整月账单))')
    return bool(clauses) and all(re.fullmatch(note_clause,x) for x in clauses)

class Candidate(Strict):
    date: str | None = Field(None,max_length=10)
    time: str | None = Field(None,max_length=8)
    precision: Literal['day','minute','second','unknown']='unknown'
    merchant: str | None = Field(None,max_length=120)
    amount_cents: int | None = Field(None,ge=1,le=10**12)
    currency: str | None = Field(None,pattern=r'^[A-Z]{3}$')
    direction: Literal['income','expense','unknown']='unknown'
    kind: Literal['purchase','refund','transfer','other','unknown']='unknown'
    status: Literal['success','pending','failed','closed','unknown']='unknown'
    payment_method: str | None = Field(None,max_length=120)
    order_number: str | None = Field(None,max_length=128)
    event_number: str | None = Field(None,max_length=128)
    evidence: str = Field('',max_length=1500)
    issues: list[str] = Field(default_factory=list,max_length=20)
    confidence: float | None = Field(None,ge=0,le=1)

    @model_validator(mode='after')
    def calendar(self):
        if self.date:
            if len(self.date)!=10:raise ValueError('explicit_year_required')
            calendar_date.fromisoformat(self.date)
        if self.time:clock_time.fromisoformat(self.time)
        if self.time and self.precision not in {'minute','second'}:raise ValueError('time_precision_mismatch')
        if self.precision=='day' and not self.date:raise ValueError('date_precision_mismatch')
        if any(len(x)>240 for x in self.issues):raise ValueError('issue_too_long')
        return self

    def unresolved(self):
        problems=[x for x in self.issues if not informational_issue(x)]
        for field in ('date','merchant','amount_cents','currency'):
            if not getattr(self,field):problems.append('missing_'+field)
        for field in ('direction','kind','status','precision'):
            if getattr(self,field)=='unknown':problems.append('unknown_'+field)
        if self.currency in {'UNK','XXX','XTS','RMB'}:problems.append('unknown_currency')
        if self.precision in {'minute','second'} and not self.time:problems.append('missing_time')
        if self.kind=='purchase' and self.direction!='expense':problems.append('purchase_direction_conflict')
        if self.kind=='refund' and self.direction!='income':problems.append('refund_direction_conflict')
        return sorted(set(problems))

class Recognition(Strict):
    platform: Literal['wechat','alipay','unknown']='unknown'
    rows: list[Candidate] = Field(default_factory=list,max_length=100)
    issues: list[str] = Field(default_factory=list,max_length=20)
    supported: bool=True

class ReviewedRow(Strict):
    row_id: str = Field(pattern=r'^r[0-9]{1,3}$')
    value: Candidate
    included: bool=True
    reviewed: bool=False
    duplicate_resolution: Literal['unresolved','keep_distinct','exclude']='unresolved'

class Revision(Strict):
    revision: int = Field(ge=1)

class Edit(Revision):
    rows: list[ReviewedRow] = Field(max_length=100)
    platform: Literal['wechat','alipay','invoice'] | None = None

CONFIRM_TEXT = '确认导入已核对账单'

class Confirm(Revision):
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    row_ids: list[str] = Field(min_length=1,max_length=100)
    text: Literal['确认导入已核对账单']

class Query(Strict):
    document_id: str | None = Field(None,pattern=r'^bv_[0-9a-f]{32}$')
    start: str | None = None
    end_exclusive: str | None = None
    merchant: str | None = Field(None,min_length=1,max_length=120)

    @model_validator(mode='after')
    def range(self):
        if (self.start is None)!=(self.end_exclusive is None):raise ValueError('both_dates_required')
        if self.start:
            calendar_date.fromisoformat(self.start);calendar_date.fromisoformat(self.end_exclusive)
            if self.start>=self.end_exclusive:raise ValueError('invalid_range')
        return self

class Record(Strict):
    document_id: str = Field(pattern=r'^bv_[0-9a-f]{32}$')
    row_id: str = Field(pattern=r'^r[0-9]{1,3}$')
    revision: int = Field(ge=1)
    date: str
    merchant: str
    amount_cents: int = Field(ge=1)
    currency: str
    direction: Literal['income','expense']
    kind: Literal['purchase','refund','transfer','other']
    status: Literal['success','pending','failed','closed']

class Totals(Strict):
    purchase_expense_cents: int = Field(ge=0)
    other_expense_cents: int = Field(ge=0)
    refund_cents: int = Field(ge=0)
    income_cents: int = Field(ge=0)
    excluded_count: int = Field(ge=0)

class QueryResult(Strict):
    source: Literal['confirmed_uploaded_images']='confirmed_uploaded_images'
    scope: Query
    coverage: Literal['上传图片中已确认记录；不是整月完整账单；未与银行流水合并']='上传图片中已确认记录；不是整月完整账单；未与银行流水合并'
    currency: Literal['CNY']='CNY'
    records: list[Record] = Field(max_length=1000)
    totals: Totals

    @model_validator(mode='after')
    def exact(self):
        ids=[(r.document_id,r.row_id) for r in self.records]
        if len(ids)!=len(set(ids)):raise ValueError('duplicate_record')
        expected=dict(purchase_expense_cents=0,other_expense_cents=0,refund_cents=0,income_cents=0,excluded_count=0)
        for r in self.records:
            calendar_date.fromisoformat(r.date)
            if self.scope.document_id and r.document_id!=self.scope.document_id:raise ValueError('wrong_document')
            if self.scope.merchant and r.merchant!=self.scope.merchant:raise ValueError('wrong_merchant')
            if self.scope.start and not self.scope.start<=r.date<self.scope.end_exclusive:raise ValueError('wrong_range')
            if r.currency!='CNY' or r.status!='success':expected['excluded_count']+=1;continue
            key=('refund_cents' if r.kind=='refund' else 'purchase_expense_cents' if r.kind=='purchase' and r.direction=='expense' else 'other_expense_cents' if r.direction=='expense' else 'income_cents')
            expected[key]+=r.amount_cents
        if self.totals.model_dump()!=expected:raise ValueError('wrong_totals')
        return self
