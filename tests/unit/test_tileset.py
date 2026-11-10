import hashlib
import json
import struct
import zipfile
from pathlib import Path

import numpy as np
import pyogrio.raw
import pytest

from cabler import ogr, tileset
from cabler.tileset import SetTile, TileSetError


def write_listing(raw: Path, tiles: list[dict[str, object]]) -> Path:
    folder = raw / "src" / "sets" / "20261109T000000Z_abc"
    folder.mkdir(parents=True)
    listing = folder / "tiles.json"
    listing.write_text(json.dumps({"source_id": "src", "tiles": tiles}), encoding="utf-8")
    return listing


def tile_entry(raw: Path, name: str, data: bytes) -> dict[str, object]:
    path = raw / "src" / "tiles" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {
        "name": name,
        "path": f"src/tiles/{name}",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def district_zip(tmp_path: Path, member: str, codes: list[str]) -> bytes:
    """A zip holding a GeoPackage member (layer 'areas', field CODE) of unit squares."""
    shapes = []
    for i, _ in enumerate(codes):
        ring = [(i, 0), (i + 1, 0), (i + 1, 1), (i, 1), (i, 0)]
        polygon = struct.pack("<BIII", 1, 3, 1, len(ring)) + b"".join(
            struct.pack("<dd", *p) for p in ring
        )
        shapes.append(struct.pack("<BII", 1, 6, 1) + polygon)
    gpkg = tmp_path / member
    pyogrio.raw.write(
        gpkg,
        geometry=np.array(shapes, dtype=object),
        field_data=[np.array(codes, dtype=object)],
        fields=["CODE"],
        geometry_type="MultiPolygon",
        crs="EPSG:25832",
        driver="GPKG",
        layer="areas",
    )
    archive = tmp_path / f"{member}.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.write(gpkg, member)
        z.writestr("readme.txt", "x")
    return archive.read_bytes()


def test_a_listing_is_read_back_with_the_raw_directory_above_it(tmp_path: Path) -> None:
    listing = write_listing(tmp_path, [tile_entry(tmp_path, "a.zip", b"data")])
    assert tileset.raw_root(listing) == tmp_path
    assert tileset.read_listing(listing) == [
        SetTile("a.zip", "src/tiles/a.zip", 4, hashlib.sha256(b"data").hexdigest())
    ]


def test_a_file_that_is_not_a_listing_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "tiles.json"
    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(TileSetError, match="not a tile listing"):
        tileset.read_listing(bad)
    odd = tmp_path / "other.json"
    odd.write_text('{"tiles": [{"name": "a"}]}', encoding="utf-8")
    with pytest.raises(TileSetError, match="not a tile listing"):
        tileset.read_listing(odd)


def test_tiles_are_returned_only_when_they_match_their_recorded_checksum(tmp_path: Path) -> None:
    entries = [tile_entry(tmp_path, "a.zip", b"aaa"), tile_entry(tmp_path, "b.zip", b"bbbb")]
    listing = write_listing(tmp_path, entries)
    assert [t.name for t, _ in tileset.verified_tiles(listing)] == ["a.zip", "b.zip"]

    (tmp_path / "src" / "tiles" / "b.zip").write_bytes(b"bbbX")  # same size, other content
    with pytest.raises(TileSetError, match="no longer matches"):
        tileset.verified_tiles(listing)
    (tmp_path / "src" / "tiles" / "b.zip").unlink()
    with pytest.raises(TileSetError, match="missing"):
        tileset.verified_tiles(listing)


def test_every_tile_of_a_listing_is_read_and_the_member_is_found_by_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    entries = [
        tile_entry(tmp_path, "a.zip", district_zip(scratch, "gru_a.gpkg", ["1", "2"])),
        tile_entry(tmp_path, "b.zip", district_zip(scratch, "gru_b.gpkg", ["3"])),
    ]
    listing = write_listing(tmp_path, entries)
    theme = ogr.Theme("*.gpkg", "areas", "parcel", key="CODE")
    monkeypatch.setitem(ogr.THEMES, "parcel_test", theme)
    found = list(ogr.read_tileset(listing, ("parcel_test",)))
    assert [f.key for f in found] == ["1", "2", "3"]
    assert {f.layer for f in found} == {"parcel"}


def test_a_damaged_tile_stops_the_read_with_an_ogr_error(tmp_path: Path) -> None:
    entry = tile_entry(tmp_path, "a.zip", b"data")
    listing = write_listing(tmp_path, [entry])
    (tmp_path / "src" / "tiles" / "a.zip").write_bytes(b"dxta")
    with pytest.raises(ogr.OgrError, match="no longer matches"):
        list(ogr.read_tileset(listing, ("parcel",)))


def test_a_pattern_must_match_exactly_one_member() -> None:
    assert ogr._member(["a/x.gpkg", "readme.txt"], "*.gpkg") == "a/x.gpkg"
    with pytest.raises(ogr.OgrError, match="0 members"):
        ogr._member(["readme.txt"], "*.gpkg")
    with pytest.raises(ogr.OgrError, match="2 members"):
        ogr._member(["a.gpkg", "b.gpkg"], "*.gpkg")


def test_a_tile_path_that_leaves_the_raw_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "outside.zip").write_bytes(b"data")
    raw = tmp_path / "raw"
    entry = tile_entry(raw, "a.zip", b"data")
    entry["path"] = "../outside.zip"  # a file that exists and has the recorded checksum
    listing = write_listing(raw, [entry])
    with pytest.raises(TileSetError, match="outside the raw directory"):
        tileset.verified_tiles(listing)
