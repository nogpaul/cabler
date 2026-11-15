"""The core wind units against a real database: position classes, evidence, scenarios, the rebuild.

The OSM data is tests/fixtures/power_hints.osm (its substations play no part here). The wind
units are the five of test_core_db (placed by hand) plus four more, the OSM turbines are inserted
next to them, and one municipality (Alpha) comes from a boundaries zip. Expected, per unit:

* w1  existing, names Alpha and lies in it, an OSM turbine 3 m away         -> confirmed
* w2  planned, names Alpha but lies about 7 km away                         -> doubtful (far)
* w3  existing, names a key the boundaries lack, a turbine 60 m away        -> plausible, evidence
* gone, placeholder: a retired unit, and a flagged coordinate               -> retired, invalid
* f1, f2, f3  one wind farm, f3 is 6 km from the others                     -> plausible, plausible,
                                                                               doubtful
* nopos  no coordinate                                                      -> unlocated

Run with:  CABLER_ENV=dev pytest -m integration   (after `python -m cabler.migrate up dev`)
"""

import dataclasses
import os
from pathlib import Path
from typing import Any

import psycopg
import pytest

from cabler import apply, db, diff, fetch, load, sources, windcore
from cabler.windcore import WindRules
from itest_helpers import BodyPublisher, boundaries_zip, box, make_spec, prepared

pytestmark = pytest.mark.integration

if "CABLER_ENV" not in os.environ:
    pytest.skip("CABLER_ENV not set", allow_module_level=True)

RULES = WindRules(
    version="0.0.1",
    states={"itest_source": "99"},
    wind_source="itest_source",
    admin_source="itest_admin",
)

EXTRA_UNIT_SQL = """
INSERT INTO staging.wind_unit
    (source_id, unit_key, valid_from_version_id, state_key, status, grid_location_key, geom)
VALUES ('itest_source', %s, %s, '99', %s, %s,
        CASE WHEN %s::float8 IS NULL THEN NULL
             ELSE ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 25832) END)
"""
TURBINE_SQL = """
INSERT INTO staging.osm_power_feature
    (source_id, layer, osm_key, valid_from_version_id, kind, power, geom, location)
SELECT 'itest_source', 'points', %(key)s, %(version)s, 'wind_turbine', 'generator',
       ST_Translate(geom, %(dx)s, %(dy)s), ST_Translate(geom, %(dx)s, %(dy)s)
FROM staging.wind_unit WHERE source_id = 'itest_source' AND unit_key = %(unit)s
"""


def stage_units_turbines_and_boundaries(conn: psycopg.Connection[Any], tmp_path: Path) -> int:
    _, version = prepared(conn, tmp_path)
    extra = [
        ("f1", "In Betrieb", "SEL1", 10.2000, 53.7000),
        ("f2", "In Betrieb", "SEL1", 10.2010, 53.7005),
        ("f3", "In Betrieb", "SEL1", 10.2900, 53.7000),  # about 6 km east of f1
        ("nopos", "In Betrieb", None, None, None),
    ]
    for key, status, farm, lon, lat in extra:
        conn.execute(EXTRA_UNIT_SQL, (key, version, status, farm, lon, lon, lat))
    for key, unit, dx, dy in (
        ("n9001", "w1", 3.0, 0.0),
        ("n9002", "w3", 60.0, 0.0),
        ("n9003", "w1", 20_000.0, 20_000.0),  # no unit anywhere near it
    ):
        conn.execute(
            TURBINE_SQL, {"key": key, "version": version, "unit": unit, "dx": dx, "dy": dy}
        )
    for unit, key in (("w1", "02000001"), ("w2", "02000001"), ("w3", "02000009")):
        conn.execute(
            "UPDATE staging.wind_unit SET municipality_key = %s"
            " WHERE source_id = 'itest_source' AND unit_key = %s",
            (key, unit),
        )
    alpha = box(conn, 9.95, 53.50, 10.05, 53.58)
    district = box(conn, 9.9, 53.4, 10.3, 53.8)
    admin = dataclasses.replace(sources.load_all()["bkg_vg250"], source_id="itest_admin")
    sources.register(conn, admin)
    publisher = BodyPublisher(
        boundaries_zip([(alpha, "02000001", "Alpha", "OBJ-a", 4)], [(district, "02000", "Kreis")])
    )
    fetch.fetch_source(conn, publisher.client, admin, tmp_path)
    load.load_source(conn, admin, tmp_path)
    diff.diff_source(conn, admin)
    (step,) = apply.apply_source(conn, admin).steps
    return step.version_id


UNITS_SQL = """
SELECT unit_key, status_group, position_class, reason, in_strict, in_likely, in_inclusive,
       osm_key, osm_distance_m, place_key
FROM core.wind_unit_current ORDER BY unit_key
"""


def test_a_build_judges_every_position_and_puts_units_into_the_scenarios(tmp_path: Path) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        admin_version = stage_units_turbines_and_boundaries(conn, tmp_path)
        result = windcore.build_wind_units(conn, RULES)
        assert result.source_versions["itest_admin"] == admin_version
        assert result.counts == {
            "units": 9,
            "strict": 4,
            "likely": 4,
            "inclusive": 6,
            "with_osm_turbine": 2,
            "with_place": 1,
            "osm_turbines": 3,
            "osm_turbines_without_unit": 1,
            "class_confirmed": 1,
            "class_plausible": 4,
            "class_doubtful": 2,
            "class_invalid": 1,
            "class_unlocated": 1,
            "status_existing": 7,
            "status_planned": 1,
            "status_retired": 1,
        }
        assert conn.execute(UNITS_SQL).fetchall() == [
            ("f1", "existing", "plausible", "register_only", True, True, True, None, None, None),
            ("f2", "existing", "plausible", "register_only", True, True, True, None, None, None),
            (
                "f3",
                "existing",
                "doubtful",
                "isolated_in_farm",
                False,
                False,
                True,
                None,
                None,
                None,
            ),
            ("gone", "retired", "plausible", "retired", False, False, False, None, None, None),
            (
                "nopos",
                "existing",
                "unlocated",
                "no_coordinate",
                False,
                False,
                False,
                None,
                None,
                None,
            ),
            (
                "placeholder",
                "existing",
                "invalid",
                "coordinate_flagged",
                False,
                False,
                False,
                None,
                None,
                None,
            ),
            (
                "w1",
                "existing",
                "confirmed",
                "osm_turbine",
                True,
                True,
                True,
                "n9001",
                3,
                "02000001",
            ),
            (
                "w2",
                "planned",
                "doubtful",
                "far_from_municipality",
                False,
                False,
                True,
                None,
                None,
                None,
            ),
            # a turbine 60 m away is evidence that is kept, but it does not confirm the position
            ("w3", "existing", "plausible", "register_only", True, True, True, "n9002", 60, None),
        ]
        distances = dict(
            conn.execute(
                "SELECT unit_key, coalesce(registered_distance_m, -1) FROM core.wind_unit_current"
                " WHERE unit_key IN ('w1', 'w2', 'w3')"
            ).fetchall()
        )
        assert distances["w1"] == 0
        assert distances["w2"] > 2000
        assert distances["w3"] == -1  # the boundaries lack the key that the register names
        gaps = dict(
            conn.execute(
                "SELECT unit_key, coalesce(farm_gap_m, -1) FROM core.wind_unit_current"
                " WHERE unit_key LIKE 'f%'"
            ).fetchall()
        )
        assert gaps["f1"] < 100
        assert gaps["f3"] > 5000


def test_a_rebuild_from_the_same_inputs_gives_the_same_hash_and_a_change_gives_another(
    tmp_path: Path,
) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        stage_units_turbines_and_boundaries(conn, tmp_path)
        first = windcore.build_wind_units(conn, RULES)
        second = windcore.build_wind_units(conn, RULES)
        assert second.build_id > first.build_id
        assert second.content_hash == first.content_hash
        row = conn.execute(
            "SELECT rules_version, parameters ->> 'confirm_m', counts ->> 'units'"
            " FROM core.wind_build WHERE wind_build_id = %s",
            (second.build_id,),
        ).fetchone()
        assert row == ("0.0.1", "25", "9")
        # only the newest build is current, and a change of a position changes the hash
        assert conn.execute(
            "SELECT count(DISTINCT wind_build_id) FROM core.wind_unit_current"
        ).fetchone() == (1,)
        conn.execute(
            "UPDATE staging.wind_unit SET status = 'Endgültig stillgelegt'"
            " WHERE source_id = 'itest_source' AND unit_key = 'w3'"
        )
        assert windcore.build_wind_units(conn, RULES).content_hash != first.content_hash


def test_the_confirm_distance_decides_what_counts_as_confirmation(tmp_path: Path) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        stage_units_turbines_and_boundaries(conn, tmp_path)
        strict = dataclasses.replace(RULES, version="0.0.2", confirm_m=2, match_m=100)
        result = windcore.build_wind_units(conn, strict)
        # the turbine 3 m from w1 no longer confirms it, so w1 is plausible and has no confirmation
        assert result.counts["class_confirmed"] == 0
        assert result.counts["with_osm_turbine"] == 2
        assert result.counts["strict"] == 4


def test_a_state_without_an_osm_extract_is_built_with_nothing_to_confirm_it(
    tmp_path: Path,
) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        stage_units_turbines_and_boundaries(conn, tmp_path)
        rules = dataclasses.replace(RULES, version="0.0.3", states={}, wind_states=("99",))
        result = windcore.build_wind_units(conn, rules)
        assert set(result.source_versions) == {"itest_admin", "wind_units"}
        assert result.counts["units"] == 9
        assert result.counts["osm_turbines"] == 0
        assert result.counts["class_confirmed"] == 0
        # w1 is now only plausible, like w3, f1, f2 and the retired unit
        assert result.counts["class_plausible"] == 5
        assert (result.counts["strict"], result.counts["likely"]) == (4, 4)


def test_a_turbine_that_two_extracts_hold_is_counted_once(tmp_path: Path) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        stage_units_turbines_and_boundaries(conn, tmp_path)
        sources.register(conn, dataclasses.replace(make_spec(), source_id="itest_source2"))
        version = conn.execute(
            "SELECT max(valid_from_version_id) FROM staging.osm_power_feature"
            " WHERE source_id = 'itest_source'"
        ).fetchone()
        assert version is not None
        # the same OSM object, as the extract of a neighbouring state holds it
        conn.execute(
            "INSERT INTO staging.osm_power_feature"
            " (source_id, layer, osm_key, valid_from_version_id, kind, power, geom, location)"
            " SELECT 'itest_source2', layer, osm_key, valid_from_version_id, kind, power, geom,"
            " location FROM staging.osm_power_feature"
            " WHERE source_id = 'itest_source' AND osm_key = 'n9001'"
        )
        params = {
            "osm_sources": ["itest_source", "itest_source2"],
            "osm_versions": [version[0], version[0]],
            "wind_source": "itest_source",
            "active": ["In Betrieb", "In Planung"],
            "match": 100,
        }
        assert conn.execute(windcore.TURBINE_SQL, params).fetchone() == (3, 1)


def test_the_build_transaction_is_repeatable_read_unless_one_is_open() -> None:
    with db.connect("app") as conn:
        with windcore.repeatable_read(conn), conn.transaction():
            level = conn.execute("SHOW transaction_isolation").fetchone()
            assert level == ("repeatable read",)
        assert conn.isolation_level is None  # left as it was
        with conn.transaction(force_rollback=True):
            conn.execute("SELECT 1")
            with windcore.repeatable_read(conn), conn.transaction():
                level = conn.execute("SHOW transaction_isolation").fetchone()
                assert level == ("read committed",)  # the open transaction decides


def test_the_application_role_cannot_change_a_build(tmp_path: Path) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        stage_units_turbines_and_boundaries(conn, tmp_path)
        windcore.build_wind_units(conn, RULES)
        for statement in (
            "UPDATE core.wind_build SET counts = '{}'",
            "DELETE FROM core.wind_build",
            "TRUNCATE core.wind_build",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute(statement)  # type: ignore[arg-type]


def test_the_owner_is_refused_too_by_a_trigger() -> None:
    with db.connect("owner") as conn, conn.transaction(force_rollback=True):
        conn.execute(
            "INSERT INTO core.wind_build"
            " (rules_version, parameters, source_versions, counts, content_hash)"
            " VALUES ('0.0.1', '{}', '{}', '{}', repeat('a', 64))"
        )
        for statement in (
            "UPDATE core.wind_build SET counts = '{}'",
            "DELETE FROM core.wind_build",
            "TRUNCATE core.wind_build CASCADE",
        ):
            with (
                pytest.raises(psycopg.errors.RestrictViolation, match="append-only"),
                conn.transaction(),
            ):
                conn.execute(statement)  # type: ignore[arg-type]
