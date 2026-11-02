"""V0.8.1 金额上限约束的确定性合并与校验（单操作与 Workflow 共用）。

规则（与 docs/V0.8.1_ACCEPTANCE.md 一致）：

1. 已有约束属于当前操作/步骤的可信状态，不依赖模型每轮重复输出。
2. 修改金额、卡片或收款人时默认继承已有约束。
3. 本轮没有提供 ``constraints``（None/省略）或提供空列表，都不得清除已有约束。
4. 新增或收紧约束时由 Python 合并：同一 ``amount <= X`` 取更小（更紧）的上限。
5. 放宽（提出更大的上限）无法由当前协议可靠表达，本轮澄清并保留原约束。
6. 当前只支持 ``amount <= X``；其他字段/操作符或没有金额字段的业务直接拒绝。
7. 金额比较全部使用 ``Decimal``，不使用浮点近似。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, NamedTuple

from pydantic import ValidationError

from .schemas import SimpleConstraint

SUPPORTED_FIELD = "amount"
SUPPORTED_OPERATOR = "lte"

# 与既有回复保持一致的稳定文案/错误码（供 Agent 与 Workflow 共用）。
UNSUPPORTED_MESSAGE = "该约束当前不受支持，操作未继续。"
AMBIGUOUS_MESSAGE = (
    "约束提案同时给出多个不同的金额上限，无法确定用户意图；"
    "已保留原约束，请一次只给出一个上限。"
)
RELAXATION_MESSAGE = (
    "当前操作已有“金额不超过 {existing} 元”的约束，本系统不支持在修改提案中"
    "放宽或删除该约束；已保留原约束，未改变金额上限。"
    "如需按更高的金额办理，请明确发起一笔新的独立操作。"
)
CONFLICT_MESSAGE = "当前金额与“金额不超过”约束冲突，请修改金额或约束。"
MISSING_AMOUNT_MESSAGE = "请先提供金额，才能判断金额上限约束。"
NOT_APPLICABLE_MESSAGE = "当前业务不支持金额上限约束，操作未继续。"
UNDECIDABLE_MESSAGE = "金额约束无法安全判定，操作未执行。"


class MergeOutcome(NamedTuple):
    """合并结果：``constraints`` 始终是可继续使用（绝不清空）的约束集合。"""

    constraints: list[SimpleConstraint]
    error_code: str | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_code is None


def tool_supports_amount(argument_model) -> bool:
    """只有公开参数里存在 ``amount`` 的业务才允许绑定金额上限。"""
    return SUPPORTED_FIELD in getattr(argument_model, "model_fields", {})


def coerce_constraints(constraints: Iterable[Any] | None) -> list[SimpleConstraint]:
    """把模型提案中的约束统一成已校验的 ``SimpleConstraint``。"""
    result: list[SimpleConstraint] = []
    for item in constraints or ():
        if isinstance(item, SimpleConstraint):
            result.append(item)
        else:
            result.append(SimpleConstraint.model_validate(item))
    return result


def _safe_existing(constraints: Iterable[Any] | None) -> list[SimpleConstraint]:
    try:
        return coerce_constraints(constraints)
    except (ValidationError, TypeError, ValueError):
        return []


def _caps(
    constraints: Iterable[SimpleConstraint],
) -> list[Decimal]:
    values = []
    for item in constraints:
        if item.field != SUPPORTED_FIELD or item.operator != SUPPORTED_OPERATOR:
            raise _UnsupportedConstraint()
        values.append(item.value)
    return values


class _UnsupportedConstraint(Exception):
    pass


def _single(cap: Decimal | None) -> list[SimpleConstraint]:
    if cap is None:
        return []
    return [SimpleConstraint(field=SUPPORTED_FIELD, operator=SUPPORTED_OPERATOR, value=cap)]


def effective_cap(constraints: Iterable[Any] | None) -> Decimal | None:
    """当前生效的金额上限；多个上限取最紧的那个。"""
    try:
        caps = _caps(coerce_constraints(constraints))
    except (_UnsupportedConstraint, ValidationError, TypeError, ValueError):
        return None
    return min(caps) if caps else None


def merge_amount_constraints(
    existing: Iterable[Any] | None,
    proposed: Iterable[Any] | None,
) -> MergeOutcome:
    """继承 ``existing`` 并合并本轮 ``proposed``；任何情况下都不静默清空。"""
    try:
        existing_list = coerce_constraints(existing)
    except (ValidationError, TypeError, ValueError):
        return MergeOutcome([], "constraint_not_supported", UNSUPPORTED_MESSAGE)
    try:
        proposed_list = coerce_constraints(proposed)
        existing_caps = _caps(existing_list)
        proposed_caps = _caps(proposed_list)
    except (_UnsupportedConstraint, ValidationError, TypeError, ValueError):
        return MergeOutcome(existing_list, "constraint_not_supported", UNSUPPORTED_MESSAGE)

    existing_cap = min(existing_caps) if existing_caps else None
    if not proposed_caps:
        # 字段省略与空列表都不是“删除约束”的可靠表达。
        return MergeOutcome(_single(existing_cap))
    if len(set(proposed_caps)) > 1:
        return MergeOutcome(
            _single(existing_cap), "constraint_ambiguous", AMBIGUOUS_MESSAGE
        )
    proposed_cap = proposed_caps[0]
    if existing_cap is not None and proposed_cap > existing_cap:
        return MergeOutcome(
            _single(existing_cap),
            "constraint_relaxation_not_supported",
            RELAXATION_MESSAGE.format(existing=_format_decimal(existing_cap)),
        )
    merged_cap = proposed_cap if existing_cap is None else min(existing_cap, proposed_cap)
    return MergeOutcome(_single(merged_cap))


def check_amount(
    arguments: dict[str, Any] | None,
    constraints: Iterable[Any] | None,
) -> tuple[str, str] | None:
    """金额上限校验；返回 ``(error_code, message)`` 或 ``None``。"""
    try:
        caps = _caps(coerce_constraints(constraints))
    except (_UnsupportedConstraint, ValidationError, TypeError, ValueError):
        return ("constraint_not_supported", UNSUPPORTED_MESSAGE)
    if not caps:
        return None
    raw = (arguments or {}).get(SUPPORTED_FIELD)
    if raw is None:
        return ("constraint_not_supported", MISSING_AMOUNT_MESSAGE)
    if isinstance(raw, bool):
        return ("constraint_not_supported", UNDECIDABLE_MESSAGE)
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, ArithmeticError, TypeError, ValueError):
        return ("constraint_not_supported", UNDECIDABLE_MESSAGE)
    if not amount.is_finite():
        return ("constraint_not_supported", UNDECIDABLE_MESSAGE)
    if amount > min(caps):
        return ("constraint_conflict", CONFLICT_MESSAGE)
    return None


def describe(constraints: Iterable[Any] | None) -> list[dict[str, Any]]:
    """精简上下文使用的可序列化形式。"""
    return [item.model_dump(mode="json") for item in coerce_constraints(constraints)]


def _format_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


__all__ = [
    "AMBIGUOUS_MESSAGE",
    "CONFLICT_MESSAGE",
    "MISSING_AMOUNT_MESSAGE",
    "MergeOutcome",
    "NOT_APPLICABLE_MESSAGE",
    "RELAXATION_MESSAGE",
    "SUPPORTED_FIELD",
    "SUPPORTED_OPERATOR",
    "UNSUPPORTED_MESSAGE",
    "check_amount",
    "coerce_constraints",
    "describe",
    "effective_cap",
    "merge_amount_constraints",
    "tool_supports_amount",
]
