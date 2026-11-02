"""Typed payment-pipeline failures.

HTTP layer preserves the project convention ``{code, message}`` for new
business APIs; the existing foundation tool protocol keeps its own envelope.
"""
from __future__ import annotations


class PaymentError(Exception):
    def __init__(self, code, message, status=422, *, data=None):
        self.code, self.message, self.status = code, message, status
        self.data = data or {}
        super().__init__(message)
