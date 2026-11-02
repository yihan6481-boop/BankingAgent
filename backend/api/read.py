"""Deterministic UI read channel — Contract 1.1 FINAL (V0.7.0 policy aware).

This module deliberately owns a read catalog that is independent from the
Agent/LLM Tool Registry.  The UI can therefore keep using ``/read`` even if an
LLM tool is renamed, removed, or reorganized.

Rules:
- only the explicit READ_SPECS catalog is executable;
- write-operation names are rejected with ``write_tool_not_allowed``;
- every query uses a strict local Pydantic argument model;
- Service output is converted to ``ToolResult`` and then to the same strongly
  typed ``result.kind`` payload used by the chat path;
- no LLM client or API key is required.

V0.7.0 addition: the official task exception makes subscription queries
``yellow`` even though they are read-only, so this channel consults the shared
policy engine before returning data.  It does **not** bypass the policy and it
does **not** become a write tool: a query that needs authorization only ever
creates a read-only authorization todo, and after confirmation the same
independent read catalog is called again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal
from threading import RLock

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent.output_guard import contains_sensitive_result
from agent.schemas import ToolResult
from bill_service import get_category_spending, get_monthly_spending, get_transactions_by_month
from card_service import get_cards
from subscription_service import get_active_subscriptions, get_monthly_subscription_cost
from transfer_service import get_transfer_recipients
from wealth_service import get_risk_profile, get_wealth_holdings, get_wealth_products

from security.policy import PolicyError, RiskEngine
from security.schemas import Principal

from .presenter import _stable_error_code, build_ui_result
from .schemas import ReadRequest, UiEnvelope, UiError, UiInteraction


class _StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _EmptyArgs(_StrictArgs):
    pass


class _OptionalCardArgs(_StrictArgs):
    card_id: int | None = Field(default=None, gt=0)


class _MonthArgs(_OptionalCardArgs):
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")


class _WealthProductArgs(_StrictArgs):
    risk_level: Literal["R1", "R2", "R3", "R4", "R5"] | None = None
    status: Literal["active", "inactive"] | None = None
    suitable_only: bool = False


ReadExecutor = Callable[..., dict[str, Any]]


def _protected_read(name, operation):
    from security.tool_guard import guarded_tool_call
    result = guarded_tool_call(name, operation, read_only=True)
    if result.get('code') == 'tool_circuit_open':
        return {'success': False, 'error_code': 'tool_circuit_open',
                'message': '查询依赖暂时不可用，请稍后重试', 'data': result['data']}
    return result


@dataclass(frozen=True)
class ReadSpec:
    argument_model: type[BaseModel]
    execute: ReadExecutor
    result_data_type: type | tuple[type, ...]
    policy_action: str


def _cards(_args, principal=None):
    return get_cards(principal=principal)


def _monthly(args, principal=None):
    return get_monthly_spending(args.month, args.card_id, principal=principal)


def _transactions(args, principal=None):
    return get_transactions_by_month(args.month, args.card_id, principal=principal)


def _categories(args, principal=None):
    return get_category_spending(args.month, args.card_id, principal=principal)


def _subscriptions(args, principal=None):
    return get_active_subscriptions(args.card_id, principal=principal)


def _subscription_cost(args, principal=None):
    return get_monthly_subscription_cost(args.card_id, principal=principal)


def _recipients(_args, principal=None):
    return get_transfer_recipients(principal=principal)


def _risk_profile(_args, principal=None):
    return get_risk_profile(principal=principal)


def _wealth_products(args, principal=None):
    return get_wealth_products(
        args.risk_level, args.status, args.suitable_only, principal=principal
    )


def _wealth_holdings(_args, principal=None):
    return get_wealth_holdings(principal=principal)


READ_SPECS: dict[str, ReadSpec] = {
    "get_cards": ReadSpec(_EmptyArgs, _cards, list, "get_cards"),
    "get_monthly_spending": ReadSpec(
        _MonthArgs, _monthly, dict, "get_monthly_spending"
    ),
    "get_transactions_by_month": ReadSpec(
        _MonthArgs, _transactions, list, "get_transactions_by_month"
    ),
    "get_category_spending": ReadSpec(
        _MonthArgs, _categories, dict, "get_category_spending"
    ),
    "get_active_subscriptions": ReadSpec(
        _OptionalCardArgs, _subscriptions, list, "get_active_subscriptions"
    ),
    "get_monthly_subscription_cost": ReadSpec(
        _OptionalCardArgs, _subscription_cost, dict, "get_monthly_subscription_cost"
    ),
    "get_transfer_recipients": ReadSpec(
        _EmptyArgs, _recipients, list, "get_transfer_recipients"
    ),
    "get_risk_profile": ReadSpec(_EmptyArgs, _risk_profile, dict, "get_risk_profile"),
    "get_wealth_products": ReadSpec(
        _WealthProductArgs, _wealth_products, list, "get_wealth_products"
    ),
    "get_wealth_holdings": ReadSpec(
        _EmptyArgs, _wealth_holdings, list, "get_wealth_holdings"
    ),
}

# Kept independent from Agent Tool Registry on purpose. These names are only
# used to return a clear UI error when somebody tries a known write operation.
WRITE_OPERATION_NAMES = {
    "transfer_money",
    "set_daily_limit",
    "report_card_lost",
    "cancel_subscription",
    "buy_wealth_product",
    "redeem_wealth_product",
}

# 赛题例外：需要明确授权的只读查询
AUTHORIZATION_REQUIRED_READS = {
    "get_active_subscriptions",
    "get_monthly_subscription_cost",
}


def _service_result_to_tool_result(value) -> ToolResult:
    if not isinstance(value, dict):
        return ToolResult(
            status="error",
            error_code="invalid_service_result",
            message="业务服务返回了无效结果",
        )
    if value.get("success") is True:
        return ToolResult(
            status="success",
            data=value.get("data"),
            message=str(value.get("message", "查询成功")),
        )
    return ToolResult(
        status="error",
        data=value.get("data"),
        error_code=str(value.get("error_code") or "service_error"),
        message=str(value.get("message", "业务服务执行失败")),
    )


class ReadOnlyService:
    """Independent read catalog, shared by V1 ``/read`` and V2 ``/read``."""

    def __init__(self, policy: RiskEngine | None = None):
        self.policy = policy or RiskEngine()

    def tool_names(self):
        return sorted(READ_SPECS)

    def validate_arguments(self, name: str, arguments: dict):
        spec = READ_SPECS.get(name)
        if spec is None:
            return None, _failure(
                "", "not_found", "unknown_tool", f"没有名为 {name} 的只读查询。"
            )
        try:
            return spec.argument_model.model_validate(dict(arguments or {})), None
        except ValidationError:
            return None, _failure(
                "", "validation_failed", "invalid_arguments", "只读查询参数校验失败"
            )

    def policy_action(self, name: str) -> str | None:
        spec = READ_SPECS.get(name)
        return spec.policy_action if spec else None

    def policy_requires_authorization(self, name: str, principal: Principal) -> bool:
        if name not in AUTHORIZATION_REQUIRED_READS:
            return False
        try:
            decision = self.policy.evaluate(name, principal, {})
        except PolicyError:
            # 缺少安全事实时按最严格的解释处理：仍然要求显式授权。
            return True
        return decision.requires_confirmation

    def execute_validated(self, name: str, validated, principal: Principal | None):
        spec = READ_SPECS[name]
        return self._finish(
            name, spec, _service_result_to_tool_result(
                _protected_read(name, lambda: spec.execute(validated, principal=principal))
            ), arguments=validated.model_dump(mode='json')
        )

    def _finish(self, name: str, spec: ReadSpec, result: ToolResult, session_id="", arguments=None):
        if not result.success:
            detail = result.error_code or "service_error"
            return _failure(
                session_id, _stable_error_code(detail), detail, result.message
            )
        if not isinstance(result.data, spec.result_data_type):
            return _failure(
                session_id,
                "verification_failed",
                "verification_failed",
                "只读查询结果类型核验失败",
            )
        from read_models import verify_read
        if not verify_read(name, result.data, arguments) or contains_sensitive_result(result.data) or contains_sensitive_result(result.message):
            return _failure(
                session_id,
                "verification_failed",
                "verification_failed",
                "只读查询结果包含未脱敏或不允许暴露的敏感信息",
            )
        try:
            ui_result = build_ui_result(name, result)
        except ValueError:
            return _failure(
                session_id,
                "contract_payload_mismatch",
                "contract_payload_mismatch",
                f"只读查询结果不符合 UI Contract：{name}",
            )
        if ui_result is None:
            return _failure(
                session_id,
                "contract_payload_mismatch",
                "contract_payload_mismatch",
                f"只读查询没有对应的 result.kind：{name}",
            )
        return UiEnvelope(
            session_id=session_id,
            status="success",
            message=result.message or "查询成功",
            interaction=UiInteraction(),
            task=None,
            result=ui_result,
            error=None,
        )

    def run(self, session_id: str, body: ReadRequest, principal: Principal | None = None):
        name = body.tool.strip()
        from storage.config import pg02_enabled as enabled,PG02_SUPPORTED_TOOLS as SUPPORTED_TOOLS
        if enabled() and name not in SUPPORTED_TOOLS:
            return _failure(session_id, 'validation_failed', 'pg02_unsupported_business',
                            'PG02暂不支持该业务，请求未执行。')
        if name in WRITE_OPERATION_NAMES:
            return _failure(
                session_id,
                "validation_failed",
                "write_tool_not_allowed",
                f"{name} 是写操作，必须通过对话确认流程执行。",
            )

        spec = READ_SPECS.get(name)
        if spec is None:
            return _failure(
                session_id,
                "not_found",
                "unknown_tool",
                f"没有名为 {name} 的只读查询。",
            )

        try:
            validated = spec.argument_model.model_validate(dict(body.arguments or {}))
        except ValidationError:
            return _failure(
                session_id,
                "validation_failed",
                "invalid_arguments",
                "只读查询参数校验失败",
            )

        # 赛题例外：黄色只读查询必须先获得明确授权，确认后仍走独立只读目录。
        if principal is not None and name in AUTHORIZATION_REQUIRED_READS:
            try:
                decision = self.policy.evaluate(name, principal, {})
            except PolicyError:
                return _failure(
                    session_id,
                    "validation_failed",
                    "security_fact_unavailable",
                    "无法读取安全事实，已拒绝返回业务数据",
                )
            if decision.requires_confirmation and not getattr(
                body, "authorize_query", False
            ):
                return _authorization_required(
                    session_id, name, decision
                )

        try:
            result = _service_result_to_tool_result(
                _protected_read(name, lambda: spec.execute(validated, principal=principal))
            )
        except Exception:
            return _failure(
                session_id,
                "tool_execution_failed",
                "tool_execution_error",
                "只读查询执行异常",
            )
        return self._finish(name, spec, result, session_id, validated.model_dump(mode='json'))


def _authorization_required(session_id: str, name: str, decision) -> UiEnvelope:
    message = (
        "该查询属于只读但需要显式授权的类别（赛题策略例外）；"
        "请通过显式确认接口授权后再查询，未授权前不会返回业务数据。"
    )
    envelope = _failure(
        session_id,
        "validation_failed",
        "query_authorization_required",
        message,
    )
    if envelope.error is not None:
        envelope.error.detail_code = "query_authorization_required"
    return envelope


def _failure(session_id: str, code, detail_code, message: str) -> UiEnvelope:
    return UiEnvelope(
        session_id=session_id,
        status="failed",
        message=message,
        interaction=UiInteraction(),
        task=None,
        result=None,
        error=UiError(code=code, detail_code=detail_code, message=message),
    )


_service = None
_service_lock = RLock()


def get_read_only_service() -> ReadOnlyService:
    global _service
    with _service_lock:
        if _service is None:
            _service = ReadOnlyService()
        return _service
