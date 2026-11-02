"""Structured calendar conversion; language understanding is model work.

Removed lexical anchors guessed roles of historical/excluded clauses and could
veto valid current plans. Tools retain Schema/catalog/owner/version/amount and
transaction bindings.
"""
from datetime import date


def period(year, month):
    start=date(year, month, 1)
    end=date(year+month//12, month%12+1, 1)
    return dict(start=start.isoformat(), end_exclusive=end.isoformat())
