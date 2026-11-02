from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictUiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSessionResponse(StrictUiModel):
    session_id: str


class ChatRequest(StrictUiModel):
    session_id: str | None = None
    message: str = Field(min_length=1, max_length=2000)
    intent_mode: Literal['analysis','action'] = 'analysis'


class ConfirmRequest(StrictUiModel):
    text: str = Field(min_length=1, max_length=200)


UiStatus = Literal[
    "ready",
    "need_info",
    "need_confirmation",
    "executing",
    "success",
    "cancelled",
    "expired",
    "failed",
]

UiEnvelopeErrorCode = Literal[
    "interaction_expired",
    "not_found",
    "risk_rejected",
    "insufficient_funds",
    "limit_exceeded",
    "validation_failed",
    "verification_failed",
    "duplicate_operation",
    "tool_execution_failed",
    "service_unavailable",
    "operation_failed",
    "agent_error",
    "contract_payload_mismatch",
]

ApiProblemCode = Literal[
    "invalid_request",
    "session_not_found",
    "no_pending_interaction",
    "service_unavailable",
]

UiTaskStatus = Literal[
    "planned",
    "running",
    "waiting_clarification",
    "waiting_confirmation",
    "completed",
    "failed",
    "cancelled",
    "expired",
]

UiStepStatus = Literal[
    "pending",
    "running",
    "waiting_clarification",
    "waiting_confirmation",
    "completed",
    "failed",
    "skipped",
    "cancelled",
]


class UiInteraction(StrictUiModel):
    type: Literal["none", "clarification", "confirmation"] = "none"
    missing_fields: list[str] = Field(default_factory=list)
    confirmation_mode: Literal["none", "explicit", "dynamic_phrase"] = "none"
    confirmation_phrase: str | None = None
    expires_at: str | None = None


class UiTaskStep(StrictUiModel):
    step_id: str
    title: str
    tool_name: str
    """诊断字段：只用于日志/排查，UI 不得据此分支渲染（请使用 title）。"""
    status: UiStepStatus
    error_code: str | None = None


class UiTask(StrictUiModel):
    task_id: str
    status: UiTaskStatus
    current_step_id: str | None = None
    steps: list[UiTaskStep]


class ReadRequest(StrictUiModel):
    """确定性只读查询：由 UI 直接指定白名单内的只读工具，不经过 LLM。

    V0.7.0 增加 ``authorize_query``：赛题策略例外中只读但需要明确授权的查询
    （订阅查询、订阅费用统计）必须显式授权后才返回业务数据；未授权时返回既有
    错误信封与稳定 detail_code，不泄露业务数据。
    """

    tool: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)
    authorize_query: bool = False


class HealthResponse(StrictUiModel):
    status: Literal["ok"] = "ok"
    backend_version: str
    contract_version: Literal["1.1"] = "1.1"
    llm_configured: bool


class Capability(StrictUiModel):
    id: Literal["bill", "card", "subscription", "transfer", "wealth", "workflow"]
    name: str
    enabled: bool


class CapabilitiesResponse(StrictUiModel):
    contract_version: Literal["1.1"] = "1.1"
    capabilities: list[Capability]


class UiError(StrictUiModel):
    code: UiEnvelopeErrorCode
    message: str
    detail_code: str | None = None


class ApiProblem(StrictUiModel):
    contract_version: Literal["1.1"] = "1.1"
    code: ApiProblemCode
    message: str


# ---------- Stable UI payloads ----------

class MonthlySpendingData(StrictUiModel):
    month: str
    transaction_count: int
    total_spending: float
    card_specified: bool
    card_id: int | None


class CategoryStat(StrictUiModel):
    amount: float
    percentage: float


class CategorySpendingData(StrictUiModel):
    month: str
    card_id: int | None
    total_spending: float
    categories: dict[str, CategoryStat]


class TransactionItem(StrictUiModel):
    transaction_id: int
    card_id: int
    card_last_four: str
    date: str
    merchant: str
    category: str
    amount: float
    direction: Literal["income", "expense"]
    event_at: str | None = None
    time_source: str = 'date_only'
    transaction_type: str = 'unknown'
    status: str = 'success'


class CardItem(StrictUiModel):
    card_id: int
    bank_name: str
    masked_card_number: str
    balance: float
    status: Literal["normal", "lost", "frozen"]
    daily_limit: float
    overseas_enabled: bool
    online_payment_enabled: bool


class SubscriptionItem(StrictUiModel):
    subscription_id: int
    name: str
    amount: float
    cycle: str
    next_charge_date: str
    card_id: int
    card_last_four: str
    status: Literal["active", "cancelled"]


class SubscriptionCostData(StrictUiModel):
    active_count: int
    monthly_cost: float
    annual_cost: float
    annual_cost_cents: int | None = None
    cost_basis: str | None = None
    monthly_rounding: str | None = None
    complete: bool = True
    unknown_cycle_count: int = 0
    subscriptions: list[SubscriptionItem]


class RecipientItem(StrictUiModel):
    recipient_id: int
    name: str
    bank_name: str
    masked_account: str
    trusted: bool


class RiskProfileData(StrictUiModel):
    risk_level: Literal["R1", "R2", "R3", "R4", "R5"]
    risk_name: str
    updated_at: str
    notice: str


class WealthProductItem(StrictUiModel):
    product_id: int
    name: str
    risk_level: Literal["R1", "R2", "R3", "R4", "R5"]
    min_purchase: float
    status: Literal["active", "inactive"]
    reference_annual_rate: float
    duration_days: int
    notice: str


class WealthHoldingItem(StrictUiModel):
    holding_id: int
    product_id: int
    product_name: str
    card_id: int
    principal: float
    status: Literal["active", "redeemed"]
    purchased_at: str
    notice: str


class CancelSubscriptionPreviewData(SubscriptionItem):
    requires_confirmation: Literal[True]


class CardLostPreviewData(StrictUiModel):
    card_id: int
    bank_name: str
    masked_card_number: str
    requires_confirmation: Literal[True]


class CardLostCompletedData(StrictUiModel):
    card_id: int
    masked_card_number: str
    status: Literal["lost"]


class DailyLimitPreviewData(StrictUiModel):
    card_id: int
    masked_card_number: str
    old_limit: float
    new_limit: float
    requires_confirmation: Literal[True]


class DailyLimitCompletedData(StrictUiModel):
    card_id: int
    masked_card_number: str
    old_limit: float
    new_limit: float


class TransferPreviewData(StrictUiModel):
    from_card_id: int
    from_bank_name: str
    from_masked_card_number: str
    recipient_id: int
    recipient_name: str
    recipient_bank_name: str
    recipient_masked_account: str
    amount: float
    amount_cents: int
    remark: str | None
    risk_level: Literal["low", "medium", "high"]
    requires_confirmation: Literal[True]


from execution.result_models import TransferCompletedData


class WealthPurchasePreviewData(StrictUiModel):
    product_id: int
    product_name: str
    product_risk_level: Literal["R1", "R2", "R3", "R4", "R5"]
    user_risk_level: Literal["R1", "R2", "R3", "R4", "R5"]
    amount: float
    card_id: int
    masked_card_number: str
    reference_annual_rate: float
    duration_days: int
    requires_confirmation: Literal[True]
    notice: str


class WealthRedemptionPreviewData(StrictUiModel):
    holding_id: int
    product_id: int
    product_name: str
    card_id: int
    masked_card_number: str
    current_principal: float
    redemption_amount: float
    remaining_principal: float
    requires_confirmation: Literal[True]
    notice: str


from execution.result_models import WealthOrderCompletedData


class UiResultBase(StrictUiModel):
    status: Literal["success"] = "success"
    message: str


class MonthlySpendingResult(UiResultBase):
    kind: Literal["monthly_spending"] = "monthly_spending"
    data: MonthlySpendingData


class CategorySpendingResult(UiResultBase):
    kind: Literal["category_spending"] = "category_spending"
    data: CategorySpendingData


class TransactionListResult(UiResultBase):
    kind: Literal["transaction_list"] = "transaction_list"
    data: list[TransactionItem]


class CardListResult(UiResultBase):
    kind: Literal["card_list"] = "card_list"
    data: list[CardItem]


class SubscriptionListResult(UiResultBase):
    kind: Literal["subscription_list"] = "subscription_list"
    data: list[SubscriptionItem]


class SubscriptionCostResult(UiResultBase):
    kind: Literal["subscription_cost"] = "subscription_cost"
    data: SubscriptionCostData


class RecipientListResult(UiResultBase):
    kind: Literal["recipient_list"] = "recipient_list"
    data: list[RecipientItem]


class WealthRiskProfileResult(UiResultBase):
    kind: Literal["wealth_risk_profile"] = "wealth_risk_profile"
    data: RiskProfileData


class WealthProductListResult(UiResultBase):
    kind: Literal["wealth_product_list"] = "wealth_product_list"
    data: list[WealthProductItem]


class WealthHoldingListResult(UiResultBase):
    kind: Literal["wealth_holding_list"] = "wealth_holding_list"
    data: list[WealthHoldingItem]


class CancelSubscriptionPreviewResult(UiResultBase):
    kind: Literal["cancel_subscription_preview"] = "cancel_subscription_preview"
    data: CancelSubscriptionPreviewData


class CancelSubscriptionCompletedResult(UiResultBase):
    kind: Literal["cancel_subscription_completed"] = "cancel_subscription_completed"
    data: SubscriptionItem


class CardLostPreviewResult(UiResultBase):
    kind: Literal["card_lost_preview"] = "card_lost_preview"
    data: CardLostPreviewData


class CardLostCompletedResult(UiResultBase):
    kind: Literal["card_lost_completed"] = "card_lost_completed"
    data: CardLostCompletedData


class DailyLimitPreviewResult(UiResultBase):
    kind: Literal["daily_limit_preview"] = "daily_limit_preview"
    data: DailyLimitPreviewData


class DailyLimitCompletedResult(UiResultBase):
    kind: Literal["daily_limit_completed"] = "daily_limit_completed"
    data: DailyLimitCompletedData


class TransferPreviewResult(UiResultBase):
    kind: Literal["transfer_preview"] = "transfer_preview"
    data: TransferPreviewData


class TransferCompletedResult(UiResultBase):
    kind: Literal["transfer_completed"] = "transfer_completed"
    data: TransferCompletedData


class WealthPurchasePreviewResult(UiResultBase):
    kind: Literal["wealth_purchase_preview"] = "wealth_purchase_preview"
    data: WealthPurchasePreviewData


class WealthPurchaseCompletedResult(UiResultBase):
    kind: Literal["wealth_purchase_completed"] = "wealth_purchase_completed"
    data: WealthOrderCompletedData


class WealthRedemptionPreviewResult(UiResultBase):
    kind: Literal["wealth_redemption_preview"] = "wealth_redemption_preview"
    data: WealthRedemptionPreviewData


class WealthRedemptionCompletedResult(UiResultBase):
    kind: Literal["wealth_redemption_completed"] = "wealth_redemption_completed"
    data: WealthOrderCompletedData


UiResult = Annotated[
    MonthlySpendingResult
    | CategorySpendingResult
    | TransactionListResult
    | CardListResult
    | SubscriptionListResult
    | SubscriptionCostResult
    | RecipientListResult
    | WealthRiskProfileResult
    | WealthProductListResult
    | WealthHoldingListResult
    | CancelSubscriptionPreviewResult
    | CancelSubscriptionCompletedResult
    | CardLostPreviewResult
    | CardLostCompletedResult
    | DailyLimitPreviewResult
    | DailyLimitCompletedResult
    | TransferPreviewResult
    | TransferCompletedResult
    | WealthPurchasePreviewResult
    | WealthPurchaseCompletedResult
    | WealthRedemptionPreviewResult
    | WealthRedemptionCompletedResult,
    Field(discriminator="kind"),
]


class UiEnvelope(StrictUiModel):
    contract_version: Literal["1.1"] = "1.1"
    session_id: str
    status: UiStatus
    message: str
    interaction: UiInteraction
    task: UiTask | None = None
    result: UiResult | None = None
    error: UiError | None = None
