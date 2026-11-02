"""Per-turn accumulated memory network wait, preserved in the existing usage."""
from contextlib import contextmanager
from contextvars import ContextVar

budget=ContextVar('r4_interaction_wait',default=None)


@contextmanager
def interaction(usage):
    token=budget.set(usage)
    try:yield
    finally:budget.reset(token)
