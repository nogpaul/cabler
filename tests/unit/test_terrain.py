import zipfile
from pathlib import Path

import pytest

from cabler import geotiff, terrain
from cabler.terrain import CatalogueError


def info(**changes: object) -> geotiff.GeoTiff:
    base: dict[str, object] = {
        "width": 1000,
        "height": 1000,
        "bits_per_sample": 32,
        "sample_format": 3,
        "compression": 5,
        "pixel_x": 1.0,
        "pixel_y": 1.0,
        "origin_x": 280000.0,
        "origin_y": 5653000.0,
        "epsg": 25832,
        "nodata": "-9999",
    }
    return geotiff.GeoTiff(**{**base, **changes})  # type: ignore[arg-type]


NAME = "dgm1_32_280_5652_1_nw_2022.tif"


def test_a_tile_that_looks_like_the_others_has_no_findings() -> None:
    assert terrain.findings(NAME, info()) == []


@pytest.mark.parametrize(
    ("change", "flag"),
    [
        ({"epsg": 25833}, "crs_unexpected"),
        ({"epsg": None}, "crs_unexpected"),
        ({"pixel_x": 0.5}, "pixel_size_unexpected"),
        ({"pixel_y": 2.0}, "pixel_size_unexpected"),
        ({"width": 500}, "size_unexpected"),
        ({"height": 1001}, "size_unexpected"),
        ({"origin_x": 280000.5}, "off_grid"),
        ({"origin_y": 5653000.25}, "off_grid"),
        ({"nodata": None}, "nodata_missing"),
        ({"sample_format": 1}, "not_float"),
    ],
)
def test_each_unusual_property_of_a_tile_is_one_finding(
    change: dict[str, object], flag: str
) -> None:
    assert flag in terrain.findings(NAME, info(**change))


def test_a_name_that_disagrees_with_the_extent_is_a_finding() -> None:
    assert terrain.findings("dgm1_32_281_5652_1_nw_2022.tif", info()) == ["name_extent_mismatch"]
    assert terrain.findings("dgm1_32_280_5653_1_nw_2022.tif", info()) == ["name_extent_mismatch"]
    # a name that does not carry coordinates cannot disagree
    assert terrain.findings("tile.tif", info()) == []


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def test_the_tile_files_of_a_zip_are_listed_without_folders_or_other_files(tmp_path: Path) -> None:
    path = make_zip(
        tmp_path / "a.zip",
        {
            "x/a.tif": b"1",
            "x/b.TIF": b"22",
            "x/": b"",
            "readme.csv": b"c",
            "x/c.tif.xml": b"d",
            "__MACOSX/x/._a.tif": b"3",  # archive metadata is not a tile
            "x/.hidden.tif": b"4",
        },
    )
    with zipfile.ZipFile(path) as archive:
        assert terrain.tif_members(archive) == [("x/a.tif", 1), ("x/b.TIF", 2)]


def test_a_zip_without_tiles_must_hold_exactly_one_zip(tmp_path: Path) -> None:
    one = make_zip(tmp_path / "one.zip", {"inner.zip": b"x", "doc/readme.csv": b"c"})
    with zipfile.ZipFile(one) as archive:
        assert terrain.inner_zip(archive, "one.zip") == "inner.zip"
    two = make_zip(tmp_path / "two.zip", {"a.zip": b"x", "b.zip": b"y"})
    with zipfile.ZipFile(two) as archive, pytest.raises(CatalogueError, match="2 zips"):
        terrain.inner_zip(archive, "two.zip")
    none = make_zip(tmp_path / "none.zip", {"readme.csv": b"c"})
    with zipfile.ZipFile(none) as archive, pytest.raises(CatalogueError, match="0 zips"):
        terrain.inner_zip(archive, "none.zip")


def test_floating_point_noise_in_a_corner_is_not_a_finding() -> None:
    noisy = info(origin_x=280000.0000001, origin_y=5653000.0000002)
    assert terrain.findings(NAME, noisy) == []
    assert terrain.findings("tile.tif", noisy) == []


def result_with(unreadable: dict[str, str]) -> terrain.CatalogueResult:
    return terrain.CatalogueResult(
        source_id="s",
        tiles=1,
        new=1,
        flags={},
        duplicate_corners=0,
        overlaps=0,
        holes=0,
        hole_km2=0.0,
        extent=(0.0, 0.0, 1.0, 1.0),
        area_km2=1.0,
        unreadable=unreadable,
    )


def test_the_stored_report_lists_a_bounded_number_of_unreadable_tiles() -> None:
    unreadable = {f"t{i}.tif": "bad" for i in range(250)}
    stored = result_with(unreadable).to_json()
    assert stored["unreadable_count"] == 250
    assert list(stored["unreadable"]) == list(unreadable)[: terrain.MAX_UNREADABLE_LISTED]


def test_a_short_unreadable_list_is_stored_whole() -> None:
    stored = result_with({"a.tif": "x", "b.tif": "y"}).to_json()
    assert stored["unreadable"] == {"a.tif": "x", "b.tif": "y"}
    assert stored["unreadable_count"] == 2
