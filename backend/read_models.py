"""Internal read contracts in CNY/yuan; public API shapes are unchanged."""
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, TypeAdapter, model_validator
from analysis_models import Strict, Evidence, Identifier


def money(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError('money_must_be_numeric_yuan')
    number = Decimal(str(value))
    if not number.is_finite() or number * 100 != (number * 100).to_integral_value():
        raise ValueError('invalid_money_precision')
    return float(value)


Yuan = Annotated[float, BeforeValidator(money)]
Expense = Annotated[Yuan, Field(ge=0)]
Month = Annotated[str, Field(pattern=r'^[0-9]{4}-(0[1-9]|1[0-2])$')]


class Monthly(Strict):
    month: Month
    transaction_count: int = Field(strict=True, ge=0)
    total_spending: Expense
    card_id: Identifier | None = None
    card_specified: bool = False

    @model_validator(mode='after')
    def relationships(self):
        date.fromisoformat(self.month + '-01')
        if self.card_specified != (self.card_id is not None):
            raise ValueError('card_scope_mismatch')
        if self.transaction_count == 0 and self.total_spending != 0:
            raise ValueError('empty_total_mismatch')
        return self


class CategoryStat(Strict):
    amount: Expense
    percentage: Annotated[Yuan, Field(ge=0, le=100)]


class Categories(Strict):
    month: Month
    card_id: Identifier | None = None
    total_spending: Expense
    categories: dict[str, CategoryStat]

    @model_validator(mode='after')
    def relationships(self):
        date.fromisoformat(self.month + '-01')
        if sum((Decimal(str(x.amount)) for x in self.categories.values()), Decimal(0)) != Decimal(str(self.total_spending)):
            raise ValueError('category_total_mismatch')
        for key, item in self.categories.items():
            if not key.strip():
                raise ValueError('empty_category')
            expected = round(item.amount / self.total_spending * 100, 2) if self.total_spending else 0
            if abs(item.percentage - expected) > .011:
                raise ValueError('category_percentage_mismatch')
        return self


class Transaction(Evidence):
    amount: Expense
    event_at: str | None = None
    time_source: Literal['date_only','demo_rule_v2','transfer_receipt','wealth_receipt'] = 'date_only'
    transaction_type: Literal['purchase','refund','transfer','income','wealth_buy','wealth_redeem','unknown'] = 'unknown'
    status: Literal['success'] = 'success'

    @model_validator(mode='after')
    def calendar(self):
        date.fromisoformat(self.date)
        if (self.event_at is None) != (self.time_source=='date_only'):
            raise ValueError('event_time_provenance_mismatch')
        if self.event_at is not None:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            value=datetime.fromisoformat(self.event_at)
            if value.tzinfo is None or value.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()!=self.date:
                raise ValueError('event_time_business_day_mismatch')
        return self


class Card(Strict):
    card_id: Identifier
    bank_name: str
    masked_card_number: str = Field(pattern=r'^\*{4} \*{4} \*{4} [0-9]{4}$')
    balance: Yuan  # A balance can be negative; no blanket nonnegative rule.
    daily_limit: Expense
    status: Literal['正常', '已挂失', '冻结', '已冻结']
    overseas_enabled: Literal['开启', '关闭', '已开启', '已关闭']
    online_payment_enabled: Literal['开启', '关闭', '已开启', '已关闭']


from billvision.models import QueryResult as UploadedBillResult

READ_MODELS = {'query_uploaded_bills': UploadedBillResult, 'get_monthly_spending': Monthly, 'get_category_spending': Categories, 'get_transactions_by_month': list[Transaction], 'get_cards': list[Card]}


def verify_read(name, data, arguments=None):
    if name=='query_data':
        from data_query import verify_output,QueryBatch
        try:
            result=verify_output(data)
            return arguments is None or [r.plan for r in result.results]==QueryBatch.model_validate(arguments).plans
        except (ValueError,TypeError,ArithmeticError):return False
    model = READ_MODELS.get(name)
    if model is None:
        return True
    try:
        value = TypeAdapter(model).validate_python(data)
        if name=='query_uploaded_bills':
            from billvision.models import Query
            return arguments is None or value.scope==Query.model_validate(arguments)
            return arguments is None or [x.topic for x in value.items]==arguments.get('topics')
        if isinstance(value, list):
            id_field = 'transaction_id' if name == 'get_transactions_by_month' else 'card_id'
            ids = [getattr(x, id_field) for x in value]
            if len(ids) != len(set(ids)):
                return False
            if name == 'get_transactions_by_month' and (
                    len({x.date[:7] for x in value}) > 1 or
                    [(x.date, x.event_at or '\uffff', x.transaction_id) for x in value] != sorted((x.date, x.event_at or '\uffff', x.transaction_id) for x in value)):
                return False
        if arguments and 'month' in arguments:
            if isinstance(value, list):
                if any(not x.date.startswith(arguments['month'] + '-') or
                       arguments.get('card_id') is not None and x.card_id != arguments['card_id'] for x in value):
                    return False
            elif value.month != arguments['month'] or value.card_id != arguments.get('card_id'):
                return False
        return True
    except (ValueError, TypeError, ArithmeticError):
        return False


