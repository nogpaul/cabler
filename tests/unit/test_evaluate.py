"""The statistics, the report and the flow of the rule evaluation, without a database.

The SQL itself is tested against a database in tests/integration/test_core_db.py.
"""

from typing import Any

import pytest

from cabler import core, evaluate
from cabler.core import CoreError, RuleSet
from cabler.evaluate import Cell, Evaluation


@pytest.mark.parametrize(
    ("k", "n", "low", "high"),
    [
        (0, 10, 0.0, 0.2775),
        (10, 10, 0.7225, 1.0),
        (5, 10, 0.2366, 0.7634),
        (81, 100, 0.7222, 0.8749),
        (1, 2, 0.0945, 0.9055),
        (980, 1234, 0.7707, 0.8158),
    ],
)
def test_the_wilson_interval_matches_the_published_formula(
    k: int, n: int, low: float, high: float
) -> None:
    got = evaluate.wilson(k, n)
    assert got == pytest.approx((low, high), abs=5e-5)


def test_nothing_counted_means_nothing_is_known() -> None:
    assert evaluate.wilson(0, 0) == (0.0, 1.0)


def test_the_interval_stays_inside_zero_and_one_and_narrows_with_more_cases() -> None:
    for k, n in [(0, 1), (1, 1), (0, 1000), (1000, 1000), (3, 7)]:
        low, high = evaluate.wilson(k, n)
        assert 0.0 <= low <= k / n <= high <= 1.0
    small = evaluate.wilson(5, 10)
    large = evaluate.wilson(500, 1000)
    assert large[1] - large[0] < small[1] - small[0]


def test_a_cell_knows_its_share_and_its_interval() -> None:
    cell = Cell("osm_nrw", "hv_candidate", 10, 5)
    assert cell.share == 0.5
    assert cell.interval == evaluate.wilson(5, 10)
    empty = Cell("osm_nrw", "conflict", 0, 0)
    assert empty.share is None
    assert empty.interval == (0.0, 1.0)


def report(*, build_id: int | None = 7, audit: bool = True) -> list[str]:
    leave_out = [
        Cell("osm_nrw", "hv_candidate", 1_234, 980),
        Cell("osm_nrw", "conflict", 0, 0),
    ]
    labels = [Cell("osm_nrw", "conflict", 4, 3, n_not_a_substation=1, n_unlabelled=20)]
    result = Evaluation({"osm_nrw": 397}, leave_out, labels if audit else [], build_id)
    return evaluate.format_report(result)


def test_the_report_gives_every_share_with_its_interval_and_what_it_describes() -> None:
    lines = report()
    text = "\n".join(lines)
    assert "snapshots osm_nrw 397" in text
    assert "at least 110,000 V" in text
    assert "describes tagged substations only" in text
    assert "(core build 7)" in text
    hv = next(line for line in lines if line.strip().startswith("hv_candidate"))
    assert hv.split() == ["hv_candidate", "1,234", "980", "79.4%", "77.1%", "to", "81.6%"]
    conflict = [line for line in lines if line.strip().startswith("conflict")]
    labelled = conflict[1].split()
    assert labelled[:4] == ["conflict", "4", "3", "75.0%"]
    assert labelled[-2:] == ["1", "20"]  # not a substation, not yet labelled


def test_the_report_names_the_limit_that_high_meant() -> None:
    result = Evaluation({"osm_nrw": 397}, [], [], None, high_voltage_v=220_000)
    assert "at least 220,000 V" in "\n".join(evaluate.format_report(result))


def test_a_rule_that_never_fired_shows_dashes_not_a_made_up_share() -> None:
    conflict = next(line for line in report() if line.strip().startswith("conflict"))
    assert conflict.split() == ["conflict", "0", "0", "-", "-"]


def test_without_a_core_build_the_report_says_so() -> None:
    assert report(build_id=None, audit=False)[-1] == "  no core build yet; run: core"


def test_an_empty_audit_sample_is_reported_as_empty() -> None:
    assert report(audit=False)[-1] == "  the audit sample is empty"


class Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def __iter__(self) -> Any:
        return iter(self.rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class Database:
    """Answers the three queries of the evaluation from memory."""

    def __init__(self, *, verdict: tuple[int, str] = (5, "passed"), build: int | None = 3) -> None:
        self.verdict = verdict
        self.build = build
        self.seen: list[Any] = []

    def execute(self, query: str, params: Any = None) -> Rows:
        if query == core.VERDICT_SQL:
            return Rows([self.verdict])
        if query == evaluate.BUILD_SQL:
            return Rows([(self.build,)])
        if query == evaluate.LEAVE_OUT_SQL:
            self.seen.append(params)
            return Rows([("hv_candidate", 10, 5), ("no_evidence", 4, 4)])
        if query == evaluate.AUDIT_SQL:
            return Rows([("conflict", 3, 2, 1, 6)])
        raise AssertionError(query)


NRW = RuleSet(version="9.9.9", states={"osm_nrw": "05"})


def test_every_rule_outcome_has_a_cell_even_when_it_never_fired() -> None:
    db = Database()
    result = evaluate.evaluate_rules(db, NRW)  # type: ignore[arg-type]
    assert result.versions == {"osm_nrw": 5}
    assert result.build_id == 3
    assert result.high_voltage_v == 110_000
    assert [(c.suggestion, c.n, c.n_high) for c in result.leave_out] == [
        ("hv_candidate", 10, 5),
        ("conflict", 0, 0),
        ("likely_low_voltage", 0, 0),
        ("no_evidence", 4, 4),
    ]
    assert result.audit == [Cell("osm_nrw", "conflict", 3, 2, 1, 6)]
    (params,) = db.seen
    assert params["version"] == 5
    assert params["high"] == 110_000
    assert params["hint_v"] == evaluate.HINT_VOLTAGE_V
    assert params["radius"] == evaluate.LINE_HINT_RADIUS_M


def test_without_a_core_build_only_the_leave_tag_out_is_measured() -> None:
    result = evaluate.evaluate_rules(Database(build=None), NRW)  # type: ignore[arg-type]
    assert result.build_id is None
    assert result.audit == []
    assert len(result.leave_out) == 4


def test_a_quarantined_or_missing_snapshot_stops_the_evaluation() -> None:
    with pytest.raises(CoreError, match="was quarantined"):
        evaluate.evaluate_rules(Database(verdict=(5, "quarantined")), NRW)  # type: ignore[arg-type]
    with pytest.raises(CoreError, match="names no source"):
        evaluate.evaluate_rules(Database(), RuleSet(version="9.9.9"))  # type: ignore[arg-type]
