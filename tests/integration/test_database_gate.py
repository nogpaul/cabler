"""Database gate: the database exists, has the right extensions, schemas and privileges.

Run against an environment with:  CABLER_ENV=dev pytest -m integration
"""

import os

import psycopg
import pytest

from cabler import db

pytestmark = pytest.mark.integration

if "CABLER_ENV" not in os.environ:
    pytest.skip("CABLER_ENV not set", allow_module_level=True)

SCHEMAS = {"raw", "staging", "core", "results", "meta"}


def test_postgis_and_pgrouting_are_installed() -> None:
    with db.connect("ro") as conn:
        postgis = conn.execute("SELECT postgis_version()").fetchone()
        pgr = conn.execute("SELECT pgr_version()").fetchone()
    assert postgis is not None
    assert postgis[0].startswith("3.5")
    assert pgr is not None
    assert pgr[0].startswith("3.")


def test_etrs89_utm32n_is_available() -> None:
    with db.connect("ro") as conn:
        row = conn.execute("SELECT srtext FROM spatial_ref_sys WHERE srid = 25832").fetchone()
    assert row is not None
    assert "ETRS89" in row[0]


def test_pgrouting_dijkstra_smoke() -> None:
    sql = "SELECT 1 AS id, 1 AS source, 2 AS target, 2.5 AS cost"
    with db.connect("ro") as conn:
        rows = conn.execute("SELECT agg_cost FROM pgr_dijkstra(%s, 1, 2, false)", (sql,)).fetchall()
    assert rows[-1][0] == pytest.approx(2.5)


def test_expected_schemas_exist_and_are_owned_by_owner() -> None:
    with db.connect("ro") as conn:
        rows = conn.execute(
            "SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = ANY(%s)",
            (sorted(SCHEMAS),),
        ).fetchall()
    assert {r[0] for r in rows} == SCHEMAS
    assert {r[1] for r in rows} == {"cabler_owner"}


def test_privilege_model() -> None:
    """Tables made by the owner are writable by app, read-only for ro, and app cannot do DDL."""
    owner = db.connect("owner")
    try:
        owner.execute("CREATE TABLE core._gate_probe (id int PRIMARY KEY)")
        owner.commit()

        with db.connect("app") as app:
            app.execute("INSERT INTO core._gate_probe VALUES (1)")
            app.commit()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app.execute("CREATE TABLE core._not_allowed (id int)")
            app.rollback()

        with db.connect("ro") as ro:
            assert ro.execute("SELECT count(*) FROM core._gate_probe").fetchone() == (1,)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                ro.execute("INSERT INTO core._gate_probe VALUES (2)")
            ro.rollback()
    finally:
        owner.rollback()
        owner.execute("DROP TABLE IF EXISTS core._gate_probe")
        owner.commit()
        owner.close()


def test_app_cannot_create_objects_in_public() -> None:
    with db.connect("app") as app, pytest.raises(psycopg.errors.InsufficientPrivilege):
        app.execute("CREATE TABLE public._not_allowed (id int)")
