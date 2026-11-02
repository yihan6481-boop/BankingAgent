"""Action runners + the single prepare/execute entry for write operations.

Each supported write action is described exactly once here:

* :meth:`ActionRunner.prepare` produces the preview through the *existing*
  service preview function (no business write, no cumulative amount consumed);
* :meth:`ActionRunner.final_arguments` returns the normalized execution
  arguments that the confirmation, the OTP challenge and the authorization
  grant all bind to;
* :meth:`ActionRunner.execute` performs the service write with the trusted
  authorization context.

:class:`ActionPlanner` is the shared entry point used by the Agent Tool Router,
the Workflow engine, the V2 API and the CLI, so no path can implement its own
"check and execute" logic and drift away from the policy.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from database import get_connection

from .config import DEFAULT_SECURITY_CONFIG, SecurityConfig
from .execution import AuthorizationGrant, WriteSecurityContext
from .ownership import OwnershipError, policy_owner_scope
from .policy import PolicyError, RiskEngine
from .schemas import PolicyDecision, Principal
from .write_context import owner_scope

PREVIEW_ERROR_CODES = {
    "card_not_found",
    "card_unavailable",
    "recipient_not_found",
    "recipient_disabled",
    "recipient_untrusted",
    "recipient_ambiguous",
    "recipient_mismatch",
    "insufficient_balance",
    "daily_limit_exceeded",
    "product_not_found",
    "product_inactive",
    "risk_mismatch",
    "below_minimum",
    "holding_not_found",
    "holding_redeemed",
    "amount_exceeds_principal",
    "risk_profile_not_found",
    "recipient_required",
    "invalid_amount",
    "invalid_identifier",
    "invalid_card_id",
    "invalid_recipient_id",
    "invalid_remark",
    "invalid_holding_id",
    "invalid_time",
}


@dataclass
class PrepareOutcome:
    success: bool
    message: str
    preview: dict | None = None
    error_code: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ServiceFailure(Exception):
    """Raised by a runner when a service returns a non-success result."""

    def __init__(self, error_code: str, message: str, data=None):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.data = data


class ActionRunner:
    action = "unsupported"
    service_name: str | None = None

    def prepare(self, arguments: dict, principal: Principal | None) -> PrepareOutcome:
        raise NotImplementedError

    def final_arguments(self, arguments: dict) -> dict:
        return dict(arguments)

    def execute(self, arguments: dict, context: WriteSecurityContext) -> dict:
        raise NotImplementedError


# --------------------------------------------------------------- runners
class CancelSubscriptionRunner(ActionRunner):
    action = "cancel_subscription"

    def _resolve(self, arguments, principal):
        """Accept either an explicit ``subscription_id`` or a subscription name.

        The name is resolved to a unique subscription id inside the acting
        principal's owner scope, so a cancellation can never target another
        user's subscription.
        """
        subscription_id = arguments.get("subscription_id")
        if isinstance(subscription_id, int) and not isinstance(subscription_id, bool):
            return subscription_id, None
        name = arguments.get("subscription")
        if not isinstance(name, str) or not name.strip():
            return None, "需要提供订阅编号（subscription_id）或订阅名称（subscription）"
        from subscription_service import get_active_subscriptions

        result = get_active_subscriptions(principal=principal)
        if result.get("success") is not True:
            return None, result.get("message", "订阅查询失败")
        target = name.strip().casefold()
        items = result.get("data") or []
        exact = [item for item in items if item["name"].strip().casefold() == target]
        matches = exact or [
            item
            for item in items
            if target in item["name"].strip().casefold()
            or item["name"].strip().casefold() in target
        ]
        if not matches:
            return None, f"未找到名为“{name.strip()}”的有效订阅"
        if len(matches) > 1:
            return None, "找到多个匹配订阅，请提供更准确的订阅名称或编号"
        return matches[0]["subscription_id"], None

    def prepare(self, arguments, principal):
        from subscription_service import cancel_subscription

        subscription_id, error = self._resolve(arguments, principal)
        if error:
            return PrepareOutcome(
                False, error, error_code="subscription_not_found"
            )
        result = cancel_subscription(subscription_id, principal=principal)
        if result.get("success") is not True:
            return PrepareOutcome(
                False, result.get("message", "订阅预览失败"),
                error_code=result.get("error_code") or "subscription_preview_failed",
            )
        data = dict(result.get("data") or {})
        subscription_id = data.get("subscription_id") or subscription_id
        return PrepareOutcome(
            True, result.get("message", ""),
            preview=data,
            arguments={"subscription_id": subscription_id},
            extra={"subscription_id": subscription_id},
        )

    def execute(self, arguments, context):
        from subscription_service import cancel_subscription

        return cancel_subscription(
            arguments["subscription_id"],
            principal=context.principal,
            security_context=context,
        )


class ReportCardLostRunner(ActionRunner):
    action = "report_card_lost"

    def prepare(self, arguments, principal):
        from card_service import report_card_lost

        result = report_card_lost(arguments.get("card_id"), principal=principal)
        if result.get("success") is not True:
            return PrepareOutcome(
                False, result.get("message", "挂失预览失败"),
                error_code=result.get("error_code") or "card_preview_failed",
            )
        data = dict(result.get("data") or {})
        return PrepareOutcome(
            True, result.get("message", ""),
            preview=data,
            arguments={"card_id": data.get("card_id")},
            extra={"card_id": data.get("card_id")},
        )

    def execute(self, arguments, context):
        from card_service import report_card_lost

        return report_card_lost(
            arguments["card_id"],
            principal=context.principal,
            security_context=context,
        )


class SetDailyLimitRunner(ActionRunner):
    action = "set_daily_limit"

    def prepare(self, arguments, principal):
        from card_service import set_daily_limit

        new_limit_cents = arguments.get("new_limit_cents")
        new_limit = arguments.get("daily_limit")
        if new_limit_cents is None and new_limit is not None:
            from wealth_service import strict_wealth_amount_to_cents

            new_limit_cents = strict_wealth_amount_to_cents(new_limit)
        result = set_daily_limit(
            arguments.get("card_id"), new_limit, principal=principal
        )
        if result.get("success") is not True:
            return PrepareOutcome(
                False, result.get("message", "限额预览失败"),
                error_code=result.get("error_code") or "limit_preview_failed",
            )
        data = dict(result.get("data") or {})
        return PrepareOutcome(
            True, result.get("message", ""),
            preview=data,
            arguments={
                "card_id": data.get("card_id"),
                "new_limit_cents": new_limit_cents,
            },
            extra={"new_limit": data.get("new_limit")},
        )

    def execute(self, arguments, context):
        from card_service import set_daily_limit
        from utils import cents_to_yuan
        from decimal import Decimal

        return set_daily_limit(
            arguments["card_id"],
            Decimal(int(arguments["new_limit_cents"])) / Decimal("100"),
            principal=context.principal,
            security_context=context,
        )


class TransferRunner(ActionRunner):
    action = "transfer_money"

    def prepare(self, arguments, principal):
        from transfer_service import prepare_transfer

        result = prepare_transfer(
            arguments.get("from_card_id"),
            arguments.get("recipient_name"),
            arguments.get("amount"),
            remark=arguments.get("remark"),
            recipient_id=arguments.get("recipient_id"),
            principal=principal,
        )
        if result.get("success") is not True:
            # 失败时仍需保留结构化数据（例如收款人消歧候选），
            # 否则消歧流程无法告知用户可选的编号。
            failure_data = result.get("data")
            return PrepareOutcome(
                False, result.get("message", "转账预览失败"),
                error_code=result.get("error_code") or "transfer_preview_failed",
                preview=failure_data if isinstance(failure_data, dict) else None,
                extra={"preview_data": failure_data},
            )
        data = dict(result.get("data") or {})
        return PrepareOutcome(
            True, result.get("message", ""),
            preview=data,
            arguments={
                "from_card_id": data.get("from_card_id"),
                "recipient_id": data.get("recipient_id"),
                "amount_cents": data.get("amount_cents"),
                "remark": data.get("remark"),
            },
            extra={
                "amount": data.get("amount"),
                "risk_level": data.get("risk_level"),
                "recipient_name": data.get("recipient_name"),
            },
        )

    def final_arguments(self, arguments):
        from .args import transfer_args

        return transfer_args(
            arguments["from_card_id"],
            arguments["recipient_id"],
            arguments["amount_cents"],
            arguments["idempotency_key"],
            arguments.get("remark"),
        )

    def execute(self, arguments, context):
        from transfer_service import execute_transfer

        return execute_transfer(
            arguments["from_card_id"],
            arguments["recipient_id"],
            int(arguments["amount_cents"]),
            arguments["idempotency_key"],
            remark=arguments.get("remark"),
            principal=context.principal,
            security_context=context,
        )


class WealthPurchaseRunner(ActionRunner):
    action = "buy_wealth_product"

    def prepare(self, arguments, principal):
        from wealth_service import prepare_wealth_purchase, strict_wealth_amount_to_cents

        result = prepare_wealth_purchase(
            arguments.get("product_id"),
            arguments.get("card_id"),
            arguments.get("amount"),
            principal=principal,
        )
        if result.get("success") is not True:
            return PrepareOutcome(
                False, result.get("message", "理财申购预览失败"),
                error_code=result.get("error_code") or "wealth_preview_failed",
            )
        data = dict(result.get("data") or {})
        return PrepareOutcome(
            True, result.get("message", ""),
            preview=data,
            arguments={
                "product_id": data.get("product_id"),
                "card_id": data.get("card_id"),
                "amount_cents": strict_wealth_amount_to_cents(
                    arguments.get("amount")
                ),
            },
            extra={"amount": data.get("amount")},
        )

    def final_arguments(self, arguments):
        from .args import wealth_purchase_args

        return wealth_purchase_args(
            arguments["product_id"],
            arguments["card_id"],
            arguments["amount_cents"],
            arguments["idempotency_key"],
        )

    def execute(self, arguments, context):
        from wealth_service import execute_wealth_purchase

        return execute_wealth_purchase(
            arguments["product_id"],
            arguments["card_id"],
            int(arguments["amount_cents"]),
            arguments["idempotency_key"],
            principal=context.principal,
            security_context=context,
        )


class WealthRedemptionRunner(ActionRunner):
    action = "redeem_wealth_product"

    def prepare(self, arguments, principal):
        from wealth_service import (
            prepare_wealth_redemption,
            strict_wealth_amount_to_cents,
        )

        result = prepare_wealth_redemption(
            arguments.get("holding_id"),
            arguments.get("amount"),
            principal=principal,
        )
        if result.get("success") is not True:
            return PrepareOutcome(
                False, result.get("message", "理财赎回预览失败"),
                error_code=result.get("error_code") or "wealth_preview_failed",
            )
        data = dict(result.get("data") or {})
        return PrepareOutcome(
            True, result.get("message", ""),
            preview=data,
            arguments={
                "holding_id": data.get("holding_id"),
                "amount_cents": strict_wealth_amount_to_cents(
                    arguments.get("amount")
                ),
                "card_id": data.get("card_id"),
                "product_id": data.get("product_id"),
            },
            extra={"amount": data.get("amount")},
        )

    def final_arguments(self, arguments):
        from .args import wealth_redemption_args

        return wealth_redemption_args(
            arguments["holding_id"],
            arguments["amount_cents"],
            arguments["idempotency_key"],
        )

    def execute(self, arguments, context):
        from wealth_service import execute_wealth_redemption

        return execute_wealth_redemption(
            arguments["holding_id"],
            int(arguments["amount_cents"]),
            arguments["idempotency_key"],
            principal=context.principal,
            security_context=context,
        )


RUNNERS: dict[str, ActionRunner] = {
    runner.action: runner
    for runner in (
        CancelSubscriptionRunner(),
        ReportCardLostRunner(),
        SetDailyLimitRunner(),
        TransferRunner(),
        WealthPurchaseRunner(),
        WealthRedemptionRunner(),
    )
}

NEEDS_IDEMPOTENCY_KEY = {
    "transfer_money",
    "buy_wealth_product",
    "redeem_wealth_product",
}


def get_runner(action: str) -> ActionRunner | None:
    return RUNNERS.get(action)


# --------------------------------------------------------------- planner
@dataclass
class WritePlan:
    """Result of the prepare step shared by every write entry point."""

    action: str
    permitted: bool
    decision: PolicyDecision | None
    preview: dict | None
    arguments: dict[str, Any]
    message: str
    error_code: str | None = None
    idempotency_key: str | None = None
    requires_strong_auth: bool = False
    requires_confirmation: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def can_execute_with_confirmation(self) -> bool:
        return (
            self.permitted
            and self.preview is not None
            and not self.requires_strong_auth
        )


class ActionPlanner:
    """Policy + preview + final-argument resolution for write actions."""

    def __init__(
        self,
        config: SecurityConfig | None = None,
        policy: RiskEngine | None = None,
        plan_hook=None,
    ):
        self.config = config or DEFAULT_SECURITY_CONFIG
        from storage.config import pg02_enabled as enabled
        if enabled() and policy is None:
            from .service import get_security_services
            policy=get_security_services().policy
        self.policy = policy or RiskEngine(self.config)
        # 可选钩子：评测驾驞可用它注入注册表自己的 prepare/confirm（故障注入），
        # 而写入仍然经过同一安全入口。
        self.plan_hook = plan_hook

    def prepare(
        self,
        action: str,
        arguments: dict,
        principal: Principal | None,
        idempotency_key: str | None = None,
    ) -> WritePlan:
        from storage.config import pg02_enabled as enabled,PG02_SUPPORTED_TOOLS as SUPPORTED_TOOLS
        if enabled() and action not in SUPPORTED_TOOLS:
            return WritePlan(action, False, None, None, dict(arguments or {}),
                             message='PG02暂不支持该业务，整条请求未执行', error_code='pg02_unsupported_business')
        runner = get_runner(action)
        if runner is None:
            return WritePlan(
                action, False, None, None, dict(arguments or {}),
                message="未知写操作，拒绝执行", error_code="unknown_action",
            )
        # 只有真正带预览钩子的写工具才走 hook；只读工具继续用 runner。
        outcome = (
            self.plan_hook(action, dict(arguments or {}), principal)
            if self.plan_hook is not None and action in NEEDS_IDEMPOTENCY_KEY
            or self.plan_hook is not None
            and action
            in {
                "cancel_subscription",
                "report_card_lost",
                "set_daily_limit",
            }
            else runner.prepare(dict(arguments or {}), principal)
        )
        if not outcome.success:
            return WritePlan(
                action, False, None, outcome.preview, dict(outcome.arguments or {}),
                message=outcome.message,
                error_code=outcome.error_code or "preview_failed",
                extra=outcome.extra,
            )
        final = dict(outcome.arguments)
        if action in NEEDS_IDEMPOTENCY_KEY:
            # 幂等键由后端产生；调用方可在一次请求内固定它，使
            # 预览 / 确认 / 强验证 / 执行绑定同一份参数摘要。
            final["idempotency_key"] = idempotency_key or self._new_key(action)
        # 权限决策必须使用**最终执行参数**摘要，确保确认/挑战/授权绑定一致。
        decision_arguments = (
            runner.final_arguments(final)
            if action in NEEDS_IDEMPOTENCY_KEY
            else final
        )
        try:
            decision = self.policy.evaluate(
                action, principal, decision_arguments
            )
        except PolicyError as error:
            return WritePlan(
                action, False, None, outcome.preview, final,
                message=error.message, error_code=error.code,
                idempotency_key=final.get("idempotency_key"),
            )
        return WritePlan(
            action,
            True,
            decision,
            outcome.preview,
            final,
            message=outcome.message,
            idempotency_key=final.get("idempotency_key"),
            requires_strong_auth=decision.requires_strong_auth,
            requires_confirmation=decision.requires_confirmation,
            extra=outcome.extra,
        )

    def final_arguments(self, action: str, arguments: dict) -> dict:
        runner = get_runner(action)
        if runner is None:
            return dict(arguments)
        return runner.final_arguments(dict(arguments))

    def digest_arguments(self, action: str, arguments: dict) -> dict:
        """The exact argument set whose digest binds confirmation and challenge."""
        return self.final_arguments(action, arguments)

    def execute(self, action: str, arguments: dict, context: WriteSecurityContext) -> dict:
        runner = get_runner(action)
        if runner is None:
            raise ServiceFailure("unknown_action", "未知写操作，拒绝执行")
        from .tool_guard import guarded_tool_call
        result = guarded_tool_call(action, lambda: runner.execute(dict(arguments), context), read_only=False)
        if result.get('code') == 'tool_circuit_open':
            return {'success': False, 'error_code': 'tool_circuit_open',
                    'message': '依赖暂时不可用，请稍后核实原任务再继续', 'data': result['data']}
        return result

    @staticmethod
    def _new_key(action: str) -> str:
        import uuid

        return f"{action}_{uuid.uuid4().hex}"


def business_state_summary(action: str, arguments: dict, principal: Principal | None) -> dict:
    """Debug/audit helper: a desensitized snapshot of the checked business facts."""
    try:
        snapshot = RiskEngine().snapshot(action, principal, arguments)
    except Exception:
        return {}
    return snapshot.summary()


__all__ = [
    "ActionPlanner",
    "ActionRunner",
    "PrepareOutcome",
    "RUNNERS",
    "ServiceFailure",
    "WritePlan",
    "business_state_summary",
    "get_runner",
]
