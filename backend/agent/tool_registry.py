"""显式白名单 Tool Registry；适配现有 Service，不复制业务逻辑。"""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
import re
from typing import Any, Callable

from pydantic import BaseModel

from database import get_connection
from bill_service import get_category_spending, get_monthly_spending, get_transactions_by_month
from card_service import get_cards, report_card_lost, set_daily_limit
from subscription_service import cancel_subscription, get_active_subscriptions, get_monthly_subscription_cost
from transfer_service import execute_transfer, get_transfer_recipients, prepare_transfer
from wealth_service import (
    execute_wealth_purchase,
    execute_wealth_redemption,
    get_risk_profile,
    get_wealth_holdings,
    get_wealth_products,
    prepare_wealth_purchase,
    prepare_wealth_redemption,
    strict_wealth_amount_to_cents,
)

from .schemas import (
    BuyWealthExecutionArguments,
    BuyWealthProductArguments,
    CancelSubscriptionArguments,
    CancelSubscriptionExecutionArguments,
    EmptyArguments,
    MonthArguments,
    OptionalCardArguments,
    ReportCardLostArguments,
    ReportCardLostExecutionArguments,
    RedeemWealthExecutionArguments,
    RedeemWealthProductArguments,
    SetDailyLimitArguments,
    SetDailyLimitExecutionArguments,
    TransferExecutionArguments,
    TransferMoneyArguments,
    ToolResult,
    WealthProductArguments,
)


class ToolRisk(str, Enum):
    READ_ONLY = "read_only"
    WRITE = "write"


ReadHandler = Callable[[BaseModel], dict[str, Any]]
PrepareHandler = Callable[[BaseModel], tuple[dict[str, Any], dict[str, Any]]]
RevalidateHandler = Callable[
    [BaseModel, dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]
]
ConfirmHandler = Callable[[dict[str, Any]], dict[str, Any]]
ConfirmationBuilder = Callable[[dict[str, Any], str | None], str]
VerifyHandler = Callable[[ToolResult], tuple[bool, str]]


@dataclass(frozen=True)
class ConditionFieldSpec:
    """One public, stable scalar that a workflow condition may read."""

    value_type: str
    unit: str
    operators: tuple[str, ...]
    description: str

    def as_dict(self):
        return {
            "type": self.value_type,
            "unit": self.unit,
            "operators": list(self.operators),
            "description": self.description,
        }


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    argument_model: type[BaseModel]
    risk: ToolRisk
    execution_model: type[BaseModel] | None = None
    execute: ReadHandler | None = None
    prepare: PrepareHandler | None = None
    revalidate: RevalidateHandler | None = None
    confirm: ConfirmHandler | None = None
    confirmation_builder: ConfirmationBuilder | None = None
    uses_idempotency_key: bool = False
    dynamic_confirmation: bool = False
    result_data_type: type | tuple[type, ...] | None = None
    verify: VerifyHandler | None = None
    condition_fields: dict[str, ConditionFieldSpec] = field(default_factory=dict)

    def as_llm_tool(self):
        description = self.description
        if self.condition_fields:
            entries = []
            for path, contract in sorted(self.condition_fields.items()):
                operators = "/".join(contract.operators)
                entries.append(
                    f"{path} ({contract.value_type}, unit={contract.unit}, "
                    f"operators={operators}): {contract.description}"
                )
            description += " 条件可读字段目录：" + "；".join(entries) + "。"
        description += (
            " 仅当这是用户唯一业务目标时可直接调用；若同一请求还有前置、后续、"
            "并列或条件目标，必须改用一次 create_task_plan 保留全部目标。"
            " ‘然后’‘再’‘完成后’‘成功后’只用 depends_on 表示顺序，不生成条件；"
            "只有用户明确要求比较本工具目录公开的结果字段时才生成 StepCondition。"
        )
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description,
                "parameters": self.argument_model.model_json_schema(),
            },
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec):
        if spec.name in self._tools:
            raise ValueError(f"工具已注册：{spec.name}")
        for path, contract in spec.condition_fields.items():
            if not isinstance(path, str) or not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){0,3}",
                path,
            ):
                raise ValueError(f"非法条件字段路径：{path!r}")
            if any(part.startswith("_") or "__" in part for part in path.split(".")):
                raise ValueError(f"条件字段不得访问私有路径：{path}")
            if contract.value_type not in {"number", "integer", "boolean", "string"}:
                raise ValueError(f"非法条件字段类型：{contract.value_type}")
            if not contract.unit or not contract.description:
                raise ValueError("条件字段必须声明单位和业务含义")
            allowed = {"eq", "ne", "gt", "gte", "lt", "lte"}
            if not contract.operators or set(contract.operators) - allowed:
                raise ValueError(f"非法条件操作符：{contract.operators}")
        self._tools[spec.name] = spec

    def get(self, name):
        from storage.config import pg02_enabled as enabled,PG02_SUPPORTED_TOOLS as SUPPORTED_TOOLS
        if enabled() and name not in SUPPORTED_TOOLS:
            return None
        return self._tools.get(name)

    def candidate_recipient_ids(self, tool_name, arguments, principal=None):
        """Candidates a disambiguation clarification is allowed to resolve to.

        The list is taken from the live read-only registration query, so a
        disambiguation reply can never select a recipient outside it. Returns
        ``None`` when no candidate set applies (the check is then skipped).
        """
        if tool_name != "transfer_money" or not isinstance(arguments, dict):
            return None
        recipient_name = arguments.get("recipient_name")
        if not isinstance(recipient_name, str) or not recipient_name.strip():
            return None
        spec = self.get("get_transfer_recipients")
        if spec is None or spec.execute is None:
            return None
        try:
            result = spec.execute(EmptyArguments(), principal)
        except TypeError:
            try:
                result = spec.execute(EmptyArguments())
            except Exception:
                return None
        except Exception:
            return None
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list):
            return None
        return {
            item["recipient_id"]
            for item in data
            if isinstance(item, dict)
            and isinstance(item.get("recipient_id"), int)
            and isinstance(item.get("name"), str)
            and item["name"] == recipient_name.strip()
        }

    def llm_tools(self):
        return [spec.as_llm_tool() for spec in self._tools.values() if self.get(spec.name) is not None]

    def condition_catalog(self):
        """Stable, sensitive-value-free catalog shared by LLM and validator."""
        return {
            name: {
                path: contract.as_dict()
                for path, contract in sorted(spec.condition_fields.items())
            }
            for name, spec in sorted(self._tools.items())
            if spec.condition_fields
        }

    @property
    def names(self):
        return tuple(name for name in self._tools if self.get(name) is not None)


def _get_cards(_arguments, principal=None):
    return get_cards(principal=principal)


def _get_monthly(arguments, principal=None):
    return get_monthly_spending(
        arguments.month, arguments.card_id, principal=principal
    )


def _get_transactions(arguments, principal=None):
    return get_transactions_by_month(
        arguments.month, arguments.card_id, principal=principal
    )


def _get_categories(arguments, principal=None):
    return get_category_spending(
        arguments.month, arguments.card_id, principal=principal
    )


def _get_subscriptions(arguments, principal=None):
    return get_active_subscriptions(arguments.card_id, principal=principal)


def _get_subscription_cost(arguments, principal=None):
    return get_monthly_subscription_cost(arguments.card_id, principal=principal)


def _get_transfer_recipients(_arguments, principal=None):
    return get_transfer_recipients(principal=principal)


def _get_risk_profile(_arguments, principal=None):
    return get_risk_profile(principal=principal)


def _get_wealth_products(arguments, principal=None):
    return get_wealth_products(
        arguments.risk_level, arguments.status, arguments.suitable_only,
        principal=principal,
    )


def _get_wealth_holdings(_arguments, principal=None):
    return get_wealth_holdings(principal=principal)


def _prepare_cancel(arguments, principal=None):
    """通过只读 Service 把名称固定为唯一订阅 ID，再调用写 Service 预览。"""
    subscriptions_result = get_active_subscriptions(principal=principal)
    if not subscriptions_result["success"]:
        return subscriptions_result, {}

    target = arguments.subscription.strip().casefold()
    items = subscriptions_result["data"]
    exact = [item for item in items if item["name"].strip().casefold() == target]
    matches = exact or [
        item
        for item in items
        if target in item["name"].strip().casefold()
        or item["name"].strip().casefold() in target
    ]
    if not matches:
        return {
            "success": False,
            "message": f"未找到名为“{arguments.subscription.strip()}”的有效订阅",
            "data": None,
        }, {}
    if len(matches) > 1:
        return {
            "success": False,
            "message": "找到多个匹配订阅，请提供更准确的订阅名称",
            "data": {"matches": [item["name"] for item in matches]},
        }, {}
    subscription_id = matches[0]["subscription_id"]
    return cancel_subscription(subscription_id, principal=principal), {
        "subscription_id": subscription_id
    }


def _confirm_cancel(execution_arguments):
    return cancel_subscription(
        execution_arguments["subscription_id"], security_context=execution_arguments.get("security_context")
    )


def _cancel_confirmation(preview, _confirmation_phrase=None):
    name = preview.get("name", "该订阅")
    amount = preview.get("amount")
    amount_text = f"（每月 {amount:.2f} 元）" if isinstance(amount, (int, float)) else ""
    return (
        f"确认停止{name}{amount_text}的后续自动扣款授权吗？"
        "此操作不等于取消商户会员。请回复“确认”或“取消”。"
    )


def _prepare_report_lost(arguments, principal=None):
    return report_card_lost(
        arguments.card_id, principal=principal, security_context=None
    ), {
        "card_id": arguments.card_id
    }


def _confirm_report_lost(execution_arguments):
    return report_card_lost(
        execution_arguments["card_id"],
        security_context=execution_arguments.get("security_context"),
    )


def _lost_confirmation(preview, _confirmation_phrase=None):
    masked = preview.get("masked_card_number", "该银行卡")
    return f"确认挂失银行卡 {masked} 吗？请回复“确认”或“取消”。"


def _prepare_daily_limit(arguments, principal=None):
    result = set_daily_limit(
        arguments.card_id, arguments.daily_limit, principal=principal
    )
    cents = strict_wealth_amount_to_cents(arguments.daily_limit)
    execution_arguments = (
        {"card_id": arguments.card_id, "new_limit_cents": cents}
        if result.get("success") is True and cents is not None
        else {}
    )
    return result, execution_arguments


def _confirm_daily_limit(execution_arguments):
    return set_daily_limit(
        execution_arguments["card_id"],
        Decimal(execution_arguments["new_limit_cents"]) / Decimal("100"),
        security_context=execution_arguments.get("security_context"),
    )


def _daily_limit_confirmation(preview, _confirmation_phrase=None):
    return (
        f"确认将银行卡 {preview.get('masked_card_number', '未知')} "
        f"的每日交易限额从 {preview.get('old_limit', 0):.2f} 元修改为 "
        f"{preview.get('new_limit', 0):.2f} 元吗？确认后才会修改。"
        "请回复“确认”或“取消”。"
    )


def _verify_cancel_subscription(result):
    data = result.data
    if not isinstance(data, dict) or not isinstance(data.get("subscription_id"), int):
        return False, "订阅结果缺少有效编号。"
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT status FROM subscriptions WHERE subscription_id = ?",
            (data["subscription_id"],),
        ).fetchone()
        return (
            row is not None and row["status"] == "cancelled",
            "订阅状态核验完成。",
        )
    finally:
        connection.close()


def _verify_report_card_lost(result):
    data = result.data
    if not isinstance(data, dict) or not isinstance(data.get("card_id"), int):
        return False, "挂失结果缺少有效银行卡编号。"
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT status FROM cards WHERE card_id = ?", (data["card_id"],)
        ).fetchone()
        return row is not None and row["status"] == "lost", "银行卡状态核验完成。"
    finally:
        connection.close()


def _yuan_value_to_cents(value):
    decimal_value = Decimal(str(value))
    cents = decimal_value * Decimal("100")
    if not cents.is_finite() or cents != cents.to_integral_value():
        raise ValueError("invalid monetary result")
    return int(cents)


def _verify_daily_limit(result):
    data = result.data
    if not isinstance(data, dict) or not isinstance(data.get("card_id"), int):
        return False, "限额结果缺少有效银行卡编号。"
    expected_cents = _yuan_value_to_cents(data.get("new_limit"))
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT daily_limit_cents FROM cards WHERE card_id = ?",
            (data["card_id"],),
        ).fetchone()
        return (
            row is not None and row["daily_limit_cents"] == expected_cents,
            "银行卡每日限额核验完成。",
        )
    finally:
        connection.close()


def _verify_transfer(result):
    from storage.config import pg02_enabled
    if pg02_enabled():
        from storage.pg02 import verify_transfer_result
        return verify_transfer_result(result)
    data = result.data
    required = {"transfer_id", "from_card_id", "recipient_id", "amount", "balance_after"}
    if not isinstance(data, dict) or not required <= set(data):
        return False, "转账结果缺少关键字段。"
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT * FROM transfer_orders WHERE transfer_id = ?",
            (data["transfer_id"],),
        ).fetchone()
        card = connection.execute(
            "SELECT balance_cents FROM cards WHERE card_id = ?",
            (data["from_card_id"],),
        ).fetchone()
        expected_balance = _yuan_value_to_cents(data["balance_after"])
        verified = (
            row is not None
            and card is not None
            and row["status"] == "completed"
            and row["from_card_id"] == data["from_card_id"]
            and row["recipient_id"] == data["recipient_id"]
            and row["amount_cents"] == _yuan_value_to_cents(data["amount"])
            and row["balance_after_cents"] == expected_balance
            and card["balance_cents"] == expected_balance
        )
        return verified, "转账订单、关键业务参数与银行卡当前余额核验完成。"
    finally:
        connection.close()


def _verify_wealth_order(result):
    from storage.config import pg02_enabled
    if pg02_enabled():
        from storage.pg04 import verify_result
        return verify_result(result)
    data = result.data
    required = {
        "order_id", "order_type", "product_id", "holding_id", "card_id",
        "amount", "principal_after", "card_balance_after",
    }
    if not isinstance(data, dict) or not required <= set(data):
        return False, "理财结果缺少关键字段。"
    connection = get_connection()
    try:
        order = connection.execute(
            "SELECT * FROM wealth_orders WHERE order_id = ?", (data["order_id"],)
        ).fetchone()
        holding = connection.execute(
            "SELECT principal_cents FROM wealth_holdings WHERE holding_id = ?",
            (data["holding_id"],),
        ).fetchone()
        card = connection.execute(
            "SELECT balance_cents FROM cards WHERE card_id = ?", (data["card_id"],)
        ).fetchone()
        expected_principal = _yuan_value_to_cents(data["principal_after"])
        expected_balance = _yuan_value_to_cents(data["card_balance_after"])
        verified = (
            order is not None
            and holding is not None
            and card is not None
            and order["status"] == "completed"
            and order["order_type"] == data["order_type"]
            and order["product_id"] == data["product_id"]
            and order["holding_id"] == data["holding_id"]
            and order["card_id"] == data["card_id"]
            and order["amount_cents"] == _yuan_value_to_cents(data["amount"])
            and order["principal_after_cents"] == expected_principal
            and order["card_balance_after_cents"] == expected_balance
            and holding["principal_cents"] == expected_principal
            and card["balance_cents"] == expected_balance
        )
        return verified, "模拟理财订单、持仓本金和银行卡余额核验完成。"
    finally:
        connection.close()


def build_default_registry(
    now_provider=None,
    transfer_id_provider=None,
    wealth_order_id_provider=None,
    include_analysis=False,
):
    def transfer_execution_arguments(preview):
        return {
            "from_card_id": preview["from_card_id"],
            "recipient_id": preview["recipient_id"],
            "amount_cents": preview["amount_cents"],
            "remark": preview.get("remark"),
        }

    def prepare_transfer_tool(arguments, principal=None):
        result = prepare_transfer(
            arguments.from_card_id,
            arguments.recipient_name,
            arguments.amount,
            remark=arguments.remark,
            now_provider=now_provider,
            recipient_id=arguments.recipient_id,
            principal=principal,
        )
        execution_arguments = (
            transfer_execution_arguments(result["data"])
            if result.get("success") is True
            else {}
        )
        return result, execution_arguments

    def revalidate_transfer_tool(arguments, saved_execution_arguments, principal=None):
        saved_recipient_id = saved_execution_arguments.get("recipient_id")
        if (
            arguments.recipient_id is not None
            and arguments.recipient_id != saved_recipient_id
        ):
            return {
                "success": False,
                "error_code": "pending_action_mismatch",
                "message": "待确认收款人与原预览不一致，未执行转账",
                "data": None,
            }, {}
        result = prepare_transfer(
            arguments.from_card_id,
            arguments.recipient_name,
            arguments.amount,
            remark=arguments.remark,
            now_provider=now_provider,
            recipient_id=saved_recipient_id,
            principal=principal,
        )
        execution_arguments = (
            transfer_execution_arguments(result["data"])
            if result.get("success") is True
            else {}
        )
        return result, execution_arguments

    def confirm_transfer_tool(execution_arguments):
        return execute_transfer(
            execution_arguments["from_card_id"],
            execution_arguments["recipient_id"],
            execution_arguments["amount_cents"],
            execution_arguments["idempotency_key"],
            remark=execution_arguments.get("remark"),
            security_context=execution_arguments.get("security_context"),
            now_provider=now_provider,
            id_provider=transfer_id_provider,
        )

    def transfer_confirmation(preview, confirmation_phrase=None):
        remark = preview.get("remark") or "无"
        risk = str(preview.get("risk_level", "")).upper()
        return (
            "请核对转账信息："
            f"付款卡 {preview.get('from_masked_card_number', '未知')}；"
            f"收款人 {preview.get('recipient_name', '未知')}；"
            f"收款银行 {preview.get('recipient_bank_name', '未知')}；"
            f"收款账户 {preview.get('recipient_masked_account', '未知')}；"
            f"金额 {preview.get('amount', 0):.2f} 元；"
            f"备注 {remark}；风险等级 {risk}。"
            f"若确认执行，请回复“{confirmation_phrase}”。"
        )

    def prepare_wealth_purchase_tool(arguments, principal=None):
        result = prepare_wealth_purchase(
            arguments.product_id,
            arguments.card_id,
            arguments.amount,
            now_provider=now_provider,
            principal=principal,
        )
        amount_cents = strict_wealth_amount_to_cents(arguments.amount)
        execution_arguments = (
            {
                "product_id": arguments.product_id,
                "card_id": arguments.card_id,
                "amount_cents": amount_cents,
            }
            if result.get("success") is True and amount_cents is not None
            else {}
        )
        return result, execution_arguments

    def confirm_wealth_purchase_tool(execution_arguments):
        return execute_wealth_purchase(
            execution_arguments["product_id"],
            execution_arguments["card_id"],
            execution_arguments["amount_cents"],
            execution_arguments["idempotency_key"],
            security_context=execution_arguments.get("security_context"),
            now_provider=now_provider,
            id_provider=wealth_order_id_provider,
        )

    def wealth_purchase_confirmation(preview, _confirmation_phrase=None):
        return (
            "请核对模拟理财购买信息："
            f"产品 {preview.get('product_name', '未知')}；"
            f"产品风险 {preview.get('product_risk_level', '未知')}；"
            f"用户风险等级 {preview.get('user_risk_level', '未知')}；"
            f"购买金额 {preview.get('amount', 0):.2f} 元；"
            f"付款卡 {preview.get('masked_card_number', '未知')}；"
            f"参考年化收益率 {preview.get('reference_annual_rate', 0):.2f}%。"
            "参考收益不代表实际收益，也不构成保证。"
            "确认后才会执行，请回复“确认”或“取消”。"
        )

    def prepare_wealth_redemption_tool(arguments, principal=None):
        result = prepare_wealth_redemption(
            arguments.holding_id,
            arguments.amount,
            now_provider=now_provider,
            principal=principal,
        )
        amount_cents = strict_wealth_amount_to_cents(arguments.amount)
        preview = result.get("data") or {}
        execution_arguments = (
            {
                "holding_id": arguments.holding_id,
                "product_id": preview["product_id"],
                "card_id": preview["card_id"],
                "amount_cents": amount_cents,
            }
            if result.get("success") is True and amount_cents is not None
            else {}
        )
        return result, execution_arguments

    def confirm_wealth_redemption_tool(execution_arguments):
        return execute_wealth_redemption(
            execution_arguments["holding_id"],
            execution_arguments["amount_cents"],
            execution_arguments["idempotency_key"],
            security_context=execution_arguments.get("security_context"),
            now_provider=now_provider,
            id_provider=wealth_order_id_provider,
        )

    def wealth_redemption_confirmation(preview, _confirmation_phrase=None):
        return (
            "请核对模拟理财赎回信息："
            f"产品 {preview.get('product_name', '未知')}；"
            f"当前持仓本金 {preview.get('current_principal', 0):.2f} 元；"
            f"本次赎回 {preview.get('redemption_amount', 0):.2f} 元；"
            f"预计剩余本金 {preview.get('remaining_principal', 0):.2f} 元；"
            f"到账卡 {preview.get('masked_card_number', '未知')}。"
            "当前版本不计算投资收益，赎回到账金额等于本次赎回本金。"
            "确认后才会执行，请回复“确认”或“取消”。"
        )

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="get_cards",
            description="查询用户全部银行卡的脱敏信息和真实状态。",
            argument_model=EmptyArguments,
            risk=ToolRisk.READ_ONLY,
            execute=_get_cards,
            result_data_type=list,
        )
    )
    registry.register(
        ToolSpec(
            name="get_monthly_spending",
            description="查询指定月份的真实总支出；用户未给月份且不是明确相对月份时必须澄清。",
            argument_model=MonthArguments,
            risk=ToolRisk.READ_ONLY,
            execute=_get_monthly,
            result_data_type=dict,
            condition_fields={
                "data.total_spending": ConditionFieldSpec(
                    value_type="number",
                    unit="yuan",
                    operators=("gt", "gte", "lt", "lte", "eq", "ne"),
                    description="指定月份总支出",
                ),
                "data.transaction_count": ConditionFieldSpec(
                    value_type="integer",
                    unit="count",
                    operators=("gt", "gte", "lt", "lte", "eq", "ne"),
                    description="指定月份支出交易笔数",
                ),
                "data.card_specified": ConditionFieldSpec(
                    value_type="boolean",
                    unit="boolean",
                    operators=("eq", "ne"),
                    description="查询是否限定了银行卡",
                ),
            },
        )
    )
    registry.register(
        ToolSpec(
            name="get_transactions_by_month",
            description="查询指定月份的脱敏交易流水，供账单明细表展示。",
            argument_model=MonthArguments,
            risk=ToolRisk.READ_ONLY,
            execute=_get_transactions,
            result_data_type=list,
        )
    )
    registry.register(
        ToolSpec(
            name="get_category_spending",
            description="查询指定月份各消费分类的真实金额和占比。",
            argument_model=MonthArguments,
            risk=ToolRisk.READ_ONLY,
            execute=_get_categories,
            result_data_type=dict,
        )
    )
    registry.register(
        ToolSpec(
            name="get_active_subscriptions",
            description="查询当前有效订阅和自动扣款授权。",
            argument_model=OptionalCardArguments,
            risk=ToolRisk.READ_ONLY,
            execute=_get_subscriptions,
            result_data_type=list,
        )
    )
    registry.register(
        ToolSpec(
            name="get_monthly_subscription_cost",
            description="统计当前有效订阅的月度和年度费用，并返回订阅明细。",
            argument_model=OptionalCardArguments,
            risk=ToolRisk.READ_ONLY,
            execute=_get_subscription_cost,
            result_data_type=dict,
        )
    )
    registry.register(
        ToolSpec(
            name="get_transfer_recipients",
            description="查询有效的预登记转账收款人，只返回脱敏账号和可信状态。",
            argument_model=EmptyArguments,
            risk=ToolRisk.READ_ONLY,
            execute=_get_transfer_recipients,
            result_data_type=list,
        )
    )
    registry.register(
        ToolSpec(
            name="get_risk_profile",
            description="查询当前固定演示用户的模拟理财风险承受等级。",
            argument_model=EmptyArguments,
            risk=ToolRisk.READ_ONLY,
            execute=_get_risk_profile,
            result_data_type=dict,
        )
    )
    registry.register(
        ToolSpec(
            name="get_wealth_products",
            description="查询数据库中的模拟理财产品，可按风险等级或状态筛选。",
            argument_model=WealthProductArguments,
            risk=ToolRisk.READ_ONLY,
            execute=_get_wealth_products,
            result_data_type=list,
        )
    )
    registry.register(
        ToolSpec(
            name="get_wealth_holdings",
            description="查询当前固定演示用户的模拟理财持仓本金，不提供实时市值或收益。",
            argument_model=EmptyArguments,
            risk=ToolRisk.READ_ONLY,
            execute=_get_wealth_holdings,
            result_data_type=list,
        )
    )
    registry.register(
        ToolSpec(
            name="cancel_subscription",
            description="按用户明确指定的订阅名称停止后续自动扣款授权；程序必定另行确认。",
            argument_model=CancelSubscriptionArguments,
            risk=ToolRisk.WRITE,
            execution_model=CancelSubscriptionExecutionArguments,
            prepare=_prepare_cancel,
            confirm=_confirm_cancel,
            confirmation_builder=_cancel_confirmation,
            result_data_type=dict,
            verify=_verify_cancel_subscription,
        )
    )
    registry.register(
        ToolSpec(
            name="report_card_lost",
            description="按银行卡编号挂失银行卡；程序必定另行确认。",
            argument_model=ReportCardLostArguments,
            risk=ToolRisk.WRITE,
            execution_model=ReportCardLostExecutionArguments,
            prepare=_prepare_report_lost,
            confirm=_confirm_report_lost,
            confirmation_builder=_lost_confirmation,
            result_data_type=dict,
            verify=_verify_report_card_lost,
        )
    )
    registry.register(
        ToolSpec(
            name="set_daily_limit",
            description="按银行卡编号修改每日交易限额；程序必定另行确认。",
            argument_model=SetDailyLimitArguments,
            risk=ToolRisk.WRITE,
            execution_model=SetDailyLimitExecutionArguments,
            prepare=_prepare_daily_limit,
            confirm=_confirm_daily_limit,
            confirmation_builder=_daily_limit_confirmation,
            result_data_type=dict,
            verify=_verify_daily_limit,
        )
    )
    registry.register(
        ToolSpec(
            name="transfer_money",
            description=(
                "向预登记收款人转账；付款卡、收款人和金额必须由用户明确提供，"
                "程序会执行风险校验并要求动态确认。"
            ),
            argument_model=TransferMoneyArguments,
            risk=ToolRisk.WRITE,
            execution_model=TransferExecutionArguments,
            prepare=prepare_transfer_tool,
            revalidate=revalidate_transfer_tool,
            confirm=confirm_transfer_tool,
            confirmation_builder=transfer_confirmation,
            uses_idempotency_key=True,
            dynamic_confirmation=True,
            result_data_type=dict,
            verify=_verify_transfer,
        )
    )
    registry.register(
        ToolSpec(
            name="buy_wealth_product",
            description=(
                "使用指定银行卡购买数据库中的模拟理财产品；"
                "风险适配、余额和确认全部由 Python 决定。"
            ),
            argument_model=BuyWealthProductArguments,
            risk=ToolRisk.WRITE,
            execution_model=BuyWealthExecutionArguments,
            prepare=prepare_wealth_purchase_tool,
            confirm=confirm_wealth_purchase_tool,
            confirmation_builder=wealth_purchase_confirmation,
            uses_idempotency_key=True,
            result_data_type=dict,
            verify=_verify_wealth_order,
        )
    )
    registry.register(
        ToolSpec(
            name="redeem_wealth_product",
            description=(
                "按持仓编号和金额赎回模拟理财本金；"
                "不计算投资收益，程序必定另行确认。"
            ),
            argument_model=RedeemWealthProductArguments,
            risk=ToolRisk.WRITE,
            execution_model=RedeemWealthExecutionArguments,
            prepare=prepare_wealth_redemption_tool,
            confirm=confirm_wealth_redemption_tool,
            confirmation_builder=wealth_redemption_confirmation,
            uses_idempotency_key=True,
            result_data_type=dict,
            verify=_verify_wealth_order,
        )
    )
    if include_analysis:
        from storage.config import pg02_enabled
        if pg02_enabled():
            from data_query import QueryBatch,read_tool as read_data
            registry.register(ToolSpec(name='query_data',
                description='通用只读查询计划；仅服务端编译SQL，来源/时间/商户/金额/分组/比较严格限界。自然语言入口优先通过route_conversation提交完整目标。直接工具不能使用current_document，须提供已核实document_id。金额单位整数分，不能填owner或SQL。',
                argument_model=QueryBatch,risk=ToolRisk.READ_ONLY,execute=read_data,result_data_type=dict))
            from billvision.models import Query as UploadedBillQuery
            from billvision.service import read_tool as read_uploaded_bills
            registry.register(ToolSpec(name='query_uploaded_bills',
                description='只读本人已确认上传图片中的账单，非银行流水。查看本人所有已确认上传账单时document_id=null或省略，直接查询全部已确认批次，无需让用户选择某一份；仅“这张/刚上传”才从服务端选择上下文取document_id。日期和商户都是可选过滤：未提日期时start/end_exclusive=null表示上传记录的全部日期，不需要追问月份；未提商户时merchant=null。有明确日期则使用[start,end_exclusive)，商户精确过滤。购买、退款、其他收入分列，不推断整月覆盖，不合并银行流水。不得确认导入或付款。',
                argument_model=UploadedBillQuery,risk=ToolRisk.READ_ONLY,execute=read_uploaded_bills,result_data_type=dict))
        from analysis_service import CATALOG, analyze, verify_result
        registry.enforce_read_permissions = True
        descriptions = {
            'query_financial_records': '查询上海[start,end_exclusive)购买支出或已记录现金流出，商户只用已登记精确别名；退款单列不扣减；compare_previous仅相邻整月比较。未知商户、净消费、歧义必须澄清。',
            'get_monthly_cash_flow_report': '查询明确月份的已记录收支报告；缺少时间范围必须澄清。',
            'get_annual_cash_flow_report': '查询明确年份的已记录收支报告和12个月分布，不能推断全年实际表现。',
            'get_expense_clues': '只分析明确月份的需核查支出线索，不能自行创建取消、挂失或交易任务。',
            'compare_wealth_products': '比较2至5个用户明确选择的产品编号；模糊产品或重复选择必须澄清，不自动购买。',
            'explain_wealth_suitability': '解释明确产品风险匹配和查询时购买条件；不是购买授权。',
        }
        for name, (model, _, _, _) in CATALOG.items():
            fields = {}
            if name=='query_financial_records':
                fields['data.current.amount_cents']=ConditionFieldSpec(value_type='integer',unit='cents',
                    operators=('gt','gte','lt','lte','eq','ne'),
                    description='已按请求口径和范围独立核验的当前期间金额，整数分；阈值必须从元精确换算')
            if name in {'get_monthly_cash_flow_report', 'get_annual_cash_flow_report'}:
                for metric in ('income', 'expense', 'net_cash_flow', 'transaction_count'):
                    fields['data.totals.'+metric] = ConditionFieldSpec(
                        value_type='integer' if metric == 'transaction_count' else 'number',
                        unit='count' if metric == 'transaction_count' else 'yuan',
                        operators=('gt','gte','lt','lte','eq','ne'),
                        description='已记录流水的'+metric)
            registry.register(ToolSpec(name=name, description=descriptions[name],
                argument_model=model, risk=ToolRisk.READ_ONLY,
                execute=lambda args, principal=None, name=name: analyze(name, args, principal),
                result_data_type=dict, verify=verify_result, condition_fields=fields))
    return registry


def service_result_to_tool_result(result, error_code="service_error"):
    if not isinstance(result, dict):
        return ToolResult(
            status="error",
            error_code="invalid_service_result",
            message="业务服务返回了无效结果",
        )
    if result.get("success") is True:
        return ToolResult(
            status="success",
            data=result.get("data"),
            message=str(result.get("message", "操作成功")),
        )
    return ToolResult(
        status="error",
        data=result.get("data"),
        error_code=str(result.get("error_code") or error_code),
        message=str(result.get("message", "业务服务执行失败")),
    )
