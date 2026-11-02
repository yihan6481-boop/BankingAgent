/**
 * Banking Agent UI Contract 1.1 — 前端 TypeScript 类型
 * 与后端 backend/api/schemas.py 及 contracts/UI_TYPES.ts 一一对应。
 * 不要把任何业务判断建立在 tool_name 上，只用 title / status / kind。
 */

export type UiStatus =
  | 'ready' | 'need_info' | 'need_confirmation' | 'executing'
  | 'success' | 'cancelled' | 'expired' | 'failed';

export type UiErrorCode =
  | 'interaction_expired' | 'not_found' | 'risk_rejected' | 'insufficient_funds'
  | 'limit_exceeded' | 'validation_failed' | 'verification_failed'
  | 'duplicate_operation' | 'tool_execution_failed' | 'service_unavailable'
  | 'operation_failed' | 'agent_error' | 'contract_payload_mismatch';

export type ApiProblemCode =
  | 'invalid_request' | 'session_not_found' | 'no_pending_interaction' | 'service_unavailable';

export interface ApiProblem {
  contract_version: '1.1';
  code: ApiProblemCode;
  message: string;
}

export interface UiInteraction {
  type: 'none' | 'clarification' | 'confirmation';
  missing_fields: string[];
  confirmation_mode: 'none' | 'explicit' | 'dynamic_phrase';
  /** 仅 dynamic_phrase 时非空；只能展示给用户手动输入，禁止自动代填或自动提交 */
  confirmation_phrase: string | null;
  expires_at: string | null;
}

export type TaskStatus =
  | 'planned' | 'running' | 'waiting_clarification' | 'waiting_confirmation'
  | 'completed' | 'failed' | 'cancelled' | 'expired';

export type StepStatus =
  | 'pending' | 'running' | 'waiting_clarification' | 'waiting_confirmation'
  | 'completed' | 'failed' | 'skipped' | 'cancelled';

export interface UiTaskStep {
  step_id: string;
  title: string;
  tool_name: string;
  status: StepStatus;
  error_code: string | null;
}

export interface UiTask {
  task_id: string;
  status: TaskStatus;
  current_step_id: string | null;
  steps: UiTaskStep[];
}

export interface UiError { code: UiErrorCode; message: string; detail_code: string | null; }

/* ---------------- 业务 payload ---------------- */

export interface MonthlySpendingData { month: string; transaction_count: number; total_spending: number; card_specified: boolean; card_id: number | null; }
export interface CategoryStat { amount: number; percentage: number; }
export interface CategorySpendingData { month: string; card_id: number | null; total_spending: number; categories: Record<string, CategoryStat>; }
export interface TransactionItem { event_at?:string|null;time_source?:string;transaction_type?:string;status?:string; transaction_id: number; card_id: number; card_last_four: string; date: string; merchant: string; category: string; amount: number; direction: 'income' | 'expense'; }
export interface CardItem { card_id: number; bank_name: string; masked_card_number: string; balance: number; status: 'normal' | 'lost' | 'frozen'; daily_limit: number; overseas_enabled: boolean; online_payment_enabled: boolean; }
export interface SubscriptionItem { subscription_id: number; name: string; amount: number; cycle: string; next_charge_date: string; card_id: number; card_last_four: string; status: 'active' | 'cancelled'; }
export interface SubscriptionCostData { active_count: number; monthly_cost: number; annual_cost: number; annual_cost_cents?: number; cost_basis?: string; monthly_rounding?: string; complete?: boolean; unknown_cycle_count?: number; subscriptions: SubscriptionItem[]; }
export interface RecipientItem { recipient_id: number; name: string; bank_name: string; masked_account: string; trusted: boolean; }
export interface WealthRiskProfileData { risk_level: 'R1' | 'R2' | 'R3' | 'R4' | 'R5'; risk_name: string; updated_at: string; notice: string; }
export interface WealthProductItem { product_id: number; name: string; risk_level: 'R1' | 'R2' | 'R3' | 'R4' | 'R5'; min_purchase: number; status: 'active' | 'inactive'; reference_annual_rate: number; duration_days: number; notice: string; }
export interface WealthHoldingItem { holding_id: number; product_id: number; product_name: string; card_id: number; principal: number; status: 'active' | 'redeemed'; purchased_at: string; notice: string; }
export interface CardLostPreviewData { card_id: number; bank_name: string; masked_card_number: string; requires_confirmation: true; }
export interface CardLostCompletedData { card_id: number; masked_card_number: string; status: 'lost'; }
export interface DailyLimitPreviewData { card_id: number; masked_card_number: string; old_limit: number; new_limit: number; requires_confirmation: true; }
export interface DailyLimitCompletedData { card_id: number; masked_card_number: string; old_limit: number; new_limit: number; }
export interface TransferPreviewData { from_card_id: number; from_bank_name: string; from_masked_card_number: string; recipient_id: number; recipient_name: string; recipient_bank_name: string; recipient_masked_account: string; amount: number; amount_cents: number; remark: string | null; risk_level: 'low' | 'medium' | 'high'; requires_confirmation: true; }
export interface TransferCompletedData { transfer_id: string; from_card_id: number; from_masked_card_number: string; recipient_id: number; recipient_name: string; recipient_masked_account: string; amount: number; remark: string | null; risk_level: 'low' | 'medium' | 'high'; balance_before: number; balance_after: number; completed_at: string; idempotent_replay: boolean; }
export interface WealthPurchasePreviewData { product_id: number; product_name: string; product_risk_level: 'R1' | 'R2' | 'R3' | 'R4' | 'R5'; user_risk_level: 'R1' | 'R2' | 'R3' | 'R4' | 'R5'; amount: number; card_id: number; masked_card_number: string; reference_annual_rate: number; duration_days: number; requires_confirmation: true; notice: string; }
export interface WealthRedemptionPreviewData { holding_id: number; product_id: number; product_name: string; card_id: number; masked_card_number: string; current_principal: number; redemption_amount: number; remaining_principal: number; requires_confirmation: true; notice: string; }
export interface WealthOrderCompletedData { order_id: string; order_type: 'BUY' | 'REDEEM'; product_id: number; product_name: string; holding_id: number; card_id: number; masked_card_number: string; amount: number; principal_after: number; card_balance_after: number; completed_at: string; idempotent_replay: boolean; notice: string; }

export type UiResult =
  | { kind: 'monthly_spending'; status: 'success'; message: string; data: MonthlySpendingData }
  | { kind: 'category_spending'; status: 'success'; message: string; data: CategorySpendingData }
  | { kind: 'transaction_list'; status: 'success'; message: string; data: TransactionItem[] }
  | { kind: 'card_list'; status: 'success'; message: string; data: CardItem[] }
  | { kind: 'subscription_list'; status: 'success'; message: string; data: SubscriptionItem[] }
  | { kind: 'subscription_cost'; status: 'success'; message: string; data: SubscriptionCostData }
  | { kind: 'recipient_list'; status: 'success'; message: string; data: RecipientItem[] }
  | { kind: 'wealth_risk_profile'; status: 'success'; message: string; data: WealthRiskProfileData }
  | { kind: 'wealth_product_list'; status: 'success'; message: string; data: WealthProductItem[] }
  | { kind: 'wealth_holding_list'; status: 'success'; message: string; data: WealthHoldingItem[] }
  | { kind: 'cancel_subscription_preview'; status: 'success'; message: string; data: SubscriptionItem & { requires_confirmation: true } }
  | { kind: 'cancel_subscription_completed'; status: 'success'; message: string; data: SubscriptionItem }
  | { kind: 'card_lost_preview'; status: 'success'; message: string; data: CardLostPreviewData }
  | { kind: 'card_lost_completed'; status: 'success'; message: string; data: CardLostCompletedData }
  | { kind: 'daily_limit_preview'; status: 'success'; message: string; data: DailyLimitPreviewData }
  | { kind: 'daily_limit_completed'; status: 'success'; message: string; data: DailyLimitCompletedData }
  | { kind: 'transfer_preview'; status: 'success'; message: string; data: TransferPreviewData }
  | { kind: 'transfer_completed'; status: 'success'; message: string; data: TransferCompletedData }
  | { kind: 'wealth_purchase_preview'; status: 'success'; message: string; data: WealthPurchasePreviewData }
  | { kind: 'wealth_purchase_completed'; status: 'success'; message: string; data: WealthOrderCompletedData }
  | { kind: 'wealth_redemption_preview'; status: 'success'; message: string; data: WealthRedemptionPreviewData }
  | { kind: 'wealth_redemption_completed'; status: 'success'; message: string; data: WealthOrderCompletedData };

export interface UiEnvelope {
  contract_version: '1.1';
  session_id: string;
  status: UiStatus;
  message: string;
  interaction: UiInteraction;
  task: UiTask | null;
  result: UiResult | null;
  error: UiError | null;
}

export interface HealthResponse { status: 'ok'; backend_version: string; contract_version: '1.1'; llm_configured: boolean; }
export interface Capability { id: 'bill' | 'card' | 'subscription' | 'transfer' | 'wealth' | 'workflow'; name: string; enabled: boolean; }
export interface CapabilitiesResponse { contract_version: '1.1'; capabilities: Capability[]; }
export interface ReadRequest { tool: string; arguments: Record<string, unknown>; }
export type ReadResponse = UiEnvelope;

export interface ChatRequest { session_id?: string | null; message: string; }
export interface ConfirmRequest { text: string; }
