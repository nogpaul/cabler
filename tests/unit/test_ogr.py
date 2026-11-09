import struct
import zipfile
from pathlib import Path

import numpy as np
import pyogrio.raw
import pytest

from cabler import ogr
from cabler.ogr import OgrError, Theme


def square(x: float, y: float, size: float = 10.0) -> bytes:
    """A little-endian WKB MultiPolygon with one square."""
    ring = [(x, y), (x + size, y), (x + size, y + size), (x, y + size), (x, y)]
    polygon = struct.pack("<BIII", 1, 3, 1, len(ring)) + b"".join(
        struct.pack("<dd", px, py) for px, py in ring
    )
    return struct.pack("<BII", 1, 6, 1) + polygon


def write_gpkg(path: Path, layer: str, rows: list[tuple[str, str | None, bytes]]) -> None:
    """A GeoPackage with fields CODE and NAME, in EPSG:25832."""
    pyogrio.raw.write(
        path,
        geometry=np.array([r[2] for r in rows], dtype=object),
        field_data=[
            np.array([r[0] for r in rows], dtype=object),
            np.array([r[1] for r in rows], dtype=object),
        ],
        fields=["CODE", "NAME"],
        geometry_type="MultiPolygon",
        crs="EPSG:25832",
        driver="GPKG",
        layer=layer,
    )


def make_zip(tmp_path: Path, rows: list[tuple[str, str | None, bytes]], member: str) -> Path:
    gpkg = tmp_path / "data.gpkg"
    write_gpkg(gpkg, "areas", rows)
    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(gpkg, member)
        z.writestr("documentation/readme.txt", "x")
    return archive


ROWS = [("231", "Pasture", square(0, 0)), ("111", None, square(100, 100))]
BY_ROW = Theme("areas.gpkg", "areas", "land_cover", key=None)
BY_ROW_UNPACKED = Theme("areas.gpkg", "areas", "land_cover", key=None, unpack=True)
BY_CODE = Theme("areas.gpkg", "areas", "land_cover", key="CODE")


def test_features_are_read_with_text_attributes_wkb_and_the_coordinate_system(
    tmp_path: Path,
) -> None:
    archive = make_zip(tmp_path, ROWS, "areas.gpkg")
    found = list(ogr.features(archive, BY_ROW))
    assert [(f.layer, f.key) for f in found] == [("land_cover", "1"), ("land_cover", "2")]
    assert found[0].attributes == {"CODE": "231", "NAME": "Pasture"}
    assert found[1].attributes == {"CODE": "111", "NAME": None}
    assert found[0].srid == 25832
    assert found[0].wkb is not None
    assert found[0].wkb[:5] == struct.pack("<BI", 1, 6)


def test_an_unpacked_member_gives_the_same_features_as_a_direct_read(tmp_path: Path) -> None:
    archive = make_zip(tmp_path, ROWS, "areas.gpkg")
    direct = list(ogr.features(archive, BY_ROW))
    unpacked = list(ogr.features(archive, BY_ROW_UNPACKED))
    assert [f.content_hash for f in direct] == [f.content_hash for f in unpacked]


def test_a_large_member_is_read_in_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ogr, "BATCH", 2)
    rows = [(str(100 + i), None, square(i * 20, 0)) for i in range(5)]
    archive = make_zip(tmp_path, rows, "areas.gpkg")
    found = list(ogr.features(archive, BY_ROW_UNPACKED))
    assert [f.key for f in found] == ["1", "2", "3", "4", "5"]
    assert [f.attributes["CODE"] for f in found] == ["100", "101", "102", "103", "104"]


def test_the_unpacked_copy_is_removed_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    used: list[Path] = []
    original = ogr._features

    def spy(path: str, theme: Theme, *, batched: bool):  # type: ignore[no-untyped-def]
        used.append(Path(path))
        yield from original(path, theme, batched=batched)

    monkeypatch.setattr(ogr, "_features", spy)
    archive = make_zip(tmp_path, ROWS, "areas.gpkg")
    list(ogr.features(archive, BY_ROW_UNPACKED))
    assert used
    assert not used[0].exists()


def test_an_attribute_can_be_the_identifier_and_a_repeat_is_an_error(tmp_path: Path) -> None:
    archive = make_zip(tmp_path, ROWS, "areas.gpkg")
    assert [f.key for f in ogr.features(archive, BY_CODE)] == ["231", "111"]
    twice = make_zip(tmp_path, [("7", None, square(0, 0)), ("7", None, square(5, 5))], "areas.gpkg")
    with pytest.raises(OgrError, match="CODE 7 occurs twice"):
        list(ogr.features(twice, BY_CODE))


def test_a_missing_identifier_value_is_an_error(tmp_path: Path) -> None:
    archive = make_zip(tmp_path, [("", None, square(0, 0))], "areas.gpkg")
    with pytest.raises(OgrError, match="without CODE"):
        list(ogr.features(archive, BY_CODE))


def test_wrong_themes_members_layers_and_archives_are_refused(tmp_path: Path) -> None:
    archive = make_zip(tmp_path, ROWS, "areas.gpkg")
    with pytest.raises(OgrError, match="unknown themes"):
        list(ogr.read_dataset(archive, ("rivers",)))
    with pytest.raises(OgrError, match="no member"):
        list(ogr.features(archive, Theme("other.gpkg", "areas", "x")))
    with pytest.raises(OgrError, match="no layer"):
        list(ogr.features(archive, Theme("areas.gpkg", "nothing", "x")))
    plain = tmp_path / "plain.zip"
    plain.write_text("no")
    with pytest.raises(OgrError, match="not a zip"):
        list(ogr.features(plain, BY_ROW))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (float("nan"), None),
        ("  ", None),
        (" a ", "a"),
        (5, "5"),
        (np.int32(7), "7"),
        (np.float64(1.5), "1.5"),
        (np.datetime64("NaT", "ms"), None),
        (np.datetime64("2024-05-17T00:00:00", "ms"), "2024-05-17T00:00:00.000"),
    ],
)
def test_attribute_values_become_text_and_missing_ones_become_none(
    value: object, expected: str | None
) -> None:
    assert ogr.text(value) == expected


@pytest.mark.parametrize(
    ("crs", "ok"), [("EPSG:25832", True), ("epsg:4326", True), (None, False), ("ESRI:102", False)]
)
def test_only_epsg_coordinate_systems_are_accepted(crs: str | None, ok: bool) -> None:
    if ok:
        assert ogr.srid_of(crs, "x") > 0
    else:
        with pytest.raises(OgrError, match="not an EPSG code"):
            ogr.srid_of(crs, "x")


def test_the_documented_themes_cover_land_cover_and_the_protected_areas() -> None:
    assert set(ogr.THEMES) == {
        "land_cover",
        "nature_reserve",
        "habitat_area",
        "bird_area",
        "national_park",
        "landscape_area",
    }
    assert ogr.THEMES["land_cover"].unpack
