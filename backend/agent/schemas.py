"""Agent 决策、Tool 参数与统一结果的 Pydantic Schema。"""

from datetime import datetime
from decimal import Decimal
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyArguments(StrictModel):
    pass


class OptionalCardArguments(StrictModel):
    card_id: int | None = Field(default=None, gt=0, description="可选银行卡编号")


class MonthArguments(OptionalCardArguments):
    month: str = Field(
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        description="查询月份，必须是 YYYY-MM；相对日期使用程序提供的当前日期换算",
    )


class CancelSubscriptionArguments(StrictModel):
    subscription: str = Field(
        min_length=1,
        max_length=100,
        description="用户明确指定的订阅或商户名称，例如腾讯视频",
    )


class ReportCardLostArguments(StrictModel):
    card_id: int = Field(gt=0, description="要挂失的银行卡编号")


class SetDailyLimitArguments(StrictModel):
    card_id: int = Field(gt=0, description="要修改每日交易限额的银行卡编号")
    daily_limit: Decimal = Field(
        gt=0,
        max_digits=9,
        decimal_places=2,
        description="新每日交易限额，人民币元，最多两位小数",
    )

    @field_validator("daily_limit", mode="before")
    @classmethod
    def reject_boolean_limit(cls, value):
        if isinstance(value, bool):
            raise ValueError("每日限额不能是布尔值")
        return value


class CancelSubscriptionExecutionArguments(StrictModel):
    subscription_id: int = Field(gt=0)


class ReportCardLostExecutionArguments(StrictModel):
    card_id: int = Field(gt=0)


class SetDailyLimitExecutionArguments(StrictModel):
    card_id: int = Field(gt=0)
    new_limit_cents: int = Field(gt=0)


class WealthProductArguments(StrictModel):
    risk_level: Literal["R1", "R2", "R3", "R4", "R5"] | None = Field(
        default=None, description="可选的模拟理财产品风险等级筛选"
    )
    status: Literal["active", "inactive"] | None = Field(
        default=None, description="可选的产品状态筛选"
    )
    suitable_only: bool = Field(
        default=False,
        description="用户明确询问适合其风险等级的产品时设为true；适配由Python计算",
    )


class BuyWealthProductArguments(StrictModel):
    product_id: int = Field(gt=0, description="模拟理财产品编号")
    card_id: int = Field(gt=0, description="用于支付的银行卡编号")
    amount: Decimal = Field(
        gt=0,
        max_digits=10,
        decimal_places=2,
        description="模拟购买金额，人民币元，最多两位小数",
    )

    @field_validator("amount", mode="before")
    @classmethod
    def reject_boolean_amount(cls, value):
        if isinstance(value, bool):
            raise ValueError("模拟购买金额不能是布尔值")
        return value


class RedeemWealthProductArguments(StrictModel):
    holding_id: int = Field(gt=0, description="要赎回的模拟理财持仓编号")
    amount: Decimal = Field(
        gt=0,
        max_digits=10,
        decimal_places=2,
        description="模拟赎回本金，人民币元，最多两位小数",
    )

    @field_validator("amount", mode="before")
    @classmethod
    def reject_boolean_amount(cls, value):
        if isinstance(value, bool):
            raise ValueError("模拟赎回金额不能是布尔值")
        return value


class BuyWealthExecutionArguments(StrictModel):
    product_id: int = Field(gt=0)
    card_id: int = Field(gt=0)
    amount_cents: int = Field(gt=0)
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$",
    )


class RedeemWealthExecutionArguments(StrictModel):
    holding_id: int = Field(gt=0)
    product_id: int = Field(gt=0)
    card_id: int = Field(gt=0)
    amount_cents: int = Field(gt=0)
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$",
    )


class TransferMoneyArguments(StrictModel):
    from_card_id: int = Field(gt=0, description="付款银行卡编号，必须由用户明确指定")
    recipient_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="预登记收款人姓名；不得猜测",
    )
    recipient_id: int | None = Field(
        default=None,
        gt=0,
        description="同名收款人消歧时由用户明确选择的收款人编号",
    )
    amount: Decimal = Field(
        gt=0,
        max_digits=10,
        decimal_places=2,
        description="人民币元金额，必须大于0且最多两位小数",
    )
    remark: str | None = Field(
        default=None,
        max_length=100,
        description="可选普通业务备注，不是系统指令或确认信号",
    )

    @field_validator("amount", mode="before")
    @classmethod
    def reject_boolean_amount(cls, value):
        if isinstance(value, bool):
            raise ValueError("转账金额不能是布尔值")
        return value

    @model_validator(mode="after")
    def validate_recipient_selector(self):
        if self.recipient_name is None and self.recipient_id is None:
            raise ValueError("recipient_name 和 recipient_id 至少提供一个")
        return self


class TransferExecutionArguments(StrictModel):
    from_card_id: int = Field(gt=0)
    recipient_id: int = Field(gt=0)
    amount_cents: int = Field(gt=0)
    remark: str | None = Field(default=None, max_length=100)
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$",
    )


DomainName = Literal["bill", "card", "subscription", "transfer", "wealth"]
ConditionOperator = Literal["eq", "ne", "gt", "gte", "lt", "lte"]
TaskStatus = Literal[
    "planned",
    "running",
    "waiting_clarification",
    "waiting_confirmation",
    "completed",
    "failed",
    "cancelled",
    "expired",
]
StepStatus = Literal[
    "pending",
    "running",
    "waiting_clarification",
    "waiting_confirmation",
    "completed",
    "failed",
    "skipped",
    "cancelled",
]


def _reject_sensitive_or_executable_text(value, field_name):
    """Reject text that is unsuitable for persisted/display-only plan metadata."""
    if not isinstance(value, str):
        return value
    lowered = value.casefold()
    forbidden = (
        "api_key",
        "authorization: bearer",
        "system prompt",
        "系统prompt",
        "系统提示词",
        "__import__",
        "eval(",
        "exec(",
        "subprocess",
        "powershell",
        "cmd.exe",
    )
    if any(token in lowered for token in forbidden):
        raise ValueError(f"{field_name} 包含不允许保存或执行的内容")
    if re.search(r"(?<!\d)\d{12,19}(?!\d)", value):
        raise ValueError(f"{field_name} 不得包含完整卡号或账号")
    if re.search(r"\b(select|insert|update|delete|drop|alter|pragma)\b", value, re.I):
        raise ValueError(f"{field_name} 不得包含 SQL")
    if re.search(r"\b(?:sk|api)[-_][A-Za-z0-9]{12,}\b", value, re.I):
        raise ValueError(f"{field_name} 不得包含凭据")
    if re.search(
        r'\bBearer\s+\S+|\bs[12]_[A-Za-z0-9_-]{8,}\b|'
        r'(?:\botp\b|验证码)\s*[:：=]?\s*\d{4,8}|确认(?:转账|操作)\s*\d{6}',
        value, re.I,
    ):
        raise ValueError(f"{field_name} 不得包含会话凭据、验证码或确认短语")
    return value


class FinancialIntentContract(StrictModel):
    contract_version: Literal["1.0"]
    goal: str = Field(min_length=1, max_length=300)
    domains: list[DomainName] = Field(min_length=1, max_length=5)
    requested_tools: list[str] = Field(min_length=1, max_length=8)
    constraints: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("goal")
    @classmethod
    def validate_goal(cls, value):
        return _reject_sensitive_or_executable_text(value.strip(), "goal")

    @field_validator("constraints")
    @classmethod
    def validate_constraints(cls, values):
        cleaned = []
        for value in values:
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError("constraint 必须是长度不超过200的非空文本")
            cleaned.append(
                _reject_sensitive_or_executable_text(value.strip(), "constraint")
            )
        return cleaned

    @model_validator(mode="after")
    def validate_unique_lists(self):
        if len(self.domains) != len(set(self.domains)):
            raise ValueError("domains 不得重复")
        if len(self.requested_tools) != len(set(self.requested_tools)):
            raise ValueError("requested_tools 不得重复")
        return self


class StepCondition(StrictModel):
    source_step_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
    field_path: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*){0,3}$",
    )
    operator: ConditionOperator
    value: str | int | float | bool

    @field_validator("field_path")
    @classmethod
    def reject_private_path(cls, value):
        if any(part.startswith("_") or "__" in part for part in value.split(".")):
            raise ValueError("条件字段路径不得访问私有字段")
        return value

    @field_validator("value", mode="before")
    @classmethod
    def validate_scalar(cls, value):
        if value is None or isinstance(value, (dict, list, tuple, set)):
            raise ValueError("条件值只能是字符串、整数、有限小数或布尔值")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("条件小数必须是有限值")
        if isinstance(value, str) and len(value) > 200:
            raise ValueError("条件字符串过长")
        return value


class PlannedStep(StrictModel):
    step_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
    purpose: str = Field(min_length=1, max_length=160)
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    condition: StepCondition | None = None

    @field_validator("purpose")
    @classmethod
    def validate_purpose(cls, value):
        return _reject_sensitive_or_executable_text(value.strip(), "purpose")

    @model_validator(mode="after")
    def validate_dependencies(self):
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("depends_on 不得重复")
        if self.step_id in self.depends_on:
            raise ValueError("步骤不得依赖自身")
        return self


class TaskPlanProposal(StrictModel):
    contract: FinancialIntentContract
    steps: list[PlannedStep] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_unique_step_ids(self):
        ids = [step.step_id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step_id 必须唯一")
        return self


class AgentDecision(StrictModel):
    """LLM 的已验证决策；自由文本不会被当作 Tool 参数。"""

    action: Literal[
        "tool_call", "task_plan", "clarification", "response", "modify_action"
    ]
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    message: str | None = None
    clarification_scope: Literal["single_tool", "workflow_plan"] | None = None
    plan_error_code: str | None = Field(
        default=None, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$"
    )
    response_type: Literal[
        "greeting",
        "help",
        "capabilities",
        "out_of_scope",
        "general_conversation",
    ] | None = None
    missing_fields: list[str] = Field(default_factory=list)
    task_plan: TaskPlanProposal | None = None
    modification: "ActionModificationControl | None" = None

    @model_validator(mode="after")
    def validate_action_fields(self):
        if self.action == "tool_call" and not self.tool_name:
            raise ValueError("tool_call 必须包含 tool_name")
        if self.action == "task_plan" and self.task_plan is None:
            raise ValueError("task_plan 必须包含已验证的计划")
        if self.action == "modify_action" and self.modification is None:
            raise ValueError("modify_action 必须包含结构化修改提案")
        if self.action in {"clarification", "response"} and not self.message:
            raise ValueError(f"{self.action} 必须包含 message")
        if self.action == "response" and self.response_type is None:
            raise ValueError("response 必须包含 response_type")
        return self


class ObjectReference(StrictModel):
    """LLM 只描述指代；Python 根据当前用户/会话的可信摘要解析。"""

    field: Literal["recipient", "from_card"]
    reference: Literal["selected", "previous", "other"]


class SimpleConstraint(StrictModel):
    """V0.8.1 仅支持可确定判定的金额上限。"""

    field: Literal["amount"]
    operator: Literal["lte"]
    value: Decimal = Field(gt=0, max_digits=10, decimal_places=2)

    @field_validator("value", mode="before")
    @classmethod
    def reject_boolean_value(cls, value):
        if isinstance(value, bool):
            raise ValueError("约束值不能是布尔值")
        return value


class ActionModificationControl(StrictModel):
    """模型提出修改目标；字段白名单、归属、依赖和授权均由 Python 校验。"""

    target: Literal[
        "pending_action", "workflow_step", "new_action", "completed_action"
    ]
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    step_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$"
    )
    changes: dict[str, Any] = Field(default_factory=dict)
    references: list[ObjectReference] = Field(default_factory=list, max_length=3)
    constraints: list[SimpleConstraint] = Field(
        default_factory=list,
        max_length=4,
        description=(
            "仅用于新增或收紧金额上限；Python 会与已有上限取更紧的值。"
            "省略该字段或给出空列表都不会删除已有上限；不要用它放宽或替换旧上限。"
        ),
    )

    @model_validator(mode="after")
    def validate_shape(self):
        if self.target == "workflow_step" and self.step_id is None:
            # 可以由 Python 在只有一个尚未执行候选时唯一解析。
            pass
        if not self.changes and not self.references and not self.constraints:
            raise ValueError("修改提案必须包含字段修改、对象引用或约束")
        protected = {
            "risk_level", "permission_level", "principal", "principal_id",
            "user_id", "authorization", "authorization_id", "confirmed",
            "confirmation_phrase", "challenge_id", "result", "status",
            "idempotency_key", "amount_cents", "decision_id", "policy_version",
        }
        if protected.intersection(self.changes):
            raise ValueError("修改提案包含后端安全字段")
        return self


class ConstraintDraft(StrictModel):
    """已废止旧待办/步骤后仍需继承的公开参数与金额上限约束。

    这不是授权，也不能用于执行；它只保证在澄清、参数非法或约束冲突之后，
    后续补参与重新预览不会丢失已经生效的约束归属。
    """

    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    constraints: list[SimpleConstraint] = Field(default_factory=list, max_length=4)
    step_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$"
    )
    task_id: str | None = None
    reason: str = Field(max_length=64)


class ToolResult(StrictModel):
    status: Literal["success", "error"]
    data: Any = None
    error_code: str | None = None
    message: str

    @property
    def success(self):
        return self.status == "success"

    @model_validator(mode="after")
    def validate_error_code(self):
        if self.status == "error" and not self.error_code:
            raise ValueError("失败结果必须包含 error_code")
        if self.status == "success" and self.error_code is not None:
            raise ValueError("成功结果不能包含 error_code")
        return self


class TaskStepState(StrictModel):
    step_id: str
    tool_name: str
    purpose: str
    arguments: dict[str, Any]
    depends_on: list[str]
    # V0.8.1：步骤级金额上限约束属于可信状态，即使该步骤尚未创建 PendingAction
    # 也必须能保存，并在轮到该步骤时由 Python 重新校验。
    constraints: list[SimpleConstraint] = Field(default_factory=list, max_length=4)
    condition: StepCondition | None = None
    condition_status: Literal[
        "not_applicable", "not_evaluated", "true", "false", "error"
    ] = "not_applicable"
    condition_result: bool | None = None
    condition_error_code: str | None = None
    condition_source_step_id: str | None = None
    condition_field_path: str | None = None
    condition_operator: ConditionOperator | None = None
    status: StepStatus = "pending"
    result: ToolResult | None = None
    verification_status: Literal["not_run", "passed", "failed"] = "not_run"
    error_code: str | None = None
    message: str | None = None

    @model_validator(mode="after")
    def validate_condition_evidence(self):
        if self.condition is None:
            if self.condition_status != "not_applicable" or any(
                value is not None
                for value in (
                    self.condition_result,
                    self.condition_error_code,
                    self.condition_source_step_id,
                    self.condition_field_path,
                    self.condition_operator,
                )
            ):
                raise ValueError("无条件步骤不能包含条件执行证据")
            return self
        if self.condition_status == "not_applicable":
            raise ValueError("有条件步骤必须初始化条件执行状态")
        if self.condition_source_step_id != self.condition.source_step_id:
            raise ValueError("条件来源证据与计划不一致")
        if self.condition_field_path != self.condition.field_path:
            raise ValueError("条件字段证据与计划不一致")
        if self.condition_operator != self.condition.operator:
            raise ValueError("条件操作符证据与计划不一致")
        if self.condition_status == "true" and self.condition_result is not True:
            raise ValueError("条件 True 证据不一致")
        if self.condition_status == "false" and self.condition_result is not False:
            raise ValueError("条件 False 证据不一致")
        if self.condition_status == "error" and not self.condition_error_code:
            raise ValueError("条件错误必须包含稳定错误代码")
        return self


class TaskState(StrictModel):
    task_id: str
    contract: FinancialIntentContract
    steps: list[TaskStepState]
    status: TaskStatus = "planned"
    current_step_id: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    failure_reason: str | None = None

    @model_validator(mode="after")
    def validate_runtime_invariants(self):
        running = [step for step in self.steps if step.status == "running"]
        waiting = [
            step for step in self.steps if step.status == "waiting_confirmation"
        ]
        clarifying = [
            step for step in self.steps if step.status == "waiting_clarification"
        ]
        if len(running) > 1 or len(waiting) > 1 or len(clarifying) > 1:
            raise ValueError("同一任务最多只能有一个运行、等待澄清或等待确认步骤")
        if sum(bool(items) for items in (running, clarifying, waiting)) > 1:
            raise ValueError("运行、等待澄清和等待确认状态不能同时存在")
        if self.status == "waiting_clarification" and len(clarifying) != 1:
            raise ValueError("等待澄清任务必须且只能有一个等待澄清步骤")
        if self.status == "waiting_confirmation" and len(waiting) != 1:
            raise ValueError("等待确认任务必须且只能有一个等待确认步骤")
        if self.status == "completed" and any(
            step.status in {
                "pending",
                "running",
                "waiting_clarification",
                "waiting_confirmation",
            }
            for step in self.steps
        ):
            raise ValueError("已完成任务不能包含未结束步骤")
        return self


class PendingClarification(StrictModel):
    kind: Literal[
        "single_tool", "workflow_plan", "workflow_execution"
    ] = "single_tool"
    tool_name: str | None = None
    known_arguments: dict[str, Any] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    requires_full_replan: bool = False
    task_id: str | None = None
    step_id: str | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_scope(self):
        if len(self.missing_fields) != len(set(self.missing_fields)):
            raise ValueError("missing_fields 不得重复")
        if self.kind == "workflow_plan":
            if not self.tool_name or not self.missing_fields:
                raise ValueError("Workflow 规划澄清必须指定工具和缺失字段")
            if not self.requires_full_replan:
                raise ValueError("Workflow 规划澄清必须重新生成完整计划")
            if self.task_id is not None or self.step_id is not None:
                raise ValueError("Workflow 规划澄清不得伪装成已创建任务")
        elif self.kind == "workflow_execution":
            if not all((self.tool_name, self.task_id, self.step_id, self.missing_fields)):
                raise ValueError("Workflow 执行澄清必须绑定任务、步骤、工具和缺失字段")
            if self.requires_full_replan:
                raise ValueError("Workflow 执行澄清不得重新生成无关任务")
            if self.created_at is None or self.expires_at is None:
                raise ValueError("Workflow 执行澄清必须包含有效期")
            if (
                self.created_at.tzinfo is None
                or self.created_at.utcoffset() is None
                or self.expires_at.tzinfo is None
                or self.expires_at.utcoffset() is None
                or self.expires_at <= self.created_at
            ):
                raise ValueError("Workflow 执行澄清有效期不合法")
        elif any((self.task_id, self.step_id, self.requires_full_replan)):
            raise ValueError("单 Tool 澄清不得包含 Workflow 状态")
        return self


class TaskPreviewBinding(StrictModel):
    """Server-only identity of an issued preview; never part of an HTTP body."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    task_id: str
    step_id: str
    operation_id: str
    issued_revision: int = Field(ge=1)
    generation: int = Field(ge=1)
    action: str
    execution_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    business_key: str | None = None


class PendingAction(StrictModel):
    """仅由 Router/统一安全入口创建；不接受 LLM 提供的授权字段。

    V0.7.0 起执行证据是 ``authorization_id``（服务器端一次性授权行），
    ``confirmed`` 布尔值不再作为充分授权依据。
    """

    tool_name: str
    requested_arguments: dict[str, Any]
    execution_arguments: dict[str, Any]
    confirmation_message: str
    confirmation_phrase: str | None = Field(
        default=None, pattern=r"^(?:确认转账|确认操作) \d{6}$"
    )
    created_at: datetime
    expires_at: datetime
    action: str | None = None
    permission_level: Literal["green", "yellow", "red"] | None = None
    decision_id: str | None = None
    policy_version: str | None = None
    action_digest: str | None = None
    requires_strong_auth: bool = False
    requires_confirmation: bool = True
    execution_permitted: bool = True
    rejection_code: str | None = None
    # V2 keeps the actual one-time grant reference server-side.  It is never
    # exposed in an Agent tool schema or UI response.
    authorization_id: str | None = None
    constraints: list[SimpleConstraint] = Field(default_factory=list, max_length=4)
    revision: int = Field(default=1, ge=1)
    lifecycle_binding: TaskPreviewBinding | None = None

    @model_validator(mode="after")
    def validate_lifetime(self):
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at 必须包含时区")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("expires_at 必须包含时区")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at 必须晚于 created_at")
        if self.tool_name == "transfer_money" and self.confirmation_phrase is None:
            raise ValueError("转账待确认操作必须包含动态确认短语")
        return self


class DispatchResult(StrictModel):
    result: ToolResult
    pending_action: PendingAction | None = None


class AgentReply(StrictModel):
    message: str
    action: Literal[
        "response",
        "clarification",
        "confirmation_required",
        "tool_result",
        "task_progress",
        "task_result",
        "error",
    ]
    tool_name: str | None = None
    tool_result: ToolResult | None = None
    requires_confirmation: bool = False
    clarification_scope: Literal["single_tool", "workflow_plan"] | None = None
    plan_error_code: str | None = Field(
        default=None, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$"
    )


class ClarificationControl(StrictModel):
    """结构化澄清控制；作用域必须由字段显式声明，不能靠中文文本猜测。

    `scope` 没有默认值：模型必须显式声明澄清作用域。旧式无作用域请求不会
    静默退回单 Tool 澄清，而是直接失败，因此不会悄悄丢失多目标 Workflow。
    """

    message: str = Field(description="向用户提出的一个简短、明确的问题")
    scope: Literal["single_tool", "workflow_plan"] = Field(
        description=(
            "必须显式声明："
            "single_tool：只有一个业务目标、只是缺少该工具的必填公开参数；"
            "workflow_plan：原请求包含多个需要顺序联动的目标、条件任务或跨场景任务，"
            "缺参后仍必须重新生成完整 create_task_plan"
        ),
    )
    tool_name: str | None = Field(
        default=None, description="预计在参数补齐后调用的银行工具名"
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="已经可靠获得的参数"
    )
    missing_fields: list[str] = Field(
        default_factory=list, description="仍缺少的关键参数名"
    )


class ResponseControl(StrictModel):
    response_type: Literal[
        "greeting",
        "help",
        "capabilities",
        "out_of_scope",
        "general_conversation",
    ] = Field(description="普通回复的安全类别；银行事实查询不属于普通回复")
    message: str = Field(description="普通回复建议；程序会按安全类别生成最终文案")


