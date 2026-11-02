"""Business connection facade over the existing PostgreSQL UoW. No fallback."""
from storage.config import pg02_enabled
from storage.errors import StorageError, Code
MERCHANT_ALIASES = {'\u6dd8\u5b9d':'taobao','\u6dd8\u5b9d\u7f51':'taobao'}

def require_postgres():
    if not pg02_enabled(): raise StorageError(Code.CONFIG)

def get_connection(busy_timeout_ms=None):
    require_postgres()
    from storage.pg02 import get_connection as connect
    return connect()

def initialize_database():
    require_postgres()
    from storage.pg02 import store
    store()  # Readiness only. Schema/seed are explicit administration operations.

def merchant_catalog():
    require_postgres()
    from storage.pg02 import _context, read_snapshot
    if _context.get() is None: return {}
    with read_snapshot() as c:
        aliases={r['alias']:r['merchant_key'] for r in c.execute('SELECT alias,merchant_key FROM merchant_aliases')}
        for row in c.execute('SELECT DISTINCT merchant FROM transactions'):
            if row['merchant']: aliases.setdefault(row['merchant'],'private:'+row['merchant'])
        return aliases
