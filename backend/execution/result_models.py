"""Canonical typed receipt data, shared with HTTP contracts."""
from typing import Literal
from agent.schemas import StrictModel

class TransferCompletedData(StrictModel):
    transfer_id: str
    from_card_id: int
    from_masked_card_number: str
    recipient_id: int
    recipient_name: str
    recipient_masked_account: str
    amount: float
    remark: str | None
    risk_level: Literal["low", "medium", "high"]
    balance_before: float
    balance_after: float
    completed_at: str
    idempotent_replay: bool


class WealthOrderCompletedData(StrictModel):
    order_id: str
    order_type: Literal["BUY", "REDEEM"]
    product_id: int
    product_name: str
    holding_id: int
    card_id: int
    masked_card_number: str
    amount: float
    principal_after: float
    card_balance_after: float
    completed_at: str
    idempotent_replay: bool
    notice: str

