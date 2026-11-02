"""Canonical action-argument builders.

The permission decision, the confirmation phrase, the OTP challenge and the
authorization grant all bind to the *same* normalized argument set.  Those
builders live here so the gateway, the Tool Router, the Workflow engine and the
services cannot drift into computing different digests for the same action —
which would silently either bypass a binding check or break a legitimate
confirmation.

Any change to amount, account, recipient, product or target step changes these
values, which invalidates the old confirmation and challenge (requirement 4.2).
"""

from __future__ import annotations

from typing import Any

WRITE_ACTIONS = (
    "cancel_subscription",
    "report_card_lost",
    "set_daily_limit",
    "transfer_money",
    "buy_wealth_product",
    "redeem_wealth_product",
)


def _int_or_none(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text_or_none(value):
    if value is None:
        return None
    return str(value)


def cancel_subscription_args(subscription_id) -> dict[str, Any]:
    return {"subscription_id": _int_or_none(subscription_id)}


def report_card_lost_args(card_id) -> dict[str, Any]:
    return {"card_id": _int_or_none(card_id)}


def set_daily_limit_args(card_id, new_limit_cents) -> dict[str, Any]:
    return {
        "card_id": _int_or_none(card_id),
        "new_limit_cents": _int_or_none(new_limit_cents),
    }


def transfer_args(
    from_card_id,
    recipient_id,
    amount_cents,
    idempotency_key,
    remark=None,
) -> dict[str, Any]:
    return {
        "from_card_id": _int_or_none(from_card_id),
        "recipient_id": _int_or_none(recipient_id),
        "amount_cents": _int_or_none(amount_cents),
        "remark": _text_or_none(remark),
        "idempotency_key": _text_or_none(idempotency_key),
    }


def wealth_purchase_args(product_id, card_id, amount_cents, idempotency_key) -> dict[str, Any]:
    return {
        "product_id": _int_or_none(product_id),
        "card_id": _int_or_none(card_id),
        "amount_cents": _int_or_none(amount_cents),
        "idempotency_key": _text_or_none(idempotency_key),
    }


def wealth_redemption_args(holding_id, amount_cents, idempotency_key) -> dict[str, Any]:
    return {
        "holding_id": _int_or_none(holding_id),
        "amount_cents": _int_or_none(amount_cents),
        "idempotency_key": _text_or_none(idempotency_key),
    }


BUILDERS = {
    "cancel_subscription": cancel_subscription_args,
    "report_card_lost": report_card_lost_args,
    "set_daily_limit": set_daily_limit_args,
    "transfer_money": transfer_args,
    "buy_wealth_product": wealth_purchase_args,
    "redeem_wealth_product": wealth_redemption_args,
}


def canonical_arguments(action: str, **values) -> dict[str, Any]:
    builder = BUILDERS.get(action)
    if builder is None:
        raise KeyError(f"unknown write action: {action}")
    return builder(**values)


__all__ = [
    "BUILDERS",
    "WRITE_ACTIONS",
    "cancel_subscription_args",
    "canonical_arguments",
    "report_card_lost_args",
    "set_daily_limit_args",
    "transfer_args",
    "wealth_purchase_args",
    "wealth_redemption_args",
]
