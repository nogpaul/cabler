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
