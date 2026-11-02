"""Synchronous bounded pool and explicit transaction ownership."""
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout

from .config import PostgresConfig
from .errors import Code, StorageError, controlled_error


class SafeConnection(psycopg.Connection):
    @classmethod
    def connect(cls, *args, **kwargs):
        # psycopg_pool logs connection exceptions from its background workers.
        # Redact at the source, before those exceptions can reach a logger.
        try:
            return super().connect(*args, **kwargs)
        except (psycopg.Error, ValueError):
            raise psycopg.OperationalError("storage_connection_failed") from None


def connect(config: PostgresConfig):
    """Migration/tool connection. Caller must close it, including on failure."""
    try:
        return SafeConnection.connect(config.dsn, autocommit=True,
                                      row_factory=dict_row,
                                      connect_timeout=config.connect_timeout,
                                      options=f"-c statement_timeout={config.statement_timeout_ms} "
                                              f"-c lock_timeout={config.lock_timeout_ms}")
    except psycopg.Error:
        raise StorageError(Code.UNAVAILABLE) from None


@dataclass(frozen=True)
class TrustedTestContext:
    """Internal fixture context ONLY; never construct from HTTP/model arguments.

    Not an authentication mechanism. PG-02 must replace this with an adapter
    from the server's verified principal. No public endpoint consumes this type.
    """
    owner_id: UUID

    def __post_init__(self):
        if type(self.owner_id) is not UUID:
            raise StorageError(Code.CONTEXT)


@dataclass(frozen=True)
class UnitOfWork:
    connection: psycopg.Connection

    @property
    def core(self):
        from .repositories import CoreRepository
        return CoreRepository(self.connection)


def assert_runtime_role(connection):
    row = connection.execute("""
        SELECT current_user AS name, rolsuper, rolbypassrls, rolcreaterole,
               rolcreatedb FROM pg_catalog.pg_roles WHERE rolname=current_user
    """).fetchone()
    owner = connection.execute("""
        SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname IN ('core','ops','pg02')
            AND pg_catalog.pg_has_role(current_user,c.relowner,'USAGE')) AS owns
    """).fetchone()["owns"]
    if (row["name"] != "banking_runtime" or owner
            or any(row[k] for k in ("rolsuper", "rolbypassrls", "rolcreaterole", "rolcreatedb"))):
        raise StorageError(Code.PERMISSION)


class PostgresStore:
    def __init__(self, config: PostgresConfig):
        self.config = config
        try:
            conninfo_to_dict(config.dsn)
        except (psycopg.Error, ValueError):
            raise StorageError(Code.CONFIG) from None

        def configure(connection):
            try:
                assert_runtime_role(connection)
            except (psycopg.Error, StorageError):
                raise psycopg.OperationalError("storage_runtime_role_rejected") from None

        self._pool = ConnectionPool(
            config.dsn, connection_class=SafeConnection, configure=configure,
            kwargs={"autocommit": True, "row_factory": dict_row,
                    "connect_timeout": config.connect_timeout,
                    "options": f"-c statement_timeout={config.statement_timeout_ms} "
                               f"-c lock_timeout={config.lock_timeout_ms} "
                               "-c idle_in_transaction_session_timeout=10000"},
            min_size=config.min_size, max_size=config.max_size,
            timeout=config.acquire_timeout, max_waiting=32,
            reconnect_timeout=config.acquire_timeout, name="pg01-runtime", open=False,
        )

    def open(self):
        try:
            self._pool.open(wait=True, timeout=self.config.acquire_timeout)
        except (PoolTimeout, PoolClosed, psycopg.Error):
            self.close()
            raise StorageError(Code.UNAVAILABLE) from None
        return self

    def close(self):
        self._pool.close(timeout=self.config.acquire_timeout)

    def __enter__(self):
        return self.open()

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def uow(self, context: TrustedTestContext, *, readonly_snapshot=False):
        if type(context) is not TrustedTestContext:
            raise StorageError(Code.CONTEXT)
        phase = "acquire"
        try:
            with self._pool.connection(timeout=self.config.acquire_timeout) as connection:
                with connection.transaction():
                    phase = "body"
                    if readonly_snapshot:
                        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    connection.execute("SELECT set_config('banking.owner_id', %s, true)",
                                       (str(context.owner_id),))
                    yield UnitOfWork(connection)
                    # PostgreSQL can otherwise silently ROLLBACK a failed
                    # transaction when a caller catches an inner SQL exception.
                    if connection.info.transaction_status != TransactionStatus.INTRANS:
                        raise StorageError(Code.ABORTED)
                    phase = "commit"
        except (PoolClosed, PoolTimeout):
            raise StorageError(Code.UNAVAILABLE) from None
        except psycopg.Error as error:
            raise controlled_error(error, committing=phase == "commit") from None
