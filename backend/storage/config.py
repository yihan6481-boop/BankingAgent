"""Environment only. No .env reader, secret defaults, or DSN in repr/errors."""
from dataclasses import dataclass, field
import os

from .errors import Code, StorageError

PG03_BILL_TOOLS=frozenset({'get_transactions_by_month','get_monthly_spending','get_category_spending',
    'query_financial_records','get_monthly_cash_flow_report','get_annual_cash_flow_report','get_expense_clues'})
PG04_WEALTH_TOOLS=frozenset({'get_risk_profile','get_wealth_products','get_wealth_holdings',
    'compare_wealth_products','explain_wealth_suitability','buy_wealth_product','redeem_wealth_product'})
PG05_EXISTING_TOOLS=frozenset({'report_card_lost','set_daily_limit','get_active_subscriptions',
    'get_monthly_subscription_cost','cancel_subscription'})
PG02_SUPPORTED_TOOLS=frozenset({'get_cards','get_transfer_recipients','transfer_money','query_uploaded_bills','query_data'}) | PG03_BILL_TOOLS | PG04_WEALTH_TOOLS | PG05_EXISTING_TOOLS


def pg02_enabled():
    """Internal process configuration only; importing this needs no PG driver."""
    return os.environ.get('BA_STORAGE_BACKEND','postgres').strip().lower() in {'pg02','postgres'}


def pg02_unsupported_request(text):
    """Bounded capability guard, independent of an omitted model plan step.

    This is not a general Chinese intent classifier. Explicit business requests
    outside the transfer slice fail closed. Quoted remarks and negative clauses
    do not become business actions.
    """
    import re
    probe=re.sub(r'[“"「].*?[”"」]','',text)
    probe=re.sub(r'备注(?:为|是|：|:).+?(?=[，。；;]|$)','',probe)
    clauses=re.split(r'[，。；;]|然后|并且|以及|但是|但|之后',probe)
    unsupported=re.compile(r'(?:购买|申购|买|赎回|查询|查看|查|取消|分析|统计).{0,10}'
                           r'(?:贷款|画像)|人脸|向量库')
    return any(unsupported.search(c) and not re.match(r'^\s*(?:本轮|这轮|也)?(?:不需要|不要|不用|不必|不)',c)
               for c in clauses)


def require_sqlite_application():
    """Retired backend; retained symbol makes old callers fail explicitly."""
    raise StorageError(Code.CONFIG)


@dataclass(frozen=True)
class PostgresConfig:
    dsn: str = field(repr=False)
    min_size: int = 1
    max_size: int = 4
    acquire_timeout: float = 3.0
    connect_timeout: int = 3
    statement_timeout_ms: int = 5000
    lock_timeout_ms: int = 2000

    def __post_init__(self):
        if (not isinstance(self.dsn, str) or not self.dsn.strip()
                or not 1 <= self.min_size <= self.max_size <= 32
                or not 0 < self.acquire_timeout <= 30
                or not 1 <= self.connect_timeout <= 30
                or not 1 <= self.lock_timeout_ms <= self.statement_timeout_ms <= 60000):
            raise StorageError(Code.CONFIG)

    @classmethod
    def from_env(cls, *, migration=False):
        name = "BA_PG_MIGRATION_DSN" if migration else "BA_PG_DSN"
        return cls(os.environ.get(name, ""))
