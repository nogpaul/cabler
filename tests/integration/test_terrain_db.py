"""The terrain catalogue against a real database: tile sources, zips in zips and the set checks.

The tiles are real GeoTIFF headers without pixels (the catalogue never reads pixels).

Run with:  CABLER_ENV=dev pytest -m integration   (after `python -m cabler.migrate up dev`)
"""

import dataclasses
import io
import os
import struct
import zipfile
from pathlib import Path

import httpx
import pytest

from cabler import db, fetch, remote, sources, terrain, tiles
from cabler.terrain import CatalogueError

pytestmark = pytest.mark.integration

if "CABLER_ENV" not in os.environ:
    pytest.skip("CABLER_ENV not set", allow_module_level=True)

BASE = "https://www.opengeodata.nrw.de/produkte/geobasis/hm/dgm1_tiff/dgm1_tiff/"


def tiff(x_km: float, y_km: float, *, epsg: int = 25832, pixel: float = 1.0) -> bytes:
    """A little-endian GeoTIFF header for a 1000 x 1000 tile whose lower left corner is given."""
    keys = [1, 1, 0, 2, 1025, 0, 1, 1, 3072, 0, 1, epsg]
    entries = {
        256: (3, [1000]),
        257: (3, [1000]),
        258: (3, [32]),
        259: (3, [5]),
        339: (3, [3]),
        33550: (12, [pixel, pixel, 0.0]),
        33922: (12, [0.0, 0.0, 0.0, x_km * 1000, y_km * 1000 + 1000 * pixel, 0.0]),
        34735: (3, keys),
        42113: (2, ["-9999"]),
    }
    formats = {3: "H", 12: "d"}
    count = len(entries)
    data_at = 8 + 2 + 12 * count + 4
    body, rows = b"", b""
    for tag in sorted(entries):
        kind, values = entries[tag]
        if kind == 2:
            raw, n = str(values[0]).encode() + b"\0", len(str(values[0])) + 1
        else:
            raw, n = struct.pack("<" + formats[kind] * len(values), *values), len(values)
        field = raw.ljust(4, b"\0") if len(raw) <= 4 else struct.pack("<I", data_at + len(body))
        if len(raw) > 4:
            body += raw
        rows += struct.pack("<HHI", tag, kind, n) + field
    header = b"II" + struct.pack("<HI", 42, 8) + struct.pack("<H", count)
    return header + rows + struct.pack("<I", 0) + body


def name(x_km: int, y_km: int, year: int = 2022) -> str:
    return f"dgm1_32_{x_km}_{y_km}_1_nw_{year}.tif"


class Publisher:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.client = remote.make_client(httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        file = request.url.path.rsplit("/", 1)[-1]
        if file == "":
            rows = "".join(
                f'<file name="{n}" size="{len(b)}" timestamp="2023-05-22T08:43:46" />'
                for n, b in sorted(self.files.items())
            )
            body = (
                "<opengeodata><datasets><dataset><files>"
                f"{rows}</files></dataset></datasets></opengeodata>"
            ).encode()
        else:
            body = self.files[file]
        return httpx.Response(
            200, stream=httpx.ByteStream(body), headers={"Content-Length": str(len(body))}
        )


def tile_spec() -> sources.SourceSpec:
    base = sources.load_all()["nrw_dgm1"]
    return dataclasses.replace(base, source_id="itest_terrain", max_bytes=10_000)


def fetched(conn, spec: sources.SourceSpec, raw: Path, files: dict[str, bytes]) -> None:  # type: ignore[no-untyped-def]
    sources.register(conn, spec)
    tiles.fetch_tiles(conn, Publisher(files).client, spec, raw, yes=True)


def test_a_tile_source_is_catalogued_with_footprints_and_no_findings(tmp_path: Path) -> None:
    spec = tile_spec()
    files = {name(280, 5652): tiff(280, 5652), name(281, 5652): tiff(281, 5652)}
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        fetched(conn, spec, tmp_path, files)
        result = terrain.catalogue_source(conn, spec, tmp_path)
        assert (result.tiles, result.new, result.flags) == (2, 2, {})
        assert result.extent == (280000, 5652000, 282000, 5653000)
        assert (result.duplicate_corners, result.overlaps, result.holes) == (0, 0, 0)
        row = conn.execute(
            "SELECT ST_AsText(footprint), epsg, width_px, nodata, is_current"
            " FROM staging.terrain_tile_current WHERE source_id = %s AND name = %s",
            (spec.source_id, name(280, 5652)),
        ).fetchone()
        assert row == (
            "POLYGON((280000 5652000,280000 5653000,281000 5653000,281000 5652000,280000 5652000))",
            25832,
            1000,
            "-9999",
            True,
        )
        again = terrain.catalogue_source(conn, spec, tmp_path)
        assert (again.tiles, again.new) == (2, 0)


def test_a_missing_tile_inside_the_coverage_is_a_hole(tmp_path: Path) -> None:
    spec = tile_spec()
    ring = {
        name(x, y): tiff(x, y)
        for x in (280, 281, 282)
        for y in (5652, 5653, 5654)
        if (x, y) != (281, 5653)
    }
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        fetched(conn, spec, tmp_path, ring)
        result = terrain.catalogue_source(conn, spec, tmp_path)
        assert (result.tiles, result.holes, result.hole_km2) == (8, 1, 1.0)
        assert result.overlaps == 0


def test_duplicates_overlaps_and_unusual_tiles_are_reported(tmp_path: Path) -> None:
    spec = tile_spec()
    files = {
        name(280, 5652, 2021): tiff(280, 5652),
        name(280, 5652, 2022): tiff(280, 5652),  # the same corner twice
        name(281, 5652): tiff(280.5, 5652),  # off the grid, overlapping both tiles at 280
        name(284, 5652): tiff(284, 5652, epsg=25833),
        name(285, 5652): tiff(285, 5652, pixel=0.5),
    }
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        fetched(conn, spec, tmp_path, files)
        result = terrain.catalogue_source(conn, spec, tmp_path)
        assert result.duplicate_corners == 1
        assert result.overlaps == 3  # the two at one corner and each of them with the third
        assert result.flags == {
            "crs_unexpected": 1,
            "name_extent_mismatch": 1,
            "off_grid": 1,
            "pixel_size_unexpected": 1,
        }


def zip_in_zip(tiles_by_name: dict[str, bytes]) -> bytes:
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        for member, data in tiles_by_name.items():
            z.writestr(member, data)
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("dgm1_hh.zip", inner.getvalue())
        z.writestr("Dokumente/list.csv", "a;b")
    return outer.getvalue()


def test_a_snapshot_zip_with_a_zip_inside_is_catalogued(tmp_path: Path) -> None:
    base = sources.load_all()["hamburg_dgm1"]
    spec = dataclasses.replace(base, source_id="itest_hh_terrain", max_bytes=1_000_000)
    body = zip_in_zip({"dgm1_32_548_5934_1_hh_2026.tif": tiff(548, 5934)})

    def handle(request: httpx.Request) -> httpx.Response:
        headers = {"ETag": '"x"', "Content-Length": str(len(body))}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))

    client = remote.make_client(httpx.MockTransport(handle))
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        fetch.fetch_source(conn, client, spec, tmp_path)
        result = terrain.catalogue_source(conn, spec, tmp_path)
        assert (result.tiles, result.new, result.flags) == (1, 1, {})
        assert result.extent == (548000, 5934000, 549000, 5935000)


def test_a_source_that_is_not_catalogued_or_has_no_tiles_is_refused(tmp_path: Path) -> None:
    plain = dataclasses.replace(tile_spec(), catalogue_kind=None)
    with (
        db.connect("app") as conn,
        pytest.raises(CatalogueError, match=r"no \[catalogue\] section"),
    ):
        terrain.catalogue_source(conn, plain, tmp_path)
    empty = dataclasses.replace(tile_spec(), source_id="itest_no_tiles")
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, empty)
        with pytest.raises(CatalogueError, match="no tiles"):
            terrain.catalogue_source(conn, empty, tmp_path)


def test_a_tile_inside_another_counts_as_an_overlap(tmp_path: Path) -> None:
    spec = tile_spec()
    files = {
        name(280, 5652): tiff(280, 5652),
        name(280, 5652, 2023): tiff(280.25, 5652.25, pixel=0.5),  # 500 m, entirely inside
    }
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        fetched(conn, spec, tmp_path, files)
        result = terrain.catalogue_source(conn, spec, tmp_path)
        assert (result.overlaps, result.duplicate_corners) == (1, 0)


def test_a_tile_that_cannot_be_read_is_reported_and_the_others_are_catalogued(
    tmp_path: Path,
) -> None:
    spec = tile_spec()
    files = {name(280, 5652): tiff(280, 5652), name(281, 5652): b"this is not a tiff file"}
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        fetched(conn, spec, tmp_path, files)
        result = terrain.catalogue_source(conn, spec, tmp_path)
        assert (result.tiles, result.new) == (1, 1)
        assert list(result.unreadable) == [name(281, 5652)]
        assert "not a TIFF" in result.unreadable[name(281, 5652)]


def test_a_second_run_does_not_rewrite_unchanged_rows(tmp_path: Path) -> None:
    spec = tile_spec()
    files = {name(280, 5652): tiff(280, 5652), name(281, 5652): tiff(281, 5652)}
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        fetched(conn, spec, tmp_path, files)
        terrain.catalogue_source(conn, spec, tmp_path)

        def positions() -> list[str]:
            rows = conn.execute(
                "SELECT ctid::text FROM staging.terrain_tile WHERE source_id = %s ORDER BY name",
                (spec.source_id,),
            ).fetchall()
            return [str(r[0]) for r in rows]

        before = positions()
        terrain.catalogue_source(conn, spec, tmp_path)
        assert positions() == before


def catalogue_zip(conn, tmp_path: Path, body: bytes) -> terrain.CatalogueResult:  # type: ignore[no-untyped-def]
    base = sources.load_all()["hamburg_dgm1"]
    spec = dataclasses.replace(base, source_id="itest_hh_terrain", max_bytes=1_000_000)

    def handle(request: httpx.Request) -> httpx.Response:
        headers = {"ETag": '"x"', "Content-Length": str(len(body))}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))

    client = remote.make_client(httpx.MockTransport(handle))
    sources.register(conn, spec)
    fetch.fetch_source(conn, client, spec, tmp_path)
    return terrain.catalogue_source(conn, spec, tmp_path)


def test_the_same_tile_in_two_folders_of_a_zip_is_one_tile(tmp_path: Path) -> None:
    data = tiff(548, 5934)
    body = zip_in_zip(
        {"a/dgm1_32_548_5934_1_hh_2026.tif": data, "b/dgm1_32_548_5934_1_hh_2026.tif": data}
    )
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        result = catalogue_zip(conn, tmp_path, body)
        assert (result.tiles, result.new) == (1, 1)


def test_two_different_tiles_with_one_name_in_a_zip_are_refused(tmp_path: Path) -> None:
    body = zip_in_zip(
        {
            "a/dgm1_32_548_5934_1_hh_2026.tif": tiff(548, 5934),
            "b/dgm1_32_548_5934_1_hh_2026.tif": tiff(549, 5934),
        }
    )
    with (
        db.connect("app") as conn,
        conn.transaction(force_rollback=True),
        pytest.raises(CatalogueError, match="occurs twice"),
    ):
        catalogue_zip(conn, tmp_path, body)


def test_a_zip_inside_a_zip_that_unpacks_too_large_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terrain, "MAX_INNER_ZIP_BYTES", 10)
    body = zip_in_zip({"dgm1_32_548_5934_1_hh_2026.tif": tiff(548, 5934)})
    with (
        db.connect("app") as conn,
        conn.transaction(force_rollback=True),
        pytest.raises(CatalogueError, match="unpacks to more than"),
    ):
        catalogue_zip(conn, tmp_path, body)
