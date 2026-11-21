"""The exit check against a real database, with defects that were put there on purpose.

Every test looks only at rows of its own source (the check of a layer is narrowed with its
``current`` condition), so real data in the database never changes the result.

Run with:  CABLER_ENV=dev pytest -m integration   (after `python -m cabler.migrate up dev`)
"""

import dataclasses
import os
from pathlib import Path
from typing import Any

import psycopg
import pytest

from cabler import core, db, layercheck, pipeline, sources, validate, windcore
from cabler.core import CoreError
from cabler.layercheck import LAYERS, Layer
from itest_helpers import make_spec, prepared

pytestmark = pytest.mark.integration

if "CABLER_ENV" not in os.environ:
    pytest.skip("CABLER_ENV not set", allow_module_level=True)

SQUARE = "POLYGON((500000 5700000, 500100 5700000, 500100 5700100, 500000 5700100, 500000 5700000))"
BOWTIE = "POLYGON((500000 5700000, 500100 5700100, 500100 5700000, 500000 5700100, 500000 5700000))"
FAR_AWAY = "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))"


def layer_of(name: str, source_id: str) -> Layer:
    """The layer of that name, narrowed to the rows of one source."""
    layer = next(x for x in LAYERS if x.name == name)
    return dataclasses.replace(layer, current=f"{layer.current} AND t.source_id = '{source_id}'")


def register(conn: psycopg.Connection[Any], source_id: str) -> None:
    sources.register(conn, dataclasses.replace(make_spec(), source_id=source_id))


def snapshot(conn: psycopg.Connection[Any], source_id: str, sha: str | None, status: str) -> int:
    row = conn.execute(
        "INSERT INTO meta.source_version (source_id, remote_url, sha256, status)"
        " VALUES (%s, 'https://example.org/data', %s, %s) RETURNING version_id",
        (source_id, sha, status),
    ).fetchone()
    assert row is not None
    return int(row[0])


def land_cover(
    conn: psycopg.Connection[Any],
    source_id: str,
    key: str,
    version: int,
    code: str,
    wkt: str | None,
) -> None:
    conn.execute(
        "INSERT INTO staging.land_cover"
        " (source_id, feature_key, valid_from_version_id, clc_code, geom)"
        " VALUES (%s, %s, %s, %s, CASE WHEN %s::text IS NULL THEN NULL"
        " ELSE ST_Multi(ST_GeomFromText(%s, 25832)) END)",
        (source_id, key, version, code, wkt, wkt),
    )


def results_of(report: layercheck.Report) -> dict[str, layercheck.CheckResult]:
    return {r.check: r for r in report.results}


def test_the_defects_of_a_layer_are_found_and_the_clean_rows_are_not_blamed() -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        register(conn, "itest_check")
        sealed = snapshot(conn, "itest_check", "a" * 64, "applied")
        unsealed = snapshot(conn, "itest_check", None, "applied")
        land_cover(conn, "itest_check", "ok", sealed, "211", SQUARE)
        land_cover(conn, "itest_check", "bowtie", sealed, "211", BOWTIE)
        land_cover(conn, "itest_check", "nogeom", sealed, "211", None)
        land_cover(conn, "itest_check", "unknown", sealed, "000", SQUARE)
        land_cover(conn, "itest_check", "unsealed", unsealed, "211", SQUARE)
        land_cover(conn, "itest_check", "abroad", sealed, "211", FAR_AWAY)

        report = layercheck.run_checks(conn, (layer_of("land_cover", "itest_check"),), files=False)
        found = results_of(report)
        expected = {
            "geometry_missing": ("error", 1),
            "crs_not_25832": ("ok", 0),
            "geometry_invalid": ("error", 1),
            "outside_germany": ("error", 1),  # one of six is far above 1%
            "lineage_snapshot_missing": ("ok", 0),
            "lineage_snapshot_unsealed": ("error", 1),
            "clc_code_unknown": ("error", 1),
        }
        assert expected.items() <= {k: (r.severity, r.bad) for k, r in found.items()}.items()
        assert found["geometry_missing"].total == 6
        assert not report.passed
        assert report.errors == 5


def test_the_same_layer_without_the_defects_passes() -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        register(conn, "itest_check")
        sealed = snapshot(conn, "itest_check", "b" * 64, "applied")
        land_cover(conn, "itest_check", "one", sealed, "211", SQUARE)
        land_cover(conn, "itest_check", "two", sealed, "312", SQUARE)
        report = layercheck.run_checks(conn, (layer_of("land_cover", "itest_check"),), files=False)
        assert report.passed
        assert (report.errors, report.warnings) == (0, 0)


def test_a_tile_in_another_reference_system_is_an_error() -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        register(conn, "itest_tiles")
        conn.execute(
            "INSERT INTO staging.terrain_tile"
            " (source_id, name, file_sha256, file_size, epsg, width_px, height_px, pixel_x,"
            "  pixel_y, x_min, y_min, x_max, y_max, bits_per_sample, sample_format, compression,"
            "  footprint)"
            " VALUES ('itest_tiles', 't1.tif', repeat('c', 64), 10, 4326, 1000, 1000, 1, 1,"
            "  500000, 5700000, 501000, 5701000, 32, 3, 8,"
            "  ST_GeomFromText(%s, 25832))",
            (
                "POLYGON((500000 5700000, 501000 5700000, 501000 5701000,"
                " 500000 5701000, 500000 5700000))",
            ),
        )
        report = layercheck.run_checks(
            conn, (layer_of("terrain_tile", "itest_tiles"),), files=False
        )
        assert results_of(report)["tile_not_25832"].severity == "error"
        assert results_of(report)["geometry_invalid"].severity == "ok"
        assert "lineage_snapshot_missing" not in results_of(report)  # nothing to check, no result


def test_a_geometry_that_reaches_out_of_germany_is_found_and_a_skipped_scan_says_so() -> None:
    reaching = (
        "POLYGON((269900 5700000, 270100 5700000, 270100 5700100, 269900 5700100, 269900 5700000))"
    )
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        register(conn, "itest_check")
        sealed = snapshot(conn, "itest_check", "a" * 64, "applied")
        land_cover(conn, "itest_check", "inside", sealed, "211", SQUARE)
        land_cover(conn, "itest_check", "reaching", sealed, "211", reaching)
        land_cover(conn, "itest_check", "bowtie", sealed, "211", BOWTIE)
        layer = layer_of("land_cover", "itest_check")
        full = layercheck.run_checks(conn, (layer,), files=False)
        assert results_of(full)["outside_germany"].bad == 1  # the box of one reaches over the edge
        assert results_of(full)["geometry_invalid"].bad == 1
        fast = layercheck.run_checks(conn, (layer,), files=False, validity=False)
        found = results_of(fast)["geometry_invalid"]
        assert (found.severity, found.bad) == ("info", 0)
        assert "skipped" in found.message
        assert fast.partial
        assert fast.isolation == "read committed"  # inside the outer transaction of the test


def test_an_empty_geometry_is_counted_as_a_missing_one() -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        register(conn, "itest_check")
        sealed = snapshot(conn, "itest_check", "a" * 64, "applied")
        land_cover(conn, "itest_check", "inside", sealed, "211", SQUARE)
        land_cover(conn, "itest_check", "empty", sealed, "211", "POLYGON EMPTY")
        report = layercheck.run_checks(conn, (layer_of("land_cover", "itest_check"),), files=False)
        found = results_of(report)["geometry_missing"]
        assert (found.severity, found.bad, found.total) == ("error", 1, 2)
        assert results_of(report)["outside_germany"].bad == 0  # an empty box is nowhere


def test_a_refused_store_on_a_real_connection_still_shows_the_findings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real_connect, real_run = db.connect, layercheck.run_checks
    # the read-only role may read every layer, but it may not write a report
    monkeypatch.setattr(pipeline.db, "connect", lambda role, environ=None: real_connect("ro"))
    monkeypatch.setattr(
        pipeline.layercheck,
        "run_checks",
        lambda conn, **kwargs: real_run(conn, (layer_of("land_cover", "itest_none"),), **kwargs),
    )
    code = pipeline._run_exitcheck("dev", fast=True, files=False)
    out, err = capsys.readouterr()
    assert code == 1
    assert "layer_not_empty" in out  # printed before the store was tried
    assert "ERROR exitcheck: the report could not be stored: permission denied" in err


def test_a_source_with_an_applied_snapshot_and_no_rows_is_found() -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        register(conn, "itest_check")
        snapshot(conn, "itest_check", "a" * 64, "applied")  # applied, and nothing was loaded
        report = layercheck.run_checks(
            conn,
            (layer_of("land_cover", "itest_check"),),
            files=False,
            expected={"land_cover": frozenset({"itest_check", "itest_never_applied"})},
        )
        checks = [(r.check, r.source_id) for r in report.results]
        assert checks == [("layer_not_empty", "-"), ("source_missing", "itest_check")]


def test_a_snapshot_without_a_verdict_is_an_error_until_it_is_judged(tmp_path: Path) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        prepared(conn, tmp_path)  # itest_source: applied and validated
        layer = layer_of("osm_power", "itest_source")
        before = layercheck.run_checks(conn, (layer,), files=False)
        assert results_of(before)["verdict"].severity == "ok"
        snapshot(conn, "itest_source", "d" * 64, "applied")  # a newer snapshot that nobody judged
        after = layercheck.run_checks(conn, (layer,), files=False)
        assert results_of(after)["verdict"].severity == "error"
        assert "run validate" in results_of(after)["verdict"].message


def test_the_register_of_wind_units_is_judged_like_the_osm_snapshots() -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        register(conn, "itest_wind")
        version = snapshot(conn, "itest_wind", "e" * 64, "applied")
        # 1,000 units, 5 of them with a placeholder coordinate: 0.5% is a warning
        conn.execute(
            "INSERT INTO staging.wind_unit (source_id, unit_key, valid_from_version_id, qa_flags)"
            " SELECT 'itest_wind', 'u' || n, %s,"
            "        CASE WHEN n <= 5 THEN ARRAY['coordinate_placeholder'] ELSE '{}' END"
            " FROM generate_series(1, 1000) AS n",
            (version,),
        )
        step = validate.validate_wind_version(conn, "itest_wind", version)
        assert (step.outcome, step.errors, step.warnings) == ("passed", 0, 1)
        assert step.rules == {"coordinate_placeholder": 1}

        # 1,000 more units without a coordinate: half of the register is far above the 30% limit
        version2 = snapshot(conn, "itest_wind", "f" * 64, "applied")
        conn.execute(
            "INSERT INTO staging.wind_unit (source_id, unit_key, valid_from_version_id, qa_flags)"
            " SELECT 'itest_wind', 'm' || n, %s, ARRAY['coordinate_missing']"
            " FROM generate_series(1, 1000) AS n",
            (version2,),
        )
        failed = validate.validate_wind_version(conn, "itest_wind", version2)
        assert failed.outcome == "quarantined"
        assert "coordinate_missing" in failed.rules


def test_the_wind_core_needs_a_passed_verdict_for_the_register_too(tmp_path: Path) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        prepared(conn, tmp_path)  # the OSM source is validated
        register(conn, "itest_register")  # the register of the units is not
        rules = windcore.WindRules(
            version="0.0.9",
            states={"itest_source": "99"},
            wind_source="itest_register",
            admin_source="itest_admin",
        )
        with pytest.raises(CoreError, match="itest_register: no validated snapshot"):
            windcore.build_wind_units(conn, rules)


def test_the_build_on_a_plain_connection_runs_at_repeatable_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def spy(conn: psycopg.Connection[Any], source_ids: Any) -> dict[str, int]:
        row = conn.execute("SHOW transaction_isolation").fetchone()
        assert row is not None
        seen.append(str(row[0]))
        raise CoreError("stop here: only the level of the transaction matters")

    monkeypatch.setattr(core, "validated_sources", spy)
    rules = windcore.WindRules(version="0.0.9", states={"itest_source": "99"})
    with db.connect("app") as conn, pytest.raises(CoreError, match="stop here"):
        windcore.build_wind_units(conn, rules, require_snapshot=True)
    assert seen == ["repeatable read"]


def test_a_stored_report_can_be_read_back_and_never_changed() -> None:
    results = (
        layercheck.CheckResult("demo", "s", "a", "ok", 0, 10),
        layercheck.CheckResult("demo", "s", "b", "warning", 2, 10, "two"),
    )
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        run_id = layercheck.store_report(conn, layercheck.Report(results), {"validity": True})
        rows = conn.execute(
            "SELECT layer, source_id, check_name, severity, bad, total FROM meta.check_result"
            " WHERE check_run_id = %s ORDER BY check_result_id",
            (run_id,),
        ).fetchall()
        assert rows == [("demo", "s", "a", "ok", 0, 10), ("demo", "s", "b", "warning", 2, 10)]
        assert conn.execute(
            "SELECT outcome, errors, warnings FROM meta.check_run WHERE check_run_id = %s",
            (run_id,),
        ).fetchone() == ("passed", 0, 1)
        assert conn.execute("SELECT count(*) FROM meta.check_result_current").fetchone() == (2,)
        for statement in (
            "UPDATE meta.check_run SET errors = 9",
            "DELETE FROM meta.check_result",
            "TRUNCATE meta.check_run",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute(statement)  # type: ignore[arg-type]


def test_the_owner_is_refused_too_by_a_trigger() -> None:
    with db.connect("owner") as conn, conn.transaction(force_rollback=True):
        conn.execute(
            "INSERT INTO meta.check_run (outcome, errors, warnings) VALUES ('passed', 0, 0)"
        )
        for statement in (
            "UPDATE meta.check_run SET warnings = 1",
            "DELETE FROM meta.check_run",
            "TRUNCATE meta.check_run CASCADE",
        ):
            with (
                pytest.raises(psycopg.errors.RestrictViolation, match="append-only"),
                conn.transaction(),
            ):
                conn.execute(statement)  # type: ignore[arg-type]
