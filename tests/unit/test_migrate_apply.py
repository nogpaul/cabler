import contextlib
from typing import Any, Self

import pytest

from cabler import migrate
from cabler.migrate import Migration, MigrationError


def test_pending_returns_only_unapplied_migrations() -> None:
    migrations = [Migration(1, "a", "x"), Migration(2, "b", "y")]
    done = {1: migrations[0].checksum}
    assert migrate.pending(migrations, done) == [migrations[1]]


def test_pending_detects_an_edited_migration() -> None:
    migrations = [Migration(1, "a", "x")]
    with pytest.raises(MigrationError, match="changed after it was applied"):
        migrate.pending(migrations, {1: "0" * 64})


def test_pending_detects_history_ahead_of_files() -> None:
    with pytest.raises(MigrationError, match="no file provides it"):
        migrate.pending([], {1: "0" * 64})


def test_table_names_are_validated() -> None:
    with pytest.raises(MigrationError, match="invalid table name"):
        migrate._table("meta.x; DROP TABLE y")
    with pytest.raises(MigrationError, match="invalid table name"):
        migrate._table("no_schema")


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class FakeConnection:
    """Records what the runner executes and answers the bookkeeping queries."""

    def __init__(self, applied_rows: list[tuple[Any, ...]], table_exists: bool = True) -> None:
        self.applied_rows = applied_rows
        self.table_exists = table_exists
        self.autocommit = False
        self.calls: list[tuple[str, Any]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def transaction(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    def execute(self, query: Any, params: Any = None) -> FakeResult:
        text = str(query)
        self.calls.append((text, params))
        if "SELECT version, checksum" in text:
            return FakeResult(self.applied_rows)
        if "to_regclass" in text:
            return FakeResult([("meta.schema_migration",) if self.table_exists else (None,)])
        return FakeResult([])


def test_apply_pending_runs_only_new_migrations_and_records_them() -> None:
    migrations = [Migration(1, "a", "CREATE TABLE a ();"), Migration(2, "b", "CREATE TABLE b ();")]
    conn = FakeConnection([(1, migrations[0].checksum)])
    assert migrate.apply_pending(conn, migrations) == [2]  # type: ignore[arg-type]
    executed = [text for text, _ in conn.calls]
    assert "CREATE TABLE b ();" in executed
    assert "CREATE TABLE a ();" not in executed
    assert (2, "b", migrations[1].checksum) in [params for _, params in conn.calls]
