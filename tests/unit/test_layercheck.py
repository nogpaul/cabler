"""The exit check: severities, the pass over a layer, the verdict check, the stored report.

A connection that answers each query by its text stands in for the database; the real queries run
in tests/integration/test_layercheck_db.py against seeded defects.
"""

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from cabler import layercheck
from cabler.layercheck import CheckResult, Layer, Report

PLAIN = Layer(
    "demo", "staging.demo", "geom", "t.valid_to_version_id IS NULL", "valid_from_version_id"
)


@pytest.mark.parametrize(
    ("bad", "total", "rule", "expected"),
    [
        (0, 100, "error", "ok"),
        (1, 100, "error", "error"),
        (1, 100, "warning", "warning"),
        (0, 100, (0.0, 0.01), "ok"),
        (1, 1000, (0.0, 0.01), "warning"),
        (10, 1000, (0.0, 0.01), "warning"),
        (11, 1000, (0.0, 0.01), "error"),
        (5, 1000, (0.01, 0.10), "info"),  # below the warning share, but not nothing
    ],
)
def test_a_severity_follows_the_rule(bad: int, total: int, rule: Any, expected: str) -> None:
    assert layercheck.severity_of(bad, total, rule) == expected


def test_one_scan_counts_every_standard_check_of_a_layer_in_one_pass() -> None:
    sql = layercheck.scan_sql(PLAIN, validity=True)
    assert sql.count("FROM staging.demo t") == 1
    assert "LEFT JOIN meta.source_version v ON v.version_id = t.valid_from_version_id" in sql
    assert "NOT ST_IsValid(t.geom)" in sql
    assert "ST_SRID(t.geom) <> 25832" in sql
    assert "GROUP BY t.source_id" in sql


def test_the_validity_scan_can_be_skipped_and_a_layer_without_versions_has_no_join() -> None:
    layer = Layer("terrain", "staging.t", "footprint", "t.is_current", None)
    sql = layercheck.scan_sql(layer, validity=False)
    assert "ST_IsValid" not in sql
    assert "meta.source_version" not in sql


def test_the_extra_checks_of_a_layer_come_after_the_standard_ones() -> None:
    extra = layercheck.Extra("odd", "t.x IS NULL", "error", "odd rows")
    sql = layercheck.scan_sql(
        Layer("l", "staging.l", "geom", "true", None, extras=(extra,)), validity=True
    )
    assert sql.index("t.x IS NULL") > sql.index("ST_MakeEnvelope")


def test_the_counts_of_a_scan_become_results_with_their_severities() -> None:
    row = ("alkis_nrw", 1_000, 0, 0, 3, 5, 0, 1)
    results = layercheck._scan_results(PLAIN, [row])
    by_check = {r.check: r for r in results}
    assert by_check["geometry_missing"].severity == "ok"
    assert (by_check["geometry_invalid"].severity, by_check["geometry_invalid"].bad) == ("error", 3)
    assert by_check["outside_germany"].severity == "warning"  # 0.5% is below the error share
    assert by_check["lineage_snapshot_unsealed"].severity == "error"
    assert by_check["geometry_invalid"].message
    assert by_check["crs_not_25832"].message == ""  # nothing to explain when nothing is bad


def test_wind_units_without_a_coordinate_are_a_warning_and_other_layers_an_error() -> None:
    wind = Layer("wind_unit", "staging.wind_unit", "geom", "true", None, null_geom="warning")
    row = ("mastr_bulk", 100, 4, 0, 0, 0, 0, 0)
    assert layercheck._scan_results(wind, [row])[0].severity == "warning"
    assert layercheck._scan_results(PLAIN, [row])[0].severity == "error"


def test_an_extra_check_is_judged_by_its_own_rule() -> None:
    extra = layercheck.Extra("odd", "t.x IS NULL", "error", "odd rows")
    layer = Layer("l", "staging.l", "geom", "true", None, extras=(extra,))
    results = layercheck._scan_results(layer, [("s", 10, 0, 0, 0, 0, 0, 0, 2)])
    assert results[-1] == CheckResult("l", "s", "odd", "error", 2, 10, "odd rows")


def test_quality_flags_are_warnings_except_the_repair_that_the_apply_stage_is_for() -> None:
    rows = [("alkis_nrw", "area_mismatch", 14), ("alkis_nrw", "geometry_repaired", 3)]
    results = layercheck._flag_results(PLAIN, rows, {"alkis_nrw": 100})
    assert [(r.check, r.severity) for r in results] == [
        ("flag:area_mismatch", "warning"),
        ("flag:geometry_repaired", "info"),
    ]
    assert results[0].total == 100


@pytest.mark.parametrize(
    ("newest", "judged", "outcome", "severity"),
    [
        (5, 5, "passed", "ok"),
        (5, 5, "quarantined", "error"),
        (5, 4, "passed", "error"),  # a newer snapshot has no verdict yet
        (5, None, None, "error"),  # never judged
        (None, None, None, "error"),
    ],
)
def test_the_newest_snapshot_needs_a_passed_verdict(
    newest: int | None, judged: int | None, outcome: str | None, severity: str
) -> None:
    assert (
        layercheck.verdict_result("osm_power", "osm_nrw", newest, judged, outcome).severity
        == severity
    )


class Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None


class Database:
    """Answers the queries of the exit check; ``scan`` holds the rows of the layer scan."""

    def __init__(self, scan: list[tuple[Any, ...]], **answers: Any) -> None:
        self.scan = scan
        self.answers = answers
        self.run: tuple[Any, ...] | None = None
        self.stored: list[tuple[Any, ...]] = []

    @contextmanager
    def transaction(self) -> Any:
        yield

    @contextmanager
    def cursor(self) -> Any:
        outer = self

        class Cursor:
            def executemany(self, query: str, rows: list[tuple[Any, ...]]) -> None:
                assert query == layercheck.RESULT_SQL
                outer.stored = rows

        yield Cursor()

    def execute(self, query: str, params: Any = None) -> Result:
        if query.startswith("SELECT t.source_id, count(*),"):
            return Result(self.scan)
        if query.startswith("SELECT t.source_id, f, count(*)"):
            return Result(self.answers.get("flags", []))
        if query == layercheck.LICENCE_SQL:
            return Result(self.answers.get("licence", []))
        if query == layercheck.VERDICT_SQL:
            return Result(self.answers.get("verdict", []))
        if query == layercheck.FILE_SQL:
            return Result(self.answers.get("files", []))
        if query == layercheck.RUN_SQL:
            self.run = params
            return Result([(41,)])
        raise AssertionError(query)


def run(db: Database, layer: Layer = PLAIN, **options: Any) -> Report:
    return layercheck.run_checks(db, (layer,), **options)  # type: ignore[arg-type]


def test_a_clean_layer_passes() -> None:
    db = Database([("alkis_nrw", 1_000, 0, 0, 0, 0, 0, 0)])
    report = run(db, files=False)
    assert report.passed
    assert (report.errors, report.warnings) == (0, 0)


def test_an_empty_layer_is_an_error() -> None:
    report = run(Database([]))
    assert [r.check for r in report.results] == ["layer_not_empty"]
    assert not report.passed


def test_a_source_without_a_licence_fails_the_check() -> None:
    db = Database([("alkis_nrw", 10, 0, 0, 0, 0, 0, 0)], licence=[("alkis_nrw",)])
    report = run(db, files=False)
    assert [r.check for r in report.results if r.severity == "error"] == ["licence_missing"]


def test_a_layer_with_a_validate_stage_needs_the_verdict_of_every_source() -> None:
    layer = Layer("osm_power", "staging.p", "geom", "true", "v", verdict=True)
    db = Database([("osm_nrw", 10, 0, 0, 0, 0, 0, 0)], verdict=[("osm_nrw", 5, None, None)])
    report = run(db, layer, files=False)
    assert [(r.check, r.severity) for r in report.results if r.check == "verdict"] == [
        ("verdict", "error")
    ]


def test_a_snapshot_file_that_is_gone_is_only_a_warning() -> None:
    db = Database(
        [("alkis_nrw", 10, 0, 0, 0, 0, 0, 0)],
        files=[("alkis_nrw", "/data/gone.zip"), ("other", "/data/there.zip")],
    )
    report = run(db, exists=lambda path: path.endswith("there.zip"))
    gone = [r for r in report.results if r.check == "snapshot_file" and r.severity != "ok"]
    assert [(r.source_id, r.severity) for r in gone] == [("alkis_nrw", "warning")]
    assert report.passed
    assert "/data/gone.zip" in gone[0].message


def test_a_snapshot_is_looked_for_in_the_folder_of_the_raw_data(tmp_path: Path) -> None:
    (tmp_path / "alkis_nrw" / "20261109T144636Z_c25d").mkdir(parents=True)
    db = Database(
        [("alkis_nrw", 10, 0, 0, 0, 0, 0, 0)],
        files=[("alkis_nrw", "alkis_nrw/20261109T144636Z_c25d"), ("other", "other/gone")],
    )
    report = run(db, raw=tmp_path)
    states = {r.source_id: r.severity for r in report.results if r.check == "snapshot_file"}
    assert states == {"alkis_nrw": "ok", "other": "warning"}


def test_the_flags_of_a_layer_are_reported_with_the_rows_of_their_source() -> None:
    db = Database(
        [("alkis_nrw", 1_000, 0, 0, 0, 0, 0, 0)], flags=[("alkis_nrw", "area_mismatch", 140)]
    )
    report = run(db, files=False)
    flag = next(r for r in report.results if r.check == "flag:area_mismatch")
    assert (flag.bad, flag.total, flag.severity) == (140, 1_000, "warning")
    assert report.passed


def test_a_report_is_stored_as_one_run_with_every_result() -> None:
    results = (
        CheckResult("demo", "s", "a", "ok", 0, 10),
        CheckResult("demo", "s", "b", "warning", 2, 10, "two"),
    )
    db = Database([])
    stored = layercheck.store_report
    run_id = stored(db, Report(results), {"validity": True})  # type: ignore[arg-type]
    assert run_id == 41
    assert db.run is not None
    assert db.run[:3] == ("passed", 0, 1)
    assert [row[1:5] for row in db.stored] == [
        ("demo", "s", "a", "ok"),
        ("demo", "s", "b", "warning"),
    ]
    assert {row[0] for row in db.stored} == {41}


def test_a_failed_report_is_stored_as_failed() -> None:
    db = Database([])
    report = Report((CheckResult("demo", "s", "a", "error", 1, 10),))
    layercheck.store_report(db, report, {})  # type: ignore[arg-type]
    assert db.run is not None
    assert db.run[:3] == ("failed", 1, 0)


def test_the_printed_report_lists_what_is_not_ok_under_its_source() -> None:
    report = Report(
        (
            CheckResult("parcel", "alkis_nrw", "geometry_invalid", "ok", 0, 1_000),
            CheckResult(
                "parcel", "alkis_nrw", "flag:area_mismatch", "warning", 140, 1_000, "flagged"
            ),
            CheckResult("parcel", "alkis_hh", "crs_not_25832", "error", 3, 50),
        )
    )
    text = "\n".join(layercheck.format_report(report))
    assert "parcel" in text
    assert "alkis_nrw" in text
    assert "1,000 rows  WARNING" in text
    assert "flag:area_mismatch" in text
    assert "(14.0%)" in text
    assert "geometry_invalid" not in text  # ok results stay out of the print
    assert "50 rows  ERROR" in text
    assert text.endswith("exit check FAILED: 1 errors, 1 warnings")
