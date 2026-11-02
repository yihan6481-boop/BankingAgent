"""Unified Risk Engine / Permission Policy for V0.7.0.

The security level is always computed by the backend from trusted facts.  The
LLM, the frontend and the user's natural language can never override it, and
unknown actions, missing security facts or policy errors fail closed.

Business refusals and authentication requirements are expressed separately:
satisfying strong authentication never bypasses insufficient balance, an
unavailable card, an untrusted recipient or an unsuitable wealth product.

Default policy (see ``docs/V0.7_SECURITY_DESIGN.md``):

======================  =====================================================
Operation               Level and requirement
======================  =====================================================
普通查询                green, automatic
本日累计+本次 ≤ 1000 元 yellow, preview + confirmation
本日累计+本次 > 1000 元 red, preview + confirmation + simulated OTP
取消订阅                yellow
卡片挂失                red
理财申购                red
理财赎回                yellow（恒定，不升级）
每日限额调整            yellow；上调超过配置阈值 => red
订阅查询/费用统计       赛题例外：默认 yellow（策略可配置）
======================  =====================================================
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta

from database import get_connection

from .config import (
    BUSINESS_TIMEZONE,
    DEFAULT_SECURITY_CONFIG,
    SecurityConfig,
)
from .ownership import owned_card_ids, resource_refs_for_decision
from .schemas import (
    BusinessStateSnapshot,
    PermissionLevel,
    PolicyDecision,
    Principal,
    compute_action_digest,
)

LEVEL_RANK: dict[str, int] = {"green": 0, "yellow": 1, "red": 2}


class PolicyError(Exception):
    """Fail-closed policy failure with a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def business_date(now: datetime) -> str:
    """Banking business day in Asia/Shanghai.

    Never slice a UTC timestamp: 00:00-07:59 Shanghai time belongs to the
    previous UTC date, which would silently under-count the daily total.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise PolicyError("invalid_time", "策略时钟必须包含时区")
    return now.astimezone(BUSINESS_TIMEZONE).date().isoformat()


class ActionPolicy:
    """Static policy fragment for one action."""

    __slots__ = ("action", "base_level", "requires_confirmation",
                 "requires_strong_auth", "inspect")

    def __init__(
        self,
        action: str,
        base_level: PermissionLevel,
        requires_confirmation: bool,
        requires_strong_auth: bool,
        inspect: bool = False,
    ):
        self.action = action
        self.base_level = base_level
        self.requires_confirmation = requires_confirmation
        self.requires_strong_auth = requires_strong_auth
        self.inspect = inspect


ACTION_POLICIES: dict[str, ActionPolicy] = {
    'query_data': ActionPolicy('query_data', 'green', False, False),
    'query_uploaded_bills': ActionPolicy('query_uploaded_bills', 'green', False, False),
    "query_financial_records": ActionPolicy("query_financial_records", "green", False, False),
    # V0.8.3 explicit ordinary analysis actions; no write policy changes.
    "get_monthly_cash_flow_report": ActionPolicy("get_monthly_cash_flow_report", "green", False, False),
    "get_annual_cash_flow_report": ActionPolicy("get_annual_cash_flow_report", "green", False, False),
    "get_expense_clues": ActionPolicy("get_expense_clues", "green", False, False),
    "compare_wealth_products": ActionPolicy("compare_wealth_products", "green", False, False),
    "explain_wealth_suitability": ActionPolicy("explain_wealth_suitability", "green", False, False),
    # ---- green ordinary queries ----
    "query": ActionPolicy("query", "green", False, False),
    "get_cards": ActionPolicy("get_cards", "green", False, False),
    "get_monthly_spending": ActionPolicy(
        "get_monthly_spending", "green", False, False
    ),
    "get_category_spending": ActionPolicy(
        "get_category_spending", "green", False, False
    ),
    "get_transactions_by_month": ActionPolicy(
        "get_transactions_by_month", "green", False, False
    ),
    "get_transfer_recipients": ActionPolicy(
        "get_transfer_recipients", "green", False, False
    ),
    "get_risk_profile": ActionPolicy("get_risk_profile", "green", False, False),
    "get_wealth_products": ActionPolicy(
        "get_wealth_products", "green", False, False
    ),
    "get_wealth_holdings": ActionPolicy(
        "get_wealth_holdings", "green", False, False
    ),
    # ---- 赛题例外：只读不等于 green ----
    "get_active_subscriptions": ActionPolicy(
        "get_active_subscriptions", "yellow", True, False
    ),
    "get_monthly_subscription_cost": ActionPolicy(
        "get_monthly_subscription_cost", "yellow", True, False
    ),
    # ---- writes ----
    "cancel_subscription": ActionPolicy(
        "cancel_subscription", "yellow", True, False
    ),
    "report_card_lost": ActionPolicy(
        "report_card_lost", "red", True, True
    ),
    "set_daily_limit": ActionPolicy(
        "set_daily_limit", "yellow", True, False, inspect=True
    ),
    "transfer_money": ActionPolicy(
        "transfer_money", "yellow", True, False, inspect=True
    ),
    "buy_wealth_product": ActionPolicy(
        "buy_wealth_product", "red", True, True
    ),
    "redeem_wealth_product": ActionPolicy(
        "redeem_wealth_product", "yellow", True, False, inspect=True
    ),
}

# 只读动作集合：用于 /read 与 V2 的确定性查询通路
READ_ACTIONS = {
    name for name, policy in ACTION_POLICIES.items() if policy.base_level == "green"
} | {"get_active_subscriptions", "get_monthly_subscription_cost"}

WRITE_ACTIONS = {
    "cancel_subscription",
    "report_card_lost",
    "set_daily_limit",
    "transfer_money",
    "buy_wealth_product",
    "redeem_wealth_product",
}

# 需要 LLM/Workflow 规划的动作（其余可由确定性通路直接执行）
ALWAYS_STRONG_AUTH_ACTIONS = {"report_card_lost", "buy_wealth_product"}


def _cents(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


class RiskEngine:
    """Computes :class:`PolicyDecision` objects from trusted backend facts."""

    def __init__(self, config: SecurityConfig | None = None, now_provider=None):
        self.config = config or DEFAULT_SECURITY_CONFIG
        self.now_provider = now_provider

    # ------------------------------------------------------------------ time
    def _now(self) -> datetime:
        from .identity import _utcnow

        value = (self.now_provider or _utcnow)()
        if not isinstance(value, datetime):
            raise PolicyError("invalid_time", "策略时钟必须返回 datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise PolicyError("invalid_time", "策略时钟必须包含时区")
        return value.replace(microsecond=0)

    # ---------------------------------------------------------- fact lookup
    def transfer_daily_total_cents(self, connection, user_id: int, day: str) -> int:
        """已完成转账合计：按用户所有归属卡片累计，不按单卡分别计算。"""
        row = connection.execute(
            """
            SELECT COALESCE(SUM(t.amount_cents), 0) AS total
            FROM transfer_orders AS t
            JOIN cards AS c ON c.card_id = t.from_card_id
            WHERE c.owner_id = ?
              AND t.status = 'completed'
              AND t.business_date = ?
            """,
            (user_id, day),
        ).fetchone()
        return _cents(row["total"]) or 0

    def card_daily_total_cents(self, connection, card_id: int, day: str) -> int:
        """单卡当日已转账合计（银行卡原有每日限额仍独立检查）。"""
        row = connection.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS total
            FROM transfer_orders
            WHERE from_card_id = ?
              AND status = 'completed'
              AND business_date = ?
            """,
            (card_id, day),
        ).fetchone()
        return _cents(row["total"]) or 0

    def snapshot(self, action: str, principal: Principal, arguments: dict) -> BusinessStateSnapshot:
        """Read the trusted business facts required to decide ``action``."""
        if principal is None:
            raise PolicyError("principal_required", "缺少服务器端身份")
        policy = ACTION_POLICIES.get(action)
        if policy is None:
            raise PolicyError("unknown_action", "未知操作，拒绝执行")
        arguments = arguments or {}
        snapshot = BusinessStateSnapshot()
        now = self._now()
        day = business_date(now)

        connection = None
        try:
            connection = get_connection()
            if action == "transfer_money":
                amount_cents = _cents(arguments.get("amount_cents"))
                if amount_cents is None:
                    amount_cents = self._amount_cents_from_decimal(
                        arguments.get("amount")
                    )
                snapshot.amount_cents = amount_cents
                card_id = arguments.get("from_card_id")
                if isinstance(card_id, int) and not isinstance(card_id, bool):
                    card = connection.execute(
                        "SELECT owner_id, status, daily_limit_cents FROM cards "
                        "WHERE card_id = ?",
                        (card_id,),
                    ).fetchone()
                    if card is not None and int(card["owner_id"]) == int(
                        principal.user_id
                    ):
                        snapshot.card_status = card["status"]
                        snapshot.card_daily_limit_cents = card["daily_limit_cents"]
                recipient_id = arguments.get("recipient_id")
                if isinstance(recipient_id, int) and not isinstance(recipient_id, bool):
                    recipient = connection.execute(
                        "SELECT owner_id, status, trusted FROM recipients "
                        "WHERE recipient_id = ?",
                        (recipient_id,),
                    ).fetchone()
                    if recipient is not None and int(recipient["owner_id"]) == int(
                        principal.user_id
                    ):
                        snapshot.recipient_status = recipient["status"]
                        snapshot.recipient_trusted = bool(recipient["trusted"])
                if principal.kind != "legacy_demo":
                    snapshot.transfer_daily_total_cents = (
                        self.transfer_daily_total_cents(
                            connection, int(principal.user_id), day
                        )
                    )
                else:
                    snapshot.transfer_daily_total_cents = 0
            elif action == "set_daily_limit":
                amount_cents = _cents(arguments.get("new_limit_cents"))
                if amount_cents is None:
                    amount_cents = self._amount_cents_from_decimal(
                        arguments.get("daily_limit")
                    )
                snapshot.amount_cents = amount_cents
                card_id = arguments.get("card_id")
                if isinstance(card_id, int) and not isinstance(card_id, bool):
                    card = connection.execute(
                        "SELECT owner_id, status, daily_limit_cents FROM cards "
                        "WHERE card_id = ?",
                        (card_id,),
                    ).fetchone()
                    if card is not None and int(card["owner_id"]) == int(
                        principal.user_id
                    ):
                        snapshot.card_status = card["status"]
                        snapshot.card_daily_limit_cents = card["daily_limit_cents"]
            elif action == "buy_wealth_product":
                amount_cents = _cents(arguments.get("amount_cents"))
                if amount_cents is None:
                    amount_cents = self._amount_cents_from_decimal(
                        arguments.get("amount")
                    )
                snapshot.amount_cents = amount_cents
                product_id = arguments.get("product_id")
                if isinstance(product_id, int) and not isinstance(product_id, bool):
                    product = connection.execute(
                        "SELECT status, risk_level FROM wealth_products "
                        "WHERE product_id = ?",
                        (product_id,),
                    ).fetchone()
                    if product is not None:
                        snapshot.product_status = product["status"]
                        snapshot.product_risk_level = product["risk_level"]
                snapshot.user_risk_level = self._user_risk_level(
                    connection, principal
                )
            elif action == "redeem_wealth_product":
                amount_cents = _cents(arguments.get("amount_cents"))
                if amount_cents is None:
                    amount_cents = self._amount_cents_from_decimal(
                        arguments.get("amount")
                    )
                snapshot.amount_cents = amount_cents
                holding_id = arguments.get("holding_id")
                if isinstance(holding_id, int) and not isinstance(holding_id, bool):
                    holding = connection.execute(
                        "SELECT user_id, status, principal_cents FROM wealth_holdings "
                        "WHERE holding_id = ?",
                        (holding_id,),
                    ).fetchone()
                    if holding is not None and int(holding["user_id"]) == int(
                        principal.user_id
                    ):
                        snapshot.holding_status = holding["status"]
            elif action == "cancel_subscription":
                subscription_id = arguments.get("subscription_id")
                if isinstance(subscription_id, int) and not isinstance(
                    subscription_id, bool
                ):
                    row = connection.execute(
                        "SELECT owner_id, status FROM subscriptions "
                        "WHERE subscription_id = ?",
                        (subscription_id,),
                    ).fetchone()
                    if row is not None and int(row["owner_id"]) == int(
                        principal.user_id
                    ):
                        snapshot.product_status = row["status"]
            elif action == "report_card_lost":
                card_id = arguments.get("card_id")
                if isinstance(card_id, int) and not isinstance(card_id, bool):
                    card = connection.execute(
                        "SELECT owner_id, status FROM cards WHERE card_id = ?",
                        (card_id,),
                    ).fetchone()
                    if card is not None and int(card["owner_id"]) == int(
                        principal.user_id
                    ):
                        snapshot.card_status = card["status"]
            return snapshot
        except sqlite3.Error as error:
            raise PolicyError(
                "security_fact_unavailable", "无法读取安全事实，拒绝执行"
            ) from error
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _amount_cents_from_decimal(value):
        if value is None or isinstance(value, bool):
            return None
        try:
            from decimal import Decimal

            decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
            cents = decimal_value * Decimal("100")
            if not cents.is_finite() or cents != cents.to_integral_value():
                return None
            return int(cents)
        except Exception:
            return None

    @staticmethod
    def _user_risk_level(connection, principal: Principal):
        row = connection.execute(
            "SELECT risk_level FROM user_risk_profiles WHERE user_id = ?",
            (int(principal.user_id),),
        ).fetchone()
        return row["risk_level"] if row is not None else None

    # ------------------------------------------------------------ decision
    def evaluate(
        self,
        action: str,
        principal: Principal,
        arguments: dict | None = None,
        *,
        snapshot: BusinessStateSnapshot | None = None,
        declared_risk_level: str | None = None,
    ) -> PolicyDecision:
        arguments = dict(arguments or {})
        if principal is None:
            raise PolicyError("principal_required", "缺少服务器端身份")
        policy = ACTION_POLICIES.get(action)
        if policy is None:
            raise PolicyError("unknown_action", "未知操作，拒绝执行")

        if snapshot is None:
            snapshot = self.snapshot(action, principal, arguments)

        reasons: list[str] = []
        level: PermissionLevel = policy.base_level
        requires_confirmation = policy.requires_confirmation
        requires_strong_auth = policy.requires_strong_auth
        threshold_cents = None
        projected_total_cents = None

        if action == "transfer_money":
            level, requires_confirmation, requires_strong_auth, reasons, threshold_cents, projected_total_cents = (
                self._transfer_policy(snapshot, reasons)
            )
        elif action == "set_daily_limit":
            level, requires_confirmation, requires_strong_auth, reasons = (
                self._daily_limit_policy(snapshot, reasons)
            )
        elif action == "redeem_wealth_product":
            # 当前冻结策略恒为 yellow；这里只验证持仓安全事实，不做 red 升级。
            reasons.append("redeem_default_yellow")
            if snapshot.amount_cents is not None and snapshot.holding_status is None:
                raise PolicyError(
                    "security_fact_unavailable", "缺少持仓安全事实，拒绝执行"
                )
            requires_confirmation = True
        elif action in {
            "get_active_subscriptions",
            "get_monthly_subscription_cost",
        }:
            if not self.config.subscription_query_requires_confirmation:
                level = "green"
                requires_confirmation = False
                reasons.append("subscription_query_policy_downgraded_by_config")
            else:
                level = "yellow"
                requires_confirmation = True
                reasons.append("subscription_query_requires_authorization")

        # 旧业务风险字段只作为说明性输入，绝不参与安全降级判断。
        if declared_risk_level in {"low", "medium", "high"}:
            reasons.append(f"legacy_business_risk_level_{declared_risk_level}")

        if action in ALWAYS_STRONG_AUTH_ACTIONS:
            level = "red"
            requires_confirmation = True
            requires_strong_auth = True
        if level == "red":
            requires_confirmation = True
            requires_strong_auth = True
        if level == "green":
            requires_confirmation = False
        if not requires_confirmation:
            requires_strong_auth = False

        from .payment_auth import enabled as payment_enabled
        if payment_enabled() and action in WRITE_ACTIONS:
            # Authentication strength changes; monetary limits, ownership,
            # suitability, confirmations and the original risk colors do not.
            requires_strong_auth = True
            requires_confirmation = True
            reasons.append('payment_credential_required')
            if action=='transfer_money':
                from .payment_auth import simulated_external
                if simulated_external(arguments.get('recipient_id')):
                    level='red';reasons.append('unverified_simulated_external_target')
        authentication = (
            "mock_otp" if requires_strong_auth else (
                "confirmation" if requires_confirmation else "none"
            )
        )
        digest = compute_action_digest(action, arguments)
        now = self._now()
        decision = PolicyDecision(
            decision_id=f"pd_{secrets.token_hex(12)}",
            policy_version=self.config.policy_version+('-payment1' if payment_enabled() else ''),
            permission_level=level,
            allowed=True,
            reason_codes=reasons or ["default_policy"],
            requires_confirmation=requires_confirmation,
            required_authentication=authentication,
            requires_strong_auth=requires_strong_auth,
            action=action,
            subject=f"user:{principal.user_id}",
            action_digest=digest,
            summary=self._summary(action, level, snapshot, arguments),
            resources=resource_refs_for_decision(principal, action, arguments),
            business_state=snapshot,
            threshold_cents=threshold_cents,
            projected_total_cents=projected_total_cents,
            decided_at=now,
            idempotency_managed=action in {
                "transfer_money",
                "buy_wealth_product",
                "redeem_wealth_product",
            },
        )
        return decision

    def _transfer_policy(self, snapshot: BusinessStateSnapshot, reasons):
        amount = snapshot.amount_cents
        if amount is None:
            raise PolicyError(
                "security_fact_unavailable", "缺少转账金额安全事实，拒绝执行"
            )
        if snapshot.card_status is None:
            raise PolicyError(
                "security_fact_unavailable", "缺少付款卡安全事实，拒绝执行"
            )
        if snapshot.transfer_daily_total_cents is None:
            raise PolicyError(
                "security_fact_unavailable", "缺少累计转账安全事实，拒绝执行"
            )
        threshold = self.config.daily_transfer_confirm_cents
        projected = snapshot.transfer_daily_total_cents + amount
        reasons.append("cumulative_daily_transfer_total_checked")
        if projected > threshold:
            reasons.append("daily_cumulative_exceeds_confirmation_threshold")
            reasons.append("strong_auth_required")
            return "red", True, True, reasons, threshold, projected
        reasons.append("daily_cumulative_within_confirmation_threshold")
        return "yellow", True, False, reasons, threshold, projected

    def _daily_limit_policy(self, snapshot: BusinessStateSnapshot, reasons):
        amount = snapshot.amount_cents
        if amount is None:
            raise PolicyError(
                "security_fact_unavailable", "缺少新限额安全事实，拒绝执行"
            )
        if snapshot.card_daily_limit_cents is None:
            raise PolicyError(
                "security_fact_unavailable", "缺少银行卡安全事实，拒绝执行"
            )
        threshold = self.config.daily_limit_escalation_cents
        if amount > threshold:
            reasons.append("daily_limit_upper_escalation")
            return "red", True, True, reasons
        reasons.append("daily_limit_default_yellow")
        return "yellow", True, False, reasons

    @staticmethod
    def _summary(action, level, snapshot, arguments) -> str:
        if action == "transfer_money":
            return (
                f"转账 {snapshot.amount_cents or 0} 分，累计后 "
                f"{snapshot.transfer_daily_total_cents or 0} 分，权限等级 {level}"
            )
        if action == "set_daily_limit":
            return f"调整每日限额为 {snapshot.amount_cents or 0} 分，权限等级 {level}"
        if action == "buy_wealth_product":
            return f"理财申购 {snapshot.amount_cents or 0} 分，权限等级 {level}"
        if action == "redeem_wealth_product":
            return f"理财赎回 {snapshot.amount_cents or 0} 分，权限等级 {level}"
        return f"操作 {action}，权限等级 {level}"

    # ------------------------------------------------- escalation comparison
    @staticmethod
    def escalated(original_level: str, current_level: str) -> bool:
        return LEVEL_RANK.get(current_level, 99) > LEVEL_RANK.get(original_level, -1)


def level_rank(level: str) -> int:
    return LEVEL_RANK.get(level, 99)


__all__ = [
    "ACTION_POLICIES",
    "ALWAYS_STRONG_AUTH_ACTIONS",
    "LEVEL_RANK",
    "PolicyError",
    "READ_ACTIONS",
    "RiskEngine",
    "WRITE_ACTIONS",
    "business_date",
    "level_rank",
]
