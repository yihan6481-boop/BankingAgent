"""Versioned SQL entry point; a whole apply batch and metadata are atomic."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

import psycopg

from .errors import Code, StorageError, controlled_error
from .postgres import connect

MIGRATIONS = Path(__file__).with_name("sql")
LOCK_KEY = 1346842673


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    checksum: str
    sql: str


@dataclass(frozen=True)
class MigrationStatus:
    version: int
    name: str
    checksum: str
    applied: bool


def load_migrations(directory=MIGRATIONS):
    items = []
    try:
        for path in sorted(Path(directory).glob("*.sql")):
            match = re.fullmatch(r"([0-9]{4})_([a-z0-9_]+)\.sql", path.name)
            if match is None:
                raise StorageError(Code.MIGRATION)
            raw = path.read_bytes()
            items.append(Migration(int(match[1]), path.name,
                                   hashlib.sha256(raw).hexdigest(), raw.decode("utf-8")))
    except (OSError, UnicodeError):
        raise StorageError(Code.MIGRATION) from None
    if not items or [m.version for m in items] != list(range(1, len(items)+1)):
        raise StorageError(Code.MIGRATION)
    return items


def _history(connection, items):
    exists = connection.execute("SELECT to_regclass('ops.schema_migrations') AS t").fetchone()["t"]
    rows = connection.execute(
        "SELECT version,name,checksum FROM ops.schema_migrations ORDER BY version"
    ).fetchall() if exists else []
    if len(rows) > len(items):
        raise StorageError(Code.MIGRATION)
    for row, item in zip(rows, items):
        if (row["version"], row["name"], row["checksum"]) != (item.version, item.name, item.checksum):
            raise StorageError(Code.MIGRATION)
    return len(rows)


def _assert_migrator(connection):
    role = connection.execute("""SELECT current_user AS name, rolsuper,rolbypassrls
        FROM pg_roles WHERE rolname=current_user""").fetchone()
    if role["name"] != "banking_migrator" or role["rolsuper"] or role["rolbypassrls"]:
        raise StorageError(Code.PERMISSION)


def migrate(config, *, apply=False, directory=MIGRATIONS) -> list[MigrationStatus]:
    items = load_migrations(directory)
    committing = False
    try:
        with connect(config) as connection:
            with connection.transaction():
                _assert_migrator(connection)
                # The lock also serializes first-time schema/metadata creation.
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
                count = _history(connection, items)
                if apply:
                    connection.execute("CREATE SCHEMA IF NOT EXISTS ops AUTHORIZATION banking_migrator")
                    connection.execute("REVOKE ALL ON SCHEMA ops FROM PUBLIC")
                    connection.execute("""CREATE TABLE IF NOT EXISTS ops.schema_migrations (
                        version INTEGER PRIMARY KEY CHECK(version>0),
                        name TEXT NOT NULL UNIQUE,
                        checksum TEXT NOT NULL CHECK(checksum ~ '^[0-9a-f]{64}$'),
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
                    connection.execute("GRANT USAGE ON SCHEMA ops TO banking_runtime")
                    connection.execute("GRANT SELECT ON ops.schema_migrations TO banking_runtime")
                    for item in items[count:]:
                        # SQL files are reviewed project assets, never model/request SQL.
                        connection.execute(item.sql, prepare=False)
                        connection.execute("""INSERT INTO ops.schema_migrations
                            (version,name,checksum) VALUES (%s,%s,%s)""",
                                           (item.version, item.name, item.checksum))
                    count = len(items)
                result = [MigrationStatus(m.version,m.name,m.checksum,i<count)
                          for i,m in enumerate(items)]
                committing = True
            return result
    except psycopg.Error as error:
        raise controlled_error(error, committing=committing) from None
