import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cabler import load, osm
from cabler.load import LoadError
from cabler.sources import SourceSpec

SpecFactory = Callable[..., SourceSpec]
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "power_mini.osm"
COLUMN = {name: 4 + i for i, (_, name) in enumerate(osm.PROMOTED)}  # index in a yielded row


def rows(layer: str, *themes: str) -> list[tuple[Any, ...]]:
    return list(load.read_layer(FIXTURE, layer, themes))


def test_where_clause_combines_themes() -> None:
    assert load.where_clause(["power"]) == "(power IS NOT NULL)"
    clause = load.where_clause(["power", "transport"])
    assert clause.startswith("(power IS NOT NULL) OR (highway IS NOT NULL")


@pytest.mark.parametrize(
    ("layer", "record", "expected"),
    [
        ("points", {"osm_id": "7"}, "n7"),
        ("lines", {"osm_id": "8"}, "w8"),
        ("multipolygons", {"osm_id": "9", "osm_way_id": None}, "r9"),
        ("multipolygons", {"osm_id": None, "osm_way_id": "12"}, "w12"),
        ("points", {"osm_id": None}, None),
    ],
)
def test_osm_keys_are_stable_feature_ids(
    layer: str, record: dict[str, Any], expected: str | None
) -> None:
    assert load.osm_key(layer, record) == expected


def test_points_keep_only_the_power_theme_and_promote_the_tags() -> None:
    found = {row[1]: row for row in rows("points", "power")}
    assert sorted(found) == ["n1", "n2"]
    substation = found["n1"]
    assert substation[0] == "points"
    assert substation[2] == 3  # osm_version
    assert str(substation[3]).startswith("2026-01-01")
    assert substation[COLUMN["name"]] == "Umspannwerk Test"
    assert substation[COLUMN["voltage"]] == "110000"
    assert substation[COLUMN["operator"]] == "Testnetz"
    generator = found["n2"]
    assert generator[COLUMN["generator_source"]] == "wind"
    assert generator[COLUMN["generator_output_electricity"]] == "3 MW"
    assert isinstance(generator[-1], bytes)


def test_lines_follow_the_selected_themes() -> None:
    assert [r[1] for r in rows("lines", "power")] == ["w10"]
    assert [r[1] for r in rows("lines", "transport")] == ["w11"]
    assert sorted(r[1] for r in rows("lines", "power", "transport")) == ["w10", "w11"]
    line = rows("lines", "power")[0]
    assert line[COLUMN["cables"]] == "3"
    assert line[COLUMN["circuits"]] == "1"


def test_a_closed_substation_outline_becomes_a_polygon() -> None:
    (area,) = rows("multipolygons", "power")
    assert area[1] == "w12"
    assert area[COLUMN["voltage"]] == "380000"
    assert area[COLUMN["name"]] == "Umspannwerk Flaeche"


def test_timestamps_are_always_marked_as_utc() -> None:
    assert load._utc("2026-01-07T10:00:00") == "2026-01-07T10:00:00Z"
    assert load._utc("2026-01-07T10:00:00Z") == "2026-01-07T10:00:00Z"
    assert load._utc("2026-01-07T10:00:00+00:00") == "2026-01-07T10:00:00+00:00"
    assert load._utc(None) is None
    area_time = rows("multipolygons", "power")[0][3]
    assert str(area_time).endswith("Z")


def test_objects_outside_every_theme_are_dropped() -> None:
    assert rows("points", "water") == []


def test_insert_statement_has_one_placeholder_per_column() -> None:
    text = load.INSERT.as_string(None)
    assert text.count("%s") == len(load.COLUMNS)
    assert '"natural"' in text
    assert "ST_SetSRID(ST_GeomFromWKB(%s), 4326)" in text


class FakeResult:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class FakeConnection:
    def __init__(self, pending: tuple[Any, ...] | None) -> None:
        self.pending = pending
        self.calls: list[str] = []

    def execute(self, query: str, params: Any = None) -> FakeResult:
        self.calls.append(query)
        if "INSERT INTO meta.pipeline_run" in query:
            return FakeResult((5,))
        if "status = 'fetched'" in query:
            return FakeResult(self.pending)
        return FakeResult(None)


def test_a_source_without_a_load_section_cannot_be_loaded(
    tmp_path: Path, make_spec: SpecFactory
) -> None:
    with pytest.raises(LoadError, match="nothing to load"):
        load.load_source(FakeConnection(None), make_spec(), tmp_path)  # type: ignore[arg-type]


def test_nothing_fetched_means_nothing_to_load(tmp_path: Path, make_spec: SpecFactory) -> None:
    spec = make_spec(load_kind="osm_pbf", load_themes=("power",))
    result = load.load_source(FakeConnection(None), spec, tmp_path)  # type: ignore[arg-type]
    assert result.status == "skipped"
    assert result.version_id is None


def test_a_modified_snapshot_is_refused(tmp_path: Path, make_spec: SpecFactory) -> None:
    folder = tmp_path / "demo" / "20261108T000000Z_abc"
    folder.mkdir(parents=True)
    (folder / "data.osm").write_bytes(FIXTURE.read_bytes())
    (folder / "manifest.json").write_text(json.dumps({"file": "data.osm"}), encoding="utf-8")
    wrong = hashlib.sha256(b"something else").hexdigest()
    conn = FakeConnection((8, "demo/20261108T000000Z_abc", wrong))
    spec = make_spec(load_kind="osm_pbf", load_themes=("power",))
    with pytest.raises(LoadError, match="no longer matches the checksum"):
        load.load_source(conn, spec, tmp_path)  # type: ignore[arg-type]
