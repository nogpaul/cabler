"""The command-line side of the later stages: what is printed, and when the exit code is not zero.

The stage functions are replaced by canned results, so these tests need no database.
"""

from pathlib import Path
from typing import Any

import pytest

from cabler import apply, diff, load, pipeline, terrain, validate
from cabler.apply import ApplyError
from cabler.diff import DiffError
from cabler.load import LoadError
from cabler.terrain import CatalogueError
from cabler.validate import ValidateError

Run = tuple[int, str, str]


@pytest.fixture(autouse=True)
def wired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_connection: Any) -> None:
    conn = fake_connection()
    monkeypatch.setattr(pipeline.db, "connect", lambda role, environ=None: conn)
    monkeypatch.setenv("CABLER_DATA_DIR", str(tmp_path))


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> Run:
    code = pipeline.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def fail_with(error: Exception) -> Any:
    def stage(*args: Any, **kwargs: Any) -> Any:
        raise error

    return stage


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (None, "unknown size"),
        (999, "999 B"),
        (1500, "1.5 KB"),
        (2_500_000, "2.5 MB"),
        (3 * 10**12, "3000.0 GB"),
    ],
)
def test_sizes_are_shown_in_human_units(size: int | None, expected: str) -> None:
    assert pipeline._human(size) == expected


def test_load_prints_the_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = load.LoadResult("osm_hamburg", "loaded", 5, {"points": 2, "lines": 1})
    monkeypatch.setattr(pipeline.load, "load_source", lambda conn, spec, raw: result)
    code, out, _ = run(["load", "dev", "osm_hamburg"], capsys)
    assert code == 0
    assert "loaded" in out
    assert "points 2, lines 1" in out


def test_a_skipped_load_says_nothing_is_new(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = load.LoadResult("osm_hamburg", "skipped", 5, {})
    monkeypatch.setattr(pipeline.load, "load_source", lambda conn, spec, raw: result)
    code, out, _ = run(["load", "dev", "osm_hamburg"], capsys)
    assert code == 0
    assert "nothing new" in out


def test_a_failed_load_is_reported_and_the_exit_code_is_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pipeline.load, "load_source", fail_with(LoadError("file changed")))
    code, _, err = run(["load", "dev", "osm_hamburg"], capsys)
    assert code == 1
    assert "ERROR osm_hamburg: file changed" in err


def diff_step(previous: int | None) -> diff.DiffStep:
    return diff.DiffStep(
        version_id=6,
        previous_version_id=previous,
        added=3417 if previous is None else 1,
        changed=0 if previous is None else 2,
        removed=0 if previous is None else 3,
        total_before=0 if previous is None else 10,
        total_after=3417 if previous is None else 8,
        changed_fields={},
    )


def test_diff_names_a_first_snapshot_and_summarises_later_ones(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = diff.DiffResult("osm_hamburg", "diffed", 7, (diff_step(None), diff_step(6)))
    monkeypatch.setattr(pipeline.diff, "diff_source", lambda conn, spec: result)
    code, out, _ = run(["diff", "dev", "osm_hamburg"], capsys)
    assert code == 0
    assert "first snapshot: 3,417 objects" in out
    assert "+1 added  ~2 changed  -3 removed" in out
    assert "(of 10 before, 8 now)" in out


def test_diff_with_nothing_pending_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = diff.DiffResult("osm_hamburg", "skipped", 7, ())
    monkeypatch.setattr(pipeline.diff, "diff_source", lambda conn, spec: result)
    code, out, _ = run(["diff", "dev", "osm_hamburg"], capsys)
    assert code == 0
    assert "nothing new to compare" in out


def test_a_refused_diff_exits_with_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pipeline.diff, "diff_source", fail_with(DiffError("no loader")))
    code, _, err = run(["diff", "dev", "osm_hamburg"], capsys)
    assert code == 1
    assert "ERROR osm_hamburg: no loader" in err


def apply_step(flags: dict[str, int]) -> apply.ApplyStep:
    return apply.ApplyStep(
        version_id=6,
        opened=4,
        closed=2,
        current=10,
        kinds={"line": 6, "substation": 4},
        flags=flags,
    )


def test_apply_shows_counts_kinds_and_findings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = apply.ApplyResult("osm_hamburg", "applied", 6, (apply_step({"voltage_missing": 1}),))
    monkeypatch.setattr(pipeline.apply, "apply_source", lambda conn, spec: result)
    code, out, _ = run(["apply", "dev", "osm_hamburg"], capsys)
    assert code == 0
    assert "+4 opened  -2 closed  (10 current)" in out
    assert "kinds: line 6, substation 4" in out
    assert "findings: voltage_missing 1" in out


def test_apply_without_findings_says_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = apply.ApplyResult("osm_hamburg", "applied", 6, (apply_step({}),))
    monkeypatch.setattr(pipeline.apply, "apply_source", lambda conn, spec: result)
    _, out, _ = run(["apply", "dev", "osm_hamburg"], capsys)
    assert "findings: none" in out


def test_apply_with_nothing_diffed_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = apply.ApplyResult("osm_hamburg", "skipped", 6, ())
    monkeypatch.setattr(pipeline.apply, "apply_source", lambda conn, spec: result)
    code, out, _ = run(["apply", "dev", "osm_hamburg"], capsys)
    assert code == 0
    assert "nothing new to apply" in out


def test_a_refused_apply_exits_with_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pipeline.apply, "apply_source", fail_with(ApplyError("no applier")))
    code, _, err = run(["apply", "dev", "osm_hamburg"], capsys)
    assert code == 1
    assert "ERROR osm_hamburg: no applier" in err


def validate_step(outcome: str) -> validate.ValidateStep:
    quarantined = outcome == "quarantined"
    return validate.ValidateStep(
        version_id=6,
        outcome=outcome,
        errors=1 if quarantined else 0,
        warnings=2,
        total=10,
        rules={"voltage_hint_line": 3},
        problems=("removal_share: too many",) if quarantined else (),
    )


def test_a_passed_snapshot_exits_with_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = validate.ValidateResult("osm_hamburg", "validated", 6, (validate_step("passed"),))
    monkeypatch.setattr(pipeline.validate, "validate_source", lambda conn, spec: result)
    code, out, _ = run(["validate", "dev", "osm_hamburg"], capsys)
    assert code == 0
    assert "PASSED" in out
    assert "0 errors, 2 warnings  (10 objects)" in out
    assert "findings: voltage_hint_line 3" in out


def test_a_quarantined_snapshot_exits_with_one_and_names_the_problem(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = validate.ValidateResult("osm_hamburg", "validated", 6, (validate_step("quarantined"),))
    monkeypatch.setattr(pipeline.validate, "validate_source", lambda conn, spec: result)
    code, out, _ = run(["validate", "dev", "osm_hamburg"], capsys)
    assert code == 1
    assert "QUARANTINED" in out
    assert "problem: removal_share: too many" in out


def test_validate_with_nothing_new_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = validate.ValidateResult("osm_hamburg", "skipped", 6, ())
    monkeypatch.setattr(pipeline.validate, "validate_source", lambda conn, spec: result)
    code, out, _ = run(["validate", "dev", "osm_hamburg"], capsys)
    assert code == 0
    assert "nothing new to validate" in out


def test_a_refused_validate_exits_with_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        pipeline.validate, "validate_source", fail_with(ValidateError("no validator"))
    )
    code, _, err = run(["validate", "dev", "osm_hamburg"], capsys)
    assert code == 1
    assert "ERROR osm_hamburg: no validator" in err


def test_review_without_a_validated_snapshot_explains_what_to_do(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pipeline.validate, "review_counts", lambda conn, source: {})
    code, out, _ = run(["review", "dev", "osm_hamburg"], capsys)
    assert code == 0
    assert "nothing to review" in out


def test_review_lists_the_evidence_for_each_substation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [
        ("n101", "hv_candidate", "Auf der Leitung", "", "", 110000, 0.0, None, 10.0, 53.55),
        (
            "n103",
            "likely_low_voltage",
            "",
            "Netz",
            "minor_distribution",
            None,
            None,
            "medium_low",
            10.1,
            53.6,
        ),
        ("n105", "no_evidence", "", "", "", None, None, None, 10.12, 53.62),
    ]
    monkeypatch.setattr(
        pipeline.validate,
        "review_counts",
        lambda conn, source: {"hv_candidate": 1, "no_evidence": 1},
    )
    monkeypatch.setattr(
        pipeline.validate, "review_rows", lambda conn, source, suggestion, limit: rows
    )
    code, out, _ = run(["review", "dev", "osm_hamburg", "--limit", "5"], capsys)
    assert code == 0
    assert "hv_candidate 1, no_evidence 1" in out
    assert "line 110,000 V at 0.0 m" in out
    assert "type minor_distribution -> medium_low" in out
    assert "no evidence" in out


def test_review_passes_the_suggestion_filter_on(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[str | None, int]] = []

    def rows(conn: Any, source: str, suggestion: str | None, limit: int) -> list[Any]:
        seen.append((suggestion, limit))
        return []

    monkeypatch.setattr(pipeline.validate, "review_counts", lambda conn, source: {"conflict": 1})
    monkeypatch.setattr(pipeline.validate, "review_rows", rows)
    run(["review", "dev", "osm_hamburg", "--suggestion", "conflict"], capsys)
    assert seen == [("conflict", 20)]


def catalogue_result(unreadable: dict[str, str] | None = None) -> terrain.CatalogueResult:
    return terrain.CatalogueResult(
        source_id="nrw_dgm1",
        tiles=35860,
        new=12,
        flags={"off_grid": 2},
        duplicate_corners=0,
        overlaps=0,
        holes=1,
        hole_km2=1.0,
        extent=(280000.0, 5652000.0, 282000.0, 5653000.0),
        area_km2=35860.0,
        unreadable=unreadable or {},
    )


def test_catalogue_prints_the_counts_the_extent_the_findings_and_the_set_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        pipeline.terrain, "catalogue_source", lambda conn, spec, raw: catalogue_result()
    )
    code, out, _ = run(["catalogue", "dev", "nrw_dgm1"], capsys)
    assert code == 0
    assert "35,860 tiles (12 new)" in out
    assert "extent: 280,000 5,652,000 to 282,000 5,653,000 (EPSG:25832)" in out
    assert "findings: off_grid 2" in out
    assert "0 duplicate corners, 0 overlaps, 1 holes (1.0 km2)" in out


def test_catalogue_names_the_tiles_it_could_not_read_and_exits_with_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = catalogue_result({"dgm1_broken.tif": "not a TIFF file"})
    monkeypatch.setattr(pipeline.terrain, "catalogue_source", lambda conn, spec, raw: result)
    code, out, _ = run(["catalogue", "dev", "nrw_dgm1"], capsys)
    assert code == 1
    assert "unreadable: 1 tiles (not catalogued)" in out
    assert "dgm1_broken.tif: not a TIFF file" in out


def test_a_refused_catalogue_exits_with_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pipeline.terrain, "catalogue_source", fail_with(CatalogueError("no tiles")))
    code, _, err = run(["catalogue", "dev", "nrw_dgm1"], capsys)
    assert code == 1
    assert "ERROR nrw_dgm1: no tiles" in err
