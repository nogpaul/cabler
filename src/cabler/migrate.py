"""Apply versioned SQL migrations and record them in the database.

Migrations are plain ``.sql`` files in ``db/migrations`` named ``NNNN_description.sql``. They run in
order, each inside its own transaction, as the owner role, and are recorded in
``meta.schema_migration`` with a checksum so that an edited, already-applied file is detected.

Usage: python -m cabler.migrate {up,status} {dev,staging,production}
"""

from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from cabler import db
from cabler.envgen import NAMES

MIGRATIONS_DIR = db.REPO_ROOT / "db" / "migrations"
FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class MigrationError(Exception):
    """Migration files or the recorded history are inconsistent."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Read every migration file; versions must run 1..N with no gaps or duplicates."""
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = FILENAME.match(path.name)
        if match is None:
            msg = f"bad migration file name: {path.name} (expected NNNN_description.sql)"
            raise MigrationError(msg)
        migrations.append(Migration(int(match[1]), match[2], path.read_text(encoding="utf-8")))
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        msg = f"versions must run 1..N without gaps or duplicates, found {versions}"
        raise MigrationError(msg)
    return migrations


DEFAULT_TABLE = "meta.schema_migration"
TABLE_NAME = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")


def _table(name: str) -> sql.Identifier:
    if not TABLE_NAME.match(name):
        msg = f"invalid table name: {name!r}"
        raise MigrationError(msg)
    schema, table = name.split(".")
    return sql.Identifier(schema, table)


def ensure_table(conn: psycopg.Connection[Any], table: str = DEFAULT_TABLE) -> None:
    """Create the bookkeeping table if it does not exist yet."""
    conn.execute(
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {} ("
            "version int PRIMARY KEY, name text NOT NULL, checksum text NOT NULL, "
            "applied_at timestamptz NOT NULL DEFAULT now())"
        ).format(_table(table))
    )


def applied(conn: psycopg.Connection[Any], table: str = DEFAULT_TABLE) -> dict[int, str]:
    """Return ``{version: checksum}`` for every migration recorded as applied."""
    query = sql.SQL("SELECT version, checksum FROM {} ORDER BY version").format(_table(table))
    return {int(v): str(c) for v, c in conn.execute(query).fetchall()}


def applied_or_empty(conn: psycopg.Connection[Any], table: str = DEFAULT_TABLE) -> dict[int, str]:
    """Like :func:`applied`, but a missing bookkeeping table means nothing is applied yet."""
    row = conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()
    if row is None or row[0] is None:
        return {}
    return applied(conn, table)


def pending(migrations: list[Migration], done: dict[int, str]) -> list[Migration]:
    """Return the migrations still to run; refuse if history and files disagree."""
    by_version = {m.version: m for m in migrations}
    for version, checksum in done.items():
        known = by_version.get(version)
        if known is None:
            msg = f"database has migration {version} but no file provides it"
            raise MigrationError(msg)
        if known.checksum != checksum:
            msg = f"migration {version} ({known.name}) was changed after it was applied"
            raise MigrationError(msg)
    return [m for m in migrations if m.version not in done]


def apply_pending(
    conn: psycopg.Connection[Any], migrations: list[Migration], table: str = DEFAULT_TABLE
) -> list[int]:
    """Apply every pending migration, one transaction each. Returns the versions applied."""
    ensure_table(conn, table)
    todo = pending(migrations, applied(conn, table))
    record = sql.SQL("INSERT INTO {} (version, name, checksum) VALUES (%s, %s, %s)").format(
        _table(table)
    )
    for migration in todo:
        with conn.transaction():
            conn.execute(migration.sql)
            conn.execute(record, (migration.version, migration.name, migration.checksum))
    return [m.version for m in todo]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("up", "status"))
    parser.add_argument("name", choices=NAMES)
    args = parser.parse_args(argv)

    migrations = discover()
    with db.connect("owner", environ={"CABLER_ENV": args.name}) as conn:
        conn.autocommit = True
        if args.action == "up":
            done = apply_pending(conn, migrations)
            print(f"applied: {done}" if done else "nothing to apply")
            return 0
        recorded = applied_or_empty(conn)
        for m in migrations:
            state = "applied" if m.version in recorded else "PENDING"
            print(f"{m.version:04d} {m.name:<30} {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
