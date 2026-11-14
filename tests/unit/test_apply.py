from collections.abc import Callable
from typing import Any

import pytest

from cabler import apply
from cabler.apply import ApplyError, ApplyStep
from cabler.sources import SourceSpec

SpecFactory = Callable[..., SourceSpec]


def step() -> ApplyStep:
    return ApplyStep(
        version_id=3,
        opened=4,
        closed=2,
        current=10,
        kinds={"substation": 4, "line": 6},
        flags={"voltage_missing": 1},
    )


def test_results_describe_themselves_as_json() -> None:
    result = apply.ApplyResult("demo", "applied", 3, (step(),))
    data = result.to_json()
    assert data["status"] == "applied"
    assert data["steps"][0]["opened"] == 4
    assert data["steps"][0]["kinds"] == {"substation": 4, "line": 6}
    assert data["steps"][0]["flags"] == {"voltage_missing": 1}


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
            return FakeResult([(11,)])
        if "status = 'applied'" in query:
            return FakeResult([self.latest] if self.latest else [])
        return FakeResult([])


def test_a_source_without_a_loader_cannot_be_applied(make_spec: SpecFactory) -> None:
    with pytest.raises(ApplyError, match="no applier"):
        apply.apply_source(FakeConnection(None), make_spec())  # type: ignore[arg-type]


def test_a_source_without_the_power_theme_cannot_be_applied(make_spec: SpecFactory) -> None:
    spec = make_spec(load_kind="osm_pbf", load_themes=("water",))
    with pytest.raises(ApplyError, match="power theme"):
        apply.apply_source(FakeConnection(None), spec)  # type: ignore[arg-type]


def test_the_administrative_themes_have_an_applier_of_their_own(make_spec: SpecFactory) -> None:
    for themes in (("municipality",), ("district",), ("municipality", "district")):
        spec = make_spec(load_kind="ogr", load_themes=themes)
        assert apply._statements_for(spec) is apply.ADMIN_STATEMENTS
    mixed = make_spec(load_kind="ogr", load_themes=("municipality", "land_cover"))
    with pytest.raises(ApplyError, match="administrative-area"):
        apply._statements_for(mixed)


def test_nothing_diffed_means_nothing_to_apply(make_spec: SpecFactory) -> None:
    spec = make_spec(load_kind="osm_pbf", load_themes=("power",))
    result = apply.apply_source(FakeConnection((7,)), spec)  # type: ignore[arg-type]
    assert result.status == "skipped"
    assert result.version_id == 7
    assert result.steps == ()


def test_nothing_ever_applied_reports_no_version(make_spec: SpecFactory) -> None:
    spec = make_spec(load_kind="osm_pbf", load_themes=("power",))
    result = apply.apply_source(FakeConnection(None), spec)  # type: ignore[arg-type]
    assert result.status == "skipped"
    assert result.version_id is None
