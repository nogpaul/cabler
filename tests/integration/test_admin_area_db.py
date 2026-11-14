"""Fetch, load, diff and apply the administrative boundaries (VG250) end to end.

A tiny GeoPackage with the layers vg250_gem and vg250_krs stands in for the publisher's file; it is
served zipped by a simulated host.

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
import psycopg
import pyogrio.raw
import pytest

from cabler import apply, db, diff, fetch, load, remote, sources

pytestmark = pytest.mark.integration

if "CABLER_ENV" not in os.environ:
    pytest.skip("CABLER_ENV not set", allow_module_level=True)

FIELDS = ["OBJID", "AGS", "ARS", "GEN", "BEZ", "GF", "SN_L"]


def multi(points: list[tuple[float, float]]) -> bytes:
    """A little-endian WKB MultiPolygon with one polygon whose outer ring is `points`."""
    ring = struct.pack("<BIII", 1, 3, 1, len(points)) + b"".join(
        struct.pack("<dd", *p) for p in points
    )
    return struct.pack("<BII", 1, 6, 1) + ring


def square(x: float, y: float, size: float = 100.0) -> bytes:
    return multi([(x, y), (x + size, y), (x + size, y + size), (x, y + size), (x, y)])


# a bow-tie: its ring crosses itself, so it is invalid and has to be repaired
BOW_TIE = multi([(0, 0), (100, 100), (100, 0), (0, 100), (0, 0)])


def municipality(
    geometry: bytes, objid: str, ags: str, name: str | None, flag: int = 4
) -> tuple[bytes, tuple[Any, ...]]:
    return geometry, (objid, ags, ags + "0000", name, "Gemeinde", flag, "05")


def boundaries(
    municipalities: list[tuple[bytes, tuple[Any, ...]]],
    districts: list[tuple[bytes, tuple[Any, ...]]],
) -> bytes:
    scratch = io.BytesIO()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "DE_VG250.gpkg"
        for number, (layer, rows) in enumerate(
            [("vg250_gem", municipalities), ("vg250_krs", districts)]
        ):
            pyogrio.raw.write(
                path,
                geometry=np.array([r[0] for r in rows], dtype=object),
                field_data=[
                    np.array([r[1][i] for r in rows], dtype=np.int16 if name == "GF" else object)
                    for i, name in enumerate(FIELDS)
                ],
                fields=FIELDS,
                geometry_type="MultiPolygon",
                crs="EPSG:25832",
                driver="GPKG",
                layer=layer,
                append=number > 0,
            )
        with zipfile.ZipFile(scratch, "w") as archive:
            archive.write(path, "vg250_ebenen_0101/DE_VG250.gpkg")
    return scratch.getvalue()


class Publisher:
    def __init__(self, body: bytes) -> None:
        self.body, self.etag = body, '"r1"'
        self.client = remote.make_client(httpx.MockTransport(self.handle))

    def release(self, body: bytes, etag: str) -> None:
        self.body, self.etag = body, etag

    def handle(self, request: httpx.Request) -> httpx.Response:
        headers = {"ETag": self.etag, "Content-Length": str(len(self.body))}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(self.body))


def current(conn: psycopg.Connection[Any], source_id: str) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT level, feature_key, area_key, name, land_flag, qa_flags"
        " FROM staging.admin_area WHERE source_id = %s AND valid_to_version_id IS NULL"
        " ORDER BY level, feature_key",
        (source_id,),
    ).fetchall()


def test_boundaries_are_loaded_repaired_flagged_and_kept_with_history(tmp_path: Path) -> None:
    spec = dataclasses.replace(sources.load_all()["bkg_vg250"], source_id="itest_vg250")
    districts = [
        (square(0, 0, 1000), ("K05916", "05916", "059160000000", "Herne", "Kreis", 4, "05"))
    ]
    release1 = boundaries(
        [
            municipality(square(0, 0), "OBJ1", "05916000", "Herne"),
            municipality(BOW_TIE, "OBJ2", "02000000", "Hamburg"),
            municipality(square(300, 0), "OBJ3", "ABC", "Broken"),
            municipality(square(500, 0), "OBJ4", "05111000", None),
            # the water part of Hamburg repeats its key, and OBJID tells the two rows apart
            municipality(square(700, 0), "OBJ5", "02000000", "Hamburg", flag=2),
        ],
        districts,
    )
    release2 = boundaries(
        [
            municipality(square(0, 0), "OBJ1", "05916000", "Herne (Stadt)"),  # renamed
            municipality(BOW_TIE, "OBJ2", "02000000", "Hamburg"),
            municipality(square(500, 0), "OBJ4", "05111000", None),
            municipality(square(700, 0), "OBJ5", "02000000", "Hamburg", flag=2),
        ],  # OBJ3 is gone
        districts,
    )
    publisher = Publisher(release1)
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        first = fetch.fetch_source(conn, publisher.client, spec, tmp_path)
        loaded = load.load_source(conn, spec, tmp_path)
        assert loaded.counts == {"municipality": 5, "district": 1}
        diff.diff_source(conn, spec)
        (step,) = apply.apply_source(conn, spec).steps
        assert (step.opened, step.closed, step.current) == (6, 0, 6)
        assert step.kinds == {"district": 1, "municipality": 5}
        assert step.flags == {"geometry_repaired": 1, "key_malformed": 1, "name_missing": 1}

        rows = {(r[0], r[1]): r for r in current(conn, "itest_vg250")}
        assert rows[("municipality", "OBJ1")][2:5] == ("05916000", "Herne", 4)
        assert rows[("municipality", "OBJ2")][5] == ["geometry_repaired"]
        assert rows[("municipality", "OBJ3")][5] == ["key_malformed"]
        assert rows[("municipality", "OBJ4")][5] == ["name_missing"]
        # two rows share the key 02000000: the land part and the water part
        shared = [r for r in rows.values() if r[2] == "02000000"]
        assert sorted(r[4] for r in shared) == [2, 4]
        assert rows[("district", "K05916")][2:4] == ("05916", "Herne")
        geometry = conn.execute(
            "SELECT bool_and(ST_IsValid(geom)), bool_and(ST_SRID(geom) = 25832)"
            " FROM staging.admin_area WHERE source_id = 'itest_vg250'"
        ).fetchone()
        assert geometry == (True, True)

        publisher.release(release2, '"r2"')
        second = fetch.fetch_source(conn, publisher.client, spec, tmp_path)
        assert second.version_id != first.version_id
        load.load_source(conn, spec, tmp_path)
        diff.diff_source(conn, spec)
        (step2,) = apply.apply_source(conn, spec).steps
        assert (step2.opened, step2.closed, step2.current) == (1, 2, 5)
        names = {r[1]: r[3] for r in current(conn, "itest_vg250")}
        assert names["OBJ1"] == "Herne (Stadt)"
        assert "OBJ3" not in names

        # the first snapshot is still answerable
        def as_of(version: int | None) -> int:
            row = conn.execute(
                "SELECT count(*) FROM staging.admin_area_as_of('itest_vg250', %s)", (version,)
            ).fetchone()
            assert row is not None
            return int(row[0])

        assert as_of(first.version_id) == 6
        assert as_of(second.version_id) == 5
