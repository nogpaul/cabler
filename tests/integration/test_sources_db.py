"""The source registry against a real database.

Run with:  CABLER_ENV=dev pytest -m integration   (after `python -m cabler.migrate up dev`)
"""

import os

import psycopg
import pytest

from cabler import db, sources

pytestmark = pytest.mark.integration

if "CABLER_ENV" not in os.environ:
    pytest.skip("CABLER_ENV not set", allow_module_level=True)


def test_registering_is_idempotent_and_stores_the_licence() -> None:
    specs = list(sources.load_all().values())
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        first = [sources.register(conn, spec) for spec in specs]
        second = [sources.register(conn, spec) for spec in specs]
        assert second == [False] * len(specs)
        assert isinstance(first[0], bool)

        row = conn.execute(
            "SELECT licence_id, attribution FROM meta.source WHERE source_id = %s",
            (specs[0].source_id,),
        ).fetchone()
        assert row == (specs[0].licence_id, specs[0].attribution)


def test_the_read_only_role_cannot_register_sources() -> None:
    spec = next(iter(sources.load_all().values()))
    with db.connect("ro") as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        sources.register(conn, spec)
