from collections.abc import Callable
from typing import Any

import pytest

from cabler import validate
from cabler.sources import SourceSpec
from cabler.validate import ValidateError, ValidateStep

SpecFactory = Callable[..., SourceSpec]


@pytest.mark.parametrize(
    ("before", "removed", "expected"),
    [
        (0, 0, None),
        (100, 0, None),
        (100, 20, None),
        (100, 21, "error"),
        (7, 6, "error"),
    ],
)
def test_too_many_vanishing_objects_is_an_error(
    before: int, removed: int, expected: str | None
) -> None:
    assert validate.removal_severity(before, removed) == expected


@pytest.mark.parametrize(
    ("count", "total", "expected"),
    [
        (0, 100, None),
        (1, 0, None),
        (1, 1000, "warning"),
        (10, 1000, "warning"),
        (11, 1000, "error"),
    ],
)
def test_shares_become_warnings_and_errors(count: int, total: int, expected: str | None) -> None:
    assert validate.share_severity(count, total, warn=0.0, error=0.01) == expected


def test_a_share_below_the_warning_level_is_not_reported() -> None:
    assert validate.share_severity(5, 1000, warn=0.01, error=0.10) is None
    assert validate.share_severity(20, 1000, warn=0.01, error=0.10) == "warning"
    assert validate.share_severity(150, 1000, warn=0.01, error=0.10) == "error"


def step(outcome: str = "passed") -> ValidateStep:
    return ValidateStep(
        version_id=4,
        outcome=outcome,
        errors=0 if outcome == "passed" else 1,
        warnings=2,
        total=10,
        rules={"voltage_hint_line": 3},
        problems=() if outcome == "passed" else ("removal_share: too many",),
    )


def test_results_describe_themselves_as_json() -> None:
    result = validate.ValidateResult("demo", "validated", 4, (step("quarantined"),))
    data = result.to_json()
    assert data["steps"][0]["outcome"] == "quarantined"
    assert data["steps"][0]["problems"] == ["removal_share: too many"]
    assert data["steps"][0]["rules"] == {"voltage_hint_line": 3}
    assert result.quarantined is True
    assert validate.ValidateResult("demo", "validated", 4, (step(),)).quarantined is False


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class FakeConnection:
    def __init__(self, latest: tuple[Any, ...] | None) -> None:
        self.latest = latest

    def execute(self, query: str, params: Any = None) -> FakeResult:
        if "INSERT INTO meta.pipeline_run" in query:
            return FakeResult([(13,)])
        if "FROM staging.snapshot_validation\nWHERE" in query:
            return FakeResult([self.latest] if self.latest else [])
        return FakeResult([])


def test_a_source_without_a_validator_is_refused(make_spec: SpecFactory) -> None:
    with pytest.raises(ValidateError, match="no validator"):
        validate.validate_source(FakeConnection(None), make_spec())  # type: ignore[arg-type]


def test_nothing_applied_means_nothing_to_validate(make_spec: SpecFactory) -> None:
    spec = make_spec(load_kind="osm_pbf", load_themes=("power",))
    result = validate.validate_source(FakeConnection((9,)), spec)  # type: ignore[arg-type]
    assert result.status == "skipped"
    assert result.version_id == 9
    assert result.steps == ()


def test_nothing_ever_validated_reports_no_version(make_spec: SpecFactory) -> None:
    spec = make_spec(load_kind="osm_pbf", load_themes=("power",))
    result = validate.validate_source(FakeConnection(None), spec)  # type: ignore[arg-type]
    assert result.version_id is None


def test_the_review_queue_is_summarised_by_suggestion() -> None:
    class Conn:
        def execute(self, query: str, params: Any = None) -> FakeResult:
            return FakeResult([("hv_candidate", 2), ("conflict", 1)])

    assert validate.review_counts(Conn(), "demo") == {"conflict": 1, "hv_candidate": 2}  # type: ignore[arg-type]


class WindConnection:
    """Answers the queries of the wind unit rules from canned numbers and keeps what is written."""

    def __init__(self, total: int, before: int, removed: int, flags: dict[str, int]) -> None:
        self.numbers = {
            validate.WIND_TOTAL_SQL: total,
            validate.WIND_BEFORE_SQL: before,
            validate.WIND_REMOVED_SQL: removed,
        }
        self.flags = flags
        self.findings: list[tuple[Any, ...]] = []
        self.verdict: tuple[Any, ...] | None = None

    def execute(self, query: str, params: Any = None) -> FakeResult:
        if query in self.numbers:
            return FakeResult([(self.numbers[query],)])
        if query == validate.WIND_FLAGS_SQL:
            return FakeResult(list(self.flags.items()))
        if query == validate.FINDING_SQL:
            self.findings.append(params)
            return FakeResult([])
        if query == validate.SUMMARY_SQL:
            counts: dict[tuple[str, str], int] = {}
            for _, rule, severity, _ in self.findings:
                counts[(rule, severity)] = counts.get((rule, severity), 0) + 1
            return FakeResult([(rule, severity, n) for (rule, severity), n in counts.items()])
        if query == validate.VERDICT_SQL:
            self.verdict = params
            return FakeResult([])
        raise AssertionError(query)


def judge_wind(conn: WindConnection) -> ValidateStep:
    return validate.validate_wind_version(conn, "mastr_bulk", 12)  # type: ignore[arg-type]


def test_a_register_with_a_few_flags_passes_with_a_warning_for_the_missing_coordinates() -> None:
    conn = WindConnection(
        44_366,
        44_000,
        10,
        {
            "coordinate_missing": 1_335,  # 3.0%: a warning
            "coordinate_placeholder": 2,
            "coordinate_outside_state": 13,
            "coordinate_outside_germany": 5,
        },
    )
    result = judge_wind(conn)
    assert (result.outcome, result.errors, result.warnings) == ("passed", 0, 1)
    assert result.rules == {"coordinate_missing": 1}
    assert conn.verdict == (12, "mastr_bulk", "passed", 0, 1)


def test_a_register_with_most_coordinates_missing_is_quarantined() -> None:
    conn = WindConnection(1_000, 1_000, 0, {"coordinate_missing": 400})
    result = judge_wind(conn)
    assert (result.outcome, result.errors) == ("quarantined", 1)
    assert "coordinate_missing" in result.problems[0]


def test_an_empty_register_is_quarantined() -> None:
    result = judge_wind(WindConnection(0, 0, 0, {}))
    assert (result.outcome, result.rules) == ("quarantined", {"empty_state": 1})


def test_a_register_that_loses_a_third_of_its_units_is_quarantined() -> None:
    result = judge_wind(WindConnection(700, 1_000, 300, {}))
    assert result.outcome == "quarantined"
    assert "removal_share" in result.rules


def test_a_register_of_wind_units_has_a_validator(make_spec: SpecFactory) -> None:
    spec = make_spec(load_kind="mastr_xml", load_themes=("wind",))
    result = validate.validate_source(FakeConnection((9,)), spec)  # type: ignore[arg-type]
    assert result.status == "skipped"  # nothing is pending; it was accepted as a source to judge
