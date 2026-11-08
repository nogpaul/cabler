from collections.abc import Callable
from typing import Any

import pytest

from cabler import diff
from cabler.diff import DiffError, DiffStep
from cabler.sources import SourceSpec

SpecFactory = Callable[..., SourceSpec]


def step(previous: int | None = 1) -> DiffStep:
    return DiffStep(
        version_id=2,
        previous_version_id=previous,
        added=1,
        changed=2,
        removed=3,
        total_before=10,
        total_after=8,
        changed_fields={"voltage": 2, "geometry": 1},
    )


def test_a_step_without_a_predecessor_is_a_baseline() -> None:
    assert step(previous=None).baseline is True
    assert step(previous=1).baseline is False


def test_results_describe_themselves_as_json() -> None:
    result = diff.DiffResult("demo", "diffed", 2, (step(),))
    data = result.to_json()
    assert data["status"] == "diffed"
    assert data["steps"][0]["changed_fields"] == {"voltage": 2, "geometry": 1}
    assert data["steps"][0]["baseline"] is False


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
            return FakeResult([(9,)])
        if "status = 'loaded'" in query:
            return FakeResult([])
        if "status IN ('diffed', 'applied')" in query:
            return FakeResult([self.latest] if self.latest else [])
        return FakeResult([])


def test_a_source_without_a_load_section_cannot_be_diffed(make_spec: SpecFactory) -> None:
    with pytest.raises(DiffError, match="nothing is ever loaded"):
        diff.diff_source(FakeConnection(None), make_spec())  # type: ignore[arg-type]


def test_nothing_pending_means_nothing_to_compare(make_spec: SpecFactory) -> None:
    spec = make_spec(load_kind="osm_pbf", load_themes=("power",))
    result = diff.diff_source(FakeConnection((7,)), spec)  # type: ignore[arg-type]
    assert result.status == "skipped"
    assert result.version_id == 7
    assert result.steps == ()


def test_nothing_ever_diffed_reports_no_version(make_spec: SpecFactory) -> None:
    spec = make_spec(load_kind="osm_pbf", load_themes=("power",))
    result = diff.diff_source(FakeConnection(None), spec)  # type: ignore[arg-type]
    assert result.status == "skipped"
    assert result.version_id is None
