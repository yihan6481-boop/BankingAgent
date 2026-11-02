"""Uniform object-ownership checks and scoped business queries.

Every private read and write in V0.7.0 must prove that the acting principal
owns the object it references.  Instead of repeating ``WHERE owner_id = ?`` in
a dozen places (and forgetting it in one), all ownership resolution happens
here.

Security posture:

* a resource owned by another user is reported as **not found**, so responses
  cannot be used to enumerate other users' cards, holdings or subscriptions;
* the public simulated wealth-product catalogue is the single documented
  exception and is intentionally ownerless;
* the legacy V1 demo scope (``owner_id = 0``) only owns its own empty scope, so
  it cannot reach simulated-user data.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from database import get_connection

from .config import LEGACY_DEMO_USER_ID
from .schemas import Principal, ResourceRef


class OwnershipError(Exception):
    """Resource missing or not owned by the acting principal."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ObjectDefinition:
    table: str
    id_column: str
    owner_column: str
    label: str


# Keyed by ``ResourceRef.kind``.  ``wealth_products`` is deliberately absent:
# it is the public simulated catalogue.
OBJECT_DEFINITIONS: dict[str, ObjectDefinition] = {
    "card": ObjectDefinition("cards", "card_id", "owner_id", "银行卡"),
    "recipient": ObjectDefinition(
        "recipients", "recipient_id", "owner_id", "收款人"
    ),
    "subscription": ObjectDefinition(
        "subscriptions", "subscription_id", "owner_id", "订阅"
    ),
    "wealth_holding": ObjectDefinition(
        "wealth_holdings", "holding_id", "user_id", "理财持仓"
    ),
    "transfer_order": ObjectDefinition(
        "transfer_orders", "transfer_id", "owner_id", "转账订单"
    ),
    "wealth_order": ObjectDefinition(
        "wealth_orders", "order_id", "user_id", "理财订单"
    ),
    "transaction": ObjectDefinition(
        "transactions", "transaction_id", "owner_id", "交易流水"
    ),
}

PUBLIC_OBJECT_KINDS = {"wealth_product", "other"}


def _owner_of(row, definition: ObjectDefinition):
    try:
        keys = set(row.keys())
    except AttributeError:
        keys = set()
    if definition.owner_column in keys:
        return row[definition.owner_column]
    return None


def load_owned(
    connection,
    principal: Principal,
    kind: str,
    object_id,
    *,
    for_update: bool = False,
):
    """Return the row only when it exists *and* belongs to the principal."""
    definition = OBJECT_DEFINITIONS.get(kind)
    if definition is None:
        if kind in PUBLIC_OBJECT_KINDS:
            return _load_public(connection, kind, object_id)
        raise OwnershipError("unknown_object_kind", "未知的资源类型，拒绝访问")
    if object_id is None:
        raise OwnershipError(
            f"{kind}_not_found", f"{definition.label}不存在"
        )
    row = connection.execute(
        f"SELECT * FROM {definition.table} WHERE {definition.id_column} = ?",
        (object_id,),
    ).fetchone()
    if row is None:
        raise OwnershipError(
            f"{kind}_not_found", f"{definition.label}不存在"
        )
    owner = _owner_of(row, definition)
    if owner is None or int(owner) != int(principal.user_id):
        # 不区分“不存在”和“不属于当前用户”，避免跨用户资源枚举。
        raise OwnershipError(
            f"{kind}_not_found", f"{definition.label}不存在"
        )
    return row


def _load_public(connection, kind, object_id):
    if kind == "wealth_product":
        row = connection.execute(
            "SELECT * FROM wealth_products WHERE product_id = ?", (object_id,)
        ).fetchone()
        if row is None:
            raise OwnershipError("wealth_product_not_found", "模拟理财产品不存在")
        return row
    return None


def policy_owner_scope(principal: Principal) -> int:
    """The ``owner_id`` value that scopes every business query.

    Simulated principals map to their own ``user_id``.  The legacy V1 demo
    principal maps to ``0``: a disjoint owner scope that never contains
    simulated-user data.
    """
    if principal is None:
        raise OwnershipError("principal_required", "缺少服务器端身份")
    if principal.kind == "legacy_demo":
        return LEGACY_DEMO_USER_ID
    return int(principal.user_id)


def assert_owned_row(row, principal: Principal, kind: str):
    definition = OBJECT_DEFINITIONS.get(kind)
    if definition is None:
        return row
    if row is None:
        raise OwnershipError(f"{kind}_not_found", f"{definition.label}不存在")
    owner = row[definition.owner_column] if definition.owner_column in row.keys() else None
    if owner is None or int(owner) != policy_owner_scope(principal):
        raise OwnershipError(f"{kind}_not_found", f"{definition.label}不存在")
    return row


def owned_ids(connection, principal: Principal, kind: str) -> list[int]:
    """All ids of ``kind`` owned by the principal (used for cumulative totals)."""
    definition = OBJECT_DEFINITIONS.get(kind)
    if definition is None:
        return []
    if principal.kind == "legacy_demo":
        scope = LEGACY_DEMO_USER_ID
    else:
        scope = int(principal.user_id)
    rows = connection.execute(
        f"SELECT {definition.id_column} AS id FROM {definition.table} "
        f"WHERE {definition.owner_column} = ? ORDER BY {definition.id_column}",
        (scope,),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def owned_card_ids(connection, principal: Principal) -> list[int]:
    """Card ids owned by the principal, used for the cumulative transfer rule."""
    return owned_ids(connection, principal, "card")


def resource_refs_for_decision(
    principal: Principal, action: str, arguments: dict
) -> list[ResourceRef]:
    """Best-effort structured resource summary for the policy decision record."""
    refs: list[ResourceRef] = []
    mapping = {
        "from_card_id": "card",
        "card_id": "card",
        "recipient_id": "recipient",
        "subscription_id": "subscription",
        "holding_id": "wealth_holding",
        "product_id": "wealth_product",
    }
    for key, kind in mapping.items():
        value = (arguments or {}).get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            refs.append(ResourceRef(kind=kind, object_id=value, label=key))
    return refs


def safe_owner_count(connection, table: str, owner_column: str, owner_id: int):
    try:
        return connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {owner_column} = ?", (owner_id,)
        ).fetchone()[0]
    except sqlite3.Error:
        return 0


def get_raw_connection():
    return get_connection()
