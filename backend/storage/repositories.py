"""Explicit PG SQL; no SQLite adapter, authorization, or funds executor."""
from dataclasses import dataclass

from .errors import Code, StorageError


def public_id(value):
    # Existing APIs use integer identifiers, including legacy owner 0.
    if type(value) is not int or not 0 <= value <= 2147483647:
        raise StorageError(Code.INTEGRITY)
    return value


@dataclass(frozen=True)
class AccountView:
    account_id: int
    owner_id: int
    currency: str
    status: str
    posted_minor: int
    held_minor: int
    available_minor: int


@dataclass(frozen=True)
class CardView:
    card_id: int
    account_id: int
    owner_id: int
    status: str
    masked_number: str


class CoreRepository:
    def __init__(self, connection):
        self._connection = connection

    def get_account(self, account_id: int) -> AccountView | None:
        row = self._connection.execute("""
            SELECT a.public_id AS account_id, u.public_id AS owner_id,
                   a.currency, a.status, a.posted_minor, a.held_minor,
                   a.posted_minor-a.held_minor AS available_minor
              FROM core.accounts a JOIN core.users u ON u.id=a.owner_id
             WHERE a.public_id=%s
        """, (public_id(account_id),)).fetchone()
        if row is None:
            return None
        row["status"] = "normal" if row["status"] == "active" else row["status"]
        return AccountView(**row)

    def get_card(self, card_id: int) -> CardView | None:
        row = self._connection.execute("""
            SELECT c.public_id AS card_id, a.public_id AS account_id,
                   u.public_id AS owner_id, c.status, c.masked_number
              FROM core.cards c JOIN core.accounts a ON a.id=c.account_id
              JOIN core.users u ON u.id=c.owner_id WHERE c.public_id=%s
        """, (public_id(card_id),)).fetchone()
        if row is None:
            return None
        row["status"] = "normal" if row["status"] == "active" else row["status"]
        return CardView(**row)
