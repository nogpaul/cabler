"""Migration runner and metadata catalogue, tested against a real database.

Run with:  CABLER_ENV=dev pytest -m integration   (after `python -m cabler.migrate up dev`)
"""

import os

import psycopg
import pytest

from cabler import db, migrate
from cabler.migrate import Migration, MigrationError

pytestmark = pytest.mark.integration

if "CABLER_ENV" not in os.environ:
    pytest.skip("CABLER_ENV not set", allow_module_level=True)

PROBE = "meta._migration_probe"


def test_apply_pending_is_idempotent_and_detects_tampering() -> None:
    migrations = [
        Migration(1, "create", "CREATE TABLE core._mig_probe (id int);"),
        Migration(2, "extend", "ALTER TABLE core._mig_probe ADD COLUMN note text;"),
    ]
    with db.connect("owner") as conn, conn.transaction(force_rollback=True):
        assert migrate.apply_pending(conn, migrations, table=PROBE) == [1, 2]
        assert migrate.apply_pending(conn, migrations, table=PROBE) == []

        tampered = [Migration(1, "create", "CREATE TABLE core._mig_probe (id bigint);")]
        with pytest.raises(MigrationError, match="changed after it was applied"):
            migrate.apply_pending(conn, [*tampered, migrations[1]], table=PROBE)


def test_a_failing_migration_leaves_no_partial_change() -> None:
    migrations = [
        Migration(1, "ok", "CREATE TABLE core._mig_probe_ok (id int);"),
        Migration(
            2, "broken", "CREATE TABLE core._mig_probe_half (id int); SELECT * FROM nowhere;"
        ),
    ]
    with db.connect("owner") as conn, conn.transaction(force_rollback=True):
        with pytest.raises(psycopg.errors.UndefinedTable):
            migrate.apply_pending(conn, migrations, table=PROBE)
        assert migrate.applied(conn, PROBE) == {1: migrations[0].checksum}
        half = conn.execute("SELECT to_regclass('core._mig_probe_half')").fetchone()
        assert half == (None,)


def test_repository_migrations_are_all_applied() -> None:
    with db.connect("ro") as conn:
        recorded = migrate.applied(conn)
    expected = {m.version: m.checksum for m in migrate.discover()}
    assert recorded == expected, "run: python -m cabler.migrate up <env>"


def test_catalogue_tables_exist_with_the_expected_privileges() -> None:
    insert = (
        "INSERT INTO meta.source (source_id, title, publisher, licence_id, licence_url,"
        " attribution) VALUES ('probe', 't', 'p', 'l', 'u', 'a')"
    )
    with db.connect("app") as app:
        app.execute(insert)
        app.rollback()
    with db.connect("ro") as ro:
        assert ro.execute("SELECT count(*) FROM meta.pipeline_run").fetchone() is not None
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            ro.execute(insert)


def test_catalogue_constraints_reject_bad_data() -> None:
    with db.connect("app") as app:
        with pytest.raises(psycopg.errors.CheckViolation):
            app.execute(
                "INSERT INTO meta.source (source_id, title, publisher, licence_id, licence_url,"
                " attribution) VALUES ('Bad Id!', 't', 'p', 'l', 'u', 'a')"
            )
        app.rollback()
        app.execute(
            "INSERT INTO meta.source (source_id, title, publisher, licence_id, licence_url,"
            " attribution) VALUES ('probe', 't', 'p', 'l', 'u', 'a')"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            app.execute(
                "INSERT INTO meta.source_version (source_id, remote_url, sha256)"
                " VALUES ('probe', 'https://example.invalid/x', 'not-a-hash')"
            )
        app.rollback()
