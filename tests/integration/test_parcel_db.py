"""Parcels from a source of many files: fetch the tiles, snapshot the set, load, diff and apply.

Two districts are two GeoPackage zips behind a simulated publisher with an index. A second release
replaces one of them.

Run with:  CABLER_ENV=dev pytest -m integration   (after `python -m cabler.migrate up dev`)
"""

import dataclasses
import io
import os
import struct
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pyogrio.raw
import pytest

from cabler import apply, db, diff, load, ogr, remote, sources, tiles, tileset

pytestmark = pytest.mark.integration

if "CABLER_ENV" not in os.environ:
    pytest.skip("CABLER_ENV not set", allow_module_level=True)

BASE = "https://www.opengeodata.nrw.de/produkte/geobasis/lk/akt/gru_vereinfacht_gpkg/"
FIELDS = [
    "flstkennz",
    "oid",
    "flaeche",
    "landschl",
    "kreis",
    "gemeinde",
    "gmdschl",
    "gemarkung",
    "gemaschl",
    "flur",
    "flstnrzae",
    "flstnrnen",
    "lagebeztxt",
    "aktualit",
]


def outline(points: list[tuple[float, float]]) -> bytes:
    """A little-endian WKB MultiPolygon with one polygon whose outer ring is `points`."""
    ring = struct.pack("<BIII", 1, 3, 1, len(points))
    ring += b"".join(struct.pack("<dd", *p) for p in points)
    return struct.pack("<BII", 1, 6, 1) + ring


def square(x: float, y: float, size: float) -> bytes:
    return outline([(x, y), (x + size, y), (x + size, y + size), (x, y + size), (x, y)])


BOW_TIE = outline([(0, 0), (10, 10), (10, 0), (0, 10), (0, 0)])  # crosses itself: invalid


def parcel(key: str, shape: bytes, area: float, district: str) -> tuple[Any, ...]:
    return (
        shape,
        key,
        f"oid-{key}",
        str(area),
        "05",
        district,
        "Town",
        "05000000",
        "Cadastral district",
        "0001",
        "16",
        "135",
        "0",
        "Main Street",
        "2024-08-30",
    )


def district_zip(rows: list[tuple[Any, ...]]) -> bytes:
    """A zip with one GeoPackage member (any name), layer Flurstueck, like the publisher's."""
    scratch = io.BytesIO()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "data.gpkg"
        pyogrio.raw.write(
            path,
            geometry=np.array([r[0] for r in rows], dtype=object),
            field_data=[np.array([r[1 + i] for r in rows], dtype=object) for i in range(14)],
            fields=FIELDS,
            geometry_type="MultiPolygon",
            crs="EPSG:25832",
            driver="GPKG",
            layer="Flurstueck",
        )
        with zipfile.ZipFile(scratch, "w") as archive:
            archive.write(path, "gru_vereinf_district.gpkg")
            archive.writestr("readme.txt", "x")
    return scratch.getvalue()


class Publisher:
    """An index plus its zips; change `files` to simulate a new release."""

    def __init__(self, files: dict[str, tuple[bytes, str]]) -> None:
        self.files = files
        self.client = remote.make_client(httpx.MockTransport(self.handle))

    def index(self) -> bytes:
        rows = "".join(
            f'<file name="{name}" size="{len(body)}" timestamp="{stamp}" />'
            for name, (body, stamp) in sorted(self.files.items())
        )
        xml = (
            "<opengeodata><datasets><dataset><files>"
            f"{rows}</files></dataset></datasets></opengeodata>"
        )
        return xml.encode()

    def handle(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        body = self.index() if name == "" else self.files[name][0]
        return httpx.Response(
            200, stream=httpx.ByteStream(body), headers={"Content-Length": str(len(body))}
        )


def make_spec() -> sources.SourceSpec:
    base = sources.load_all()["alkis_nrw"]
    return dataclasses.replace(base, source_id="itest_alkis", max_bytes=1_000_000)


RELEASE_1 = {
    "alpha.zip": (
        district_zip(
            [
                parcel("A1", square(0, 0, 10), 100.0, "Alpha"),
                parcel("A2", BOW_TIE, 50.0, "Alpha"),
            ]
        ),
        "2026-07-29T10:00:00",
    ),
    "beta.zip": (
        district_zip([parcel("B1", square(100, 0, 20), 500.0, "Beta")]),  # 400 m2, says 500
        "2026-07-29T10:01:00",
    ),
}
RELEASE_2 = {
    "alpha.zip": RELEASE_1["alpha.zip"],
    "beta.zip": (
        district_zip(
            [
                parcel("B1", square(100, 10, 20), 400.0, "Beta"),  # moved, area now right
                parcel("B2", square(200, 0, 10), 100.0, "Beta"),  # new
            ]
        ),
        "2026-10-15T10:01:00",
    ),
}


def test_parcels_of_many_files_become_one_snapshot_with_history(tmp_path: Path) -> None:
    spec = make_spec()
    publisher = Publisher(dict(RELEASE_1))
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        tiles.fetch_tiles(conn, publisher.client, spec, tmp_path, yes=True)

        loaded = load.load_source(conn, spec, tmp_path)
        assert loaded.status == "loaded"
        assert loaded.counts == {"parcel": 3}
        first = loaded.version_id
        assert first is not None
        assert (
            load.load_source(conn, spec, tmp_path).status == "skipped"
        )  # same set, no new snapshot

        (base,) = diff.diff_source(conn, spec).steps
        assert (base.added, base.total_after) == (3, 3)
        (step,) = apply.apply_source(conn, spec).steps
        assert (step.opened, step.closed, step.current) == (3, 0, 3)
        assert step.kinds == {"Alpha": 2, "Beta": 1}
        assert step.flags == {"area_mismatch_large": 1, "geometry_repaired": 1}
        row = conn.execute(
            "SELECT state_key, district, cadastral_district, area_official_m2, area_geometry_m2,"
            " updated::text, ST_SRID(geom) FROM staging.parcel_current"
            " WHERE source_id = %s AND feature_key = 'A1'",
            (spec.source_id,),
        ).fetchone()
        assert row == ("05", "Alpha", "Cadastral district", 100, 100, "2024-08-30", 25832)

        publisher.files = dict(RELEASE_2)
        tiles.fetch_tiles(conn, publisher.client, spec, tmp_path, yes=True)
        second = load.load_source(conn, spec, tmp_path)
        assert second.status == "loaded"
        assert second.version_id != first
        (change,) = diff.diff_source(conn, spec).steps
        assert (change.added, change.changed, change.removed) == (1, 1, 0)
        assert change.changed_fields == {"flaeche": 1, "geometry": 1}
        (step2,) = apply.apply_source(conn, spec).steps
        assert (step2.opened, step2.closed, step2.current) == (2, 1, 4)
        assert step2.flags == {"geometry_repaired": 1}  # B1 is consistent now

        then = [
            r[0]
            for r in conn.execute(
                "SELECT feature_key FROM staging.parcel_as_of(%s, %s) ORDER BY 1",
                (spec.source_id, first),
            ).fetchall()
        ]
        assert then == ["A1", "A2", "B1"]


def test_a_tile_that_changed_after_it_was_fetched_is_not_loaded(tmp_path: Path) -> None:
    spec = make_spec()
    publisher = Publisher(dict(RELEASE_1))
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        tiles.fetch_tiles(conn, publisher.client, spec, tmp_path, yes=True)
        damaged = tmp_path / "itest_alkis" / "tiles" / "beta.zip"
        damaged.chmod(0o644)
        damaged.write_bytes(damaged.read_bytes() + b"x")
        with pytest.raises(ogr.OgrError, match="no longer matches"):
            load.load_source(conn, spec, tmp_path)


def test_the_listing_of_a_snapshot_names_every_tile_with_its_checksum(tmp_path: Path) -> None:
    spec = make_spec()
    publisher = Publisher(dict(RELEASE_1))
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        tiles.fetch_tiles(conn, publisher.client, spec, tmp_path, yes=True)
        version = tileset.snapshot_set(conn, spec, tmp_path)
        assert version is not None
        assert tileset.snapshot_set(conn, spec, tmp_path) == version
        row = conn.execute(
            "SELECT local_path FROM meta.source_version WHERE version_id = %s", (version,)
        ).fetchone()
        assert row is not None
        listing = tmp_path / str(row[0]) / "tiles.json"
        names = [t.name for t in tileset.read_listing(listing)]
        assert names == ["alpha.zip", "beta.zip"]
        assert len(tileset.verified_tiles(listing)) == 2


def test_a_source_without_tiles_has_no_snapshot_to_make(tmp_path: Path) -> None:
    spec = dataclasses.replace(make_spec(), source_id="itest_empty")
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        assert tileset.snapshot_set(conn, spec, tmp_path) is None
