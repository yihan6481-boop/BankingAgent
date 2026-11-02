"""Analysis protocol 1.0. Independent of the frozen UI business kinds."""
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, strict=True,
        json_schema_serialization_defaults_required=True)


Identifier = Annotated[int, Field(strict=True, gt=0)]
Money = Annotated[float, Field(allow_inf_nan=False)]
Risk = Literal['R1', 'R2', 'R3', 'R4', 'R5']


class MonthReportArgs(Strict):
    month: str = Field(pattern=r'^[0-9]{4}-(0[1-9]|1[0-2])$')
    card_id: Identifier | None = None
    top_n: int = Field(default=10, strict=True, ge=1, le=20)

    @field_validator('month')
    @classmethod
    def calendar(cls, value):
        from datetime import date
        date.fromisoformat(value + '-01')
        if value[:4] == '9999':
            raise ValueError('unsupported upper calendar boundary')
        return value


class YearReportArgs(Strict):
    year: int = Field(strict=True, ge=1, le=9998)
    card_id: Identifier | None = None
    top_n: int = Field(default=10, strict=True, ge=1, le=20)


class AnomalyArgs(MonthReportArgs):
    pass


class CompareArgs(Strict):
    product_ids: list[Identifier] = Field(min_length=2, max_length=5)

    @field_validator('product_ids')
    @classmethod
    def unique(cls, values):
        if len(values) != len(set(values)):
            raise ValueError('duplicate_product_selection')
        return values


class SuitabilityArgs(Strict):
    product_id: Identifier
    amount: Decimal | None = None
    card_id: Identifier | None = None

    @field_validator('amount', mode='before')
    @classmethod
    def amount_precision(cls, value):
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError('invalid_amount')
        try:
            number = Decimal(str(value))
        except Exception as exc:
            raise ValueError('invalid_amount') from exc
        if (not number.is_finite() or number < Decimal('.01') or
            number > Decimal('92233720368547758.07') or
            number * 100 != (number * 100).to_integral_value()):
            raise ValueError('invalid_amount')
        return number


class Range(Strict):
    start: str
    end_exclusive: str
    timezone: Literal['Asia/Shanghai'] = 'Asia/Shanghai'
    card_id: Identifier | None


class Evidence(Strict):
    transaction_id: Identifier
    card_id: Identifier
    card_last_four: str = Field(pattern=r'^[0-9]{4}$')
    date: str
    merchant: str = Field(max_length=100)
    category: str = Field(max_length=100)
    amount: Money
    direction: Literal['income', 'expense']


class Coverage(Strict):
    source: Literal['local_simulated_transactions'] = 'local_simulated_transactions'
    completeness: Literal['recorded_only'] = 'recorded_only'
    first_recorded: str | None
    last_recorded: str | None
    recorded_months: list[str] = Field(max_length=12)
    note: str
    evidence: list[Evidence] = Field(max_length=100)
    evidence_truncated: bool
    ledger_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class Totals(Strict):
    income: Money
    expense: Money
    net_cash_flow: Money
    transaction_count: int = Field(strict=True, ge=0)
    income_count: int = Field(strict=True, ge=0)
    expense_count: int = Field(strict=True, ge=0)

    @model_validator(mode='after')
    def arithmetic(cls, self):
        if Decimal(str(self.income)) - Decimal(str(self.expense)) != Decimal(str(self.net_cash_flow)):
            raise ValueError('cash_flow_mismatch')
        if self.transaction_count != self.income_count + self.expense_count:
            raise ValueError('count_mismatch')
        return self


class Category(Strict):
    name: str
    direction: Literal['income', 'expense']
    amount: Money
    percentage: Money | None
    percentage_reason: Literal['zero_denominator'] | None


class Merchant(Strict):
    name: str
    expense: Money
    transaction_count: int = Field(strict=True, ge=1)


class Change(Strict):
    metric: Literal['income', 'expense', 'net_cash_flow', 'transaction_count']
    base: Money
    current: Money
    change: Money
    percentage: Money | None
    percentage_reason: Literal['zero_denominator'] | None


class Comparison(Strict):
    range: Range | None
    totals: Totals
    coverage: Coverage
    changes: list[Change] = Field(min_length=4, max_length=4)


class MonthDistribution(Strict):
    month: str
    totals: Totals


class Report(Strict):
    kind: Literal['monthly_cash_flow_report', 'annual_cash_flow_report']
    range: Range
    currency: Literal['CNY'] = 'CNY'
    totals: Totals
    categories: list[Category]
    merchants: list[Merchant] = Field(max_length=20)
    coverage: Coverage
    comparison: Comparison
    months: list[MonthDistribution] = Field(max_length=12)
    explanation: str

    @model_validator(mode='after')
    def consistency(cls, self):
        for direction in ('income', 'expense'):
            items = [x for x in self.categories if x.direction == direction]
            total = getattr(self.totals, direction)
            if sum((Decimal(str(x.amount)) for x in items), Decimal(0)) != Decimal(str(total)):
                raise ValueError('category_total_mismatch')
        if self.kind == 'annual_cash_flow_report':
            if len(self.months) != 12 or len({m.month for m in self.months}) != 12:
                raise ValueError('month_distribution_mismatch')
            for field in ('income', 'expense', 'net_cash_flow', 'transaction_count'):
                if sum((Decimal(str(getattr(m.totals, field))) for m in self.months), Decimal(0)) != Decimal(str(getattr(self.totals, field))):
                    raise ValueError('annual_total_mismatch')
        elif self.months:
            raise ValueError('monthly_distribution_not_allowed')
        for change in self.comparison.changes:
            if Decimal(str(change.current)) - Decimal(str(change.base)) != Decimal(str(change.change)):
                raise ValueError('comparison_mismatch')
            if change.base == 0 and (change.percentage is not None or change.percentage_reason != 'zero_denominator'):
                raise ValueError('zero_denominator_mismatch')
        return self


class Trigger(Strict):
    rule_id: Literal['large_expense', 'same_day_duplicate_candidate']
    rule_version: Literal['demo-1.0']
    actual: int = Field(strict=True, ge=0)
    threshold: int = Field(strict=True, ge=1)
    unit: Literal['cents', 'count']
    reason: str
    transaction_ids: list[Identifier]


class Clue(Strict):
    transaction: Evidence
    triggers: list[Trigger] = Field(min_length=1, max_length=2)


class Applicability(Strict):
    rule_id: Literal['large_expense', 'same_day_duplicate_candidate']
    applicable_count: int = Field(strict=True, ge=0)
    inapplicable_count: int = Field(strict=True, ge=0)
    reason: str | None


class Anomalies(Strict):
    kind: Literal['expense_clues'] = 'expense_clues'
    range: Range
    currency: Literal['CNY'] = 'CNY'
    rule_version: Literal['demo-1.0'] = 'demo-1.0'
    matched_transaction_count: int = Field(strict=True, ge=0)
    clues: list[Clue] = Field(max_length=20)
    truncated: bool
    applicability: list[Applicability] = Field(min_length=2, max_length=2)
    coverage: Coverage
    explanation: str


class ConditionFact(Strict):
    reason_code: Literal['risk_match', 'risk_mismatch', 'risk_profile_missing', 'product_active',
        'product_inactive', 'minimum_met', 'below_minimum', 'amount_not_provided',
        'card_not_provided', 'card_unavailable', 'balance_sufficient', 'insufficient_balance',
        'payment_not_evaluated', 'card_available', 'purchase_preview_required']
    state: Literal['pass', 'fail', 'unknown']
    user_risk_level: Risk | None = None
    product_risk_level: Risk | None = None
    amount: Money | None = None
    minimum: Money | None = None
    card_status: Literal['normal', 'lost', 'frozen'] | None = None
    balance: Money | None = None
    explanation: str


class Suitability(Strict):
    kind: Literal['wealth_suitability'] = 'wealth_suitability'
    product_id: Identifier
    user_risk_level: Risk | None
    product_risk_level: Risk
    risk_matches: bool | None
    product_active: bool
    risk_fact: ConditionFact
    purchase_conditions: list[ConditionFact]
    purchase_assessment: Literal['conditions_met_at_query', 'conditions_not_met', 'not_fully_evaluated']
    authorization: Literal['not_granted'] = 'not_granted'


class Product(Strict):
    product_id: Identifier
    name: str
    risk_level: Risk
    status: Literal['active', 'inactive']
    min_purchase: Money
    reference_annual_rate: Money
    # Catalog/storage allow zero as unspecified. It grants no liquidity or
    # redemption claim; these remain explicitly not_provided below.
    duration_days: int = Field(strict=True, ge=0)
    liquidity: Literal['not_provided'] = 'not_provided'
    early_redemption: Literal['not_provided'] = 'not_provided'
    fees: Literal['not_provided'] = 'not_provided'
    notice: str
    suitability: Suitability


class ProductComparison(Strict):
    kind: Literal['wealth_product_comparison'] = 'wealth_product_comparison'
    products: list[Product] = Field(min_length=2, max_length=5)
    source: Literal['local_simulated_product_catalog'] = 'local_simulated_product_catalog'
    explanation: str


class QueryArgs(Strict):
    start: str
    end_exclusive: str
    merchant: str | None = Field(default=None, min_length=1, max_length=100)
    card_id: Identifier | None = None
    metric: Literal['purchase_expense', 'cash_outflow']
    compare_previous: bool = False

    @model_validator(mode='after')
    def period(self):
        from datetime import date
        for value in (self.start, self.end_exclusive):
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError('noncanonical_date')
        if self.start >= self.end_exclusive:
            raise ValueError('invalid_range')
        if self.compare_previous:
            d = date.fromisoformat(self.start)
            end = f'{d.year + (d.month == 12):04d}-{d.month % 12 + 1:02d}-01'
            if d.day != 1 or self.end_exclusive != end or self.start == '0001-01-01':
                raise ValueError('comparison_requires_adjacent_months')
        return self


Cents = Annotated[int, Field(strict=True, ge=0)]
TransactionType = Literal['purchase','transfer','wealth_buy','wealth_redeem','refund','income','unknown']


class QueryEvidence(Strict):
    transaction_id: Identifier
    card_id: Identifier
    date: str
    merchant: str = Field(max_length=100)
    transaction_type: TransactionType
    classification_source: str = Field(max_length=100)
    amount_cents: Cents
    direction: Literal['income','expense']
    refund_of_transaction_id: Identifier | None


class QueryPeriod(Strict):
    range: Range
    amount_cents: Cents
    matched_count: Cents
    recorded_count: Cents
    refund_cents: Cents
    refund_count: Cents
    unlinked_refund_count: Cents
    unclassified_cents: Cents
    unclassified_count: Cents
    classification: Literal['known_recorded','partially_classified','no_records']
    evidence: list[QueryEvidence] = Field(max_length=100)
    evidence_truncated: bool
    ledger_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def consistency(self):
        from datetime import date
        for value in (self.range.start,self.range.end_exclusive):
            if date.fromisoformat(value).isoformat()!=value:
                raise ValueError('noncanonical_query_date')
        if self.range.start>=self.range.end_exclusive:
            raise ValueError('invalid_query_range')
        if (self.matched_count > self.recorded_count or self.refund_count > self.recorded_count or
                self.unclassified_count > self.recorded_count or self.unlinked_refund_count > self.refund_count or
                len(self.evidence) != min(self.recorded_count,100) or
                self.evidence_truncated != (self.recorded_count > 100)):
            raise ValueError('query_count_mismatch')
        expected = 'no_records' if not self.recorded_count else 'partially_classified' if self.unclassified_count else 'known_recorded'
        if self.classification != expected:
            raise ValueError('classification_mismatch')
        ids = [r.transaction_id for r in self.evidence]
        if len(set(ids)) != len(ids):
            raise ValueError('duplicate_evidence')
        for row in self.evidence:
            if date.fromisoformat(row.date).isoformat()!=row.date:
                raise ValueError('noncanonical_evidence_date')
            if not self.range.start <= row.date < self.range.end_exclusive or self.range.card_id not in (None,row.card_id):
                raise ValueError('evidence_scope_mismatch')
            if (row.transaction_type in ('purchase','transfer','wealth_buy') and row.direction!='expense' or
                row.transaction_type in ('refund','income','wealth_redeem') and row.direction!='income'):
                raise ValueError('evidence_direction_mismatch')
        return self


class QueryComparison(Strict):
    base: QueryPeriod
    change_cents: int = Field(strict=True)
    percentage: Money | None
    percentage_reason: Literal['zero_denominator'] | None


class QueryResult(Strict):
    kind: Literal['financial_records_query'] = 'financial_records_query'
    metric: Literal['purchase_expense','cash_outflow']
    merchant: str | None
    merchant_key: str | None
    aliases: list[str] = Field(max_length=100)
    currency: Literal['CNY'] = 'CNY'
    completeness: Literal['recorded_only'] = 'recorded_only'
    actual_coverage: Literal['unknown'] = 'unknown'
    current: QueryPeriod
    comparison: QueryComparison | None
    net_consumption: Literal['not_supported'] = 'not_supported'
    explanation: str

    @model_validator(mode='after')
    def consistency(self):
        from datetime import date
        from decimal import ROUND_HALF_UP
        if self.aliases!=sorted(set(self.aliases)) or (self.merchant is not None and self.merchant not in self.aliases):
            raise ValueError('invalid_query_aliases')
        for period in [self.current] + ([self.comparison.base] if self.comparison else []):
            if not period.evidence_truncated:
                rows = period.evidence
                selected = [r for r in rows if r.direction=='expense' and
                    (self.metric=='cash_outflow' or r.transaction_type=='purchase')]
                refunds = [r for r in rows if r.transaction_type=='refund']
                unknown = [r for r in rows if r.transaction_type=='unknown']
                if (period.amount_cents != sum(r.amount_cents for r in selected) or period.matched_count != len(selected) or
                    period.refund_cents != sum(r.amount_cents for r in refunds) or period.refund_count != len(refunds) or
                    period.unlinked_refund_count != sum(r.refund_of_transaction_id is None for r in refunds) or
                    period.unclassified_cents != sum(r.amount_cents for r in unknown) or period.unclassified_count != len(unknown)):
                    raise ValueError('query_totals_mismatch')
            if self.merchant is not None and any(r.merchant not in self.aliases for r in period.evidence):
                raise ValueError('merchant_scope_mismatch')
        if self.comparison:
            c = self.comparison
            QueryArgs(start=self.current.range.start,end_exclusive=self.current.range.end_exclusive,
                metric=self.metric,compare_previous=True)
            d = date.fromisoformat(self.current.range.start)
            prior = f'{d.year-(d.month==1):04d}-{12 if d.month==1 else d.month-1:02d}-01'
            if (c.base.range.start!=prior or c.base.range.end_exclusive!=self.current.range.start or
                    c.base.range.card_id!=self.current.range.card_id or
                    c.change_cents!=self.current.amount_cents-c.base.amount_cents):
                raise ValueError('comparison_scope_mismatch')
            pct = float((Decimal(c.change_cents)*100/Decimal(c.base.amount_cents)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)) if c.base.amount_cents else None
            if c.percentage!=pct or c.percentage_reason!=('zero_denominator' if pct is None else None):
                raise ValueError('comparison_percentage_mismatch')
        return self


AnalysisResult = Annotated[Report | Anomalies | ProductComparison | Suitability | QueryResult, Field(discriminator='kind')]
