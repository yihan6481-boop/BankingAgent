"""Short-lived financial read snapshots; metadata and writes stay outside."""
from contextlib import contextmanager
from functools import wraps


@contextmanager
def snapshot():
    from .config import pg02_enabled
    if not pg02_enabled():
        yield
        return
    from .pg02 import read_snapshot
    with read_snapshot():
        yield


def financial_read(function):
    @wraps(function)
    def run(*args, **kwargs):
        from .config import pg02_enabled
        if not pg02_enabled():
            return function(*args, **kwargs)
        # Pool acquisition/BEGIN can fail before the service's own try block.
        import sqlite3
        import psycopg
        from .errors import StorageError
        try:
            with snapshot():
                return function(*args, **kwargs)
        except (StorageError, psycopg.Error, sqlite3.Error):
            return dict(success=False,error_code='analysis_data_unavailable',
                        message='账单数据不可用，请稍后重试',data=None)
    return run
