"""The *catalogue* stage for terrain tiles: where every tile is and what it holds.

For each GeoTIFF tile of a source the header is read (:mod:`cabler.geotiff`, no pixel is decoded)
and one row is stored in ``staging.terrain_tile``: the footprint in EPSG:25832, the size, the pixel
size, the data type and the no-data value, plus findings (``qa_flags``) for anything unusual: a
coordinate system other than the study area's, a different pixel size or tile size, a tile whose
corner is not on the grid, whose name disagrees with its extent, or that has no no-data value.

The tiles come from one of two places. A tile source (``[tiles]``, many files fetched one by one)
is catalogued from ``meta.tile``. Any other source is a snapshot zip; its tile files are listed
from the zip, and a zip inside the zip (Hamburg delivers one) is unpacked once into a temporary
folder. The catalogue can be rebuilt at any time from the files.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import psycopg

from cabler import geotiff, load, runs
from cabler.sources import SourceSpec
from cabler.tiles import is_safe_name

EXPECTED_EPSG = 25832
EXPECTED_PIXEL = 1.0
EXPECTED_SIZE = 1000
GRID = 1000.0
GRID_TOLERANCE = 1e-6  # metres: coordinates come out of floating point arithmetic
MAX_INNER_ZIP_BYTES = 8 << 30  # the zip inside a zip may not unpack to more than this
MAX_UNREADABLE_LISTED = 100  # tiles named in the stored report; the count is always stored
NAME_KM = re.compile(r"_(\d{3})_(\d{4})_")  # dgm1_32_280_5652_1_nw_2022.tif -> 280, 5652 (km)

TILES_SQL = """
SELECT name, local_path, sha256, size
FROM meta.tile
WHERE source_id = %s AND status = 'fetched'
ORDER BY name
"""
SNAPSHOT_SQL = """
SELECT local_path FROM meta.source_version
WHERE source_id = %s AND status IN ('fetched', 'loaded')
ORDER BY created_at DESC, version_id DESC LIMIT 1
"""
KNOWN_SQL = "SELECT name, file_sha256 FROM staging.terrain_tile WHERE source_id = %s"
INSERT_SQL = """
INSERT INTO staging.terrain_tile
    (source_id, name, file_sha256, file_size, epsg, width_px, height_px, pixel_x, pixel_y,
     x_min, y_min, x_max, y_max, bits_per_sample, sample_format, compression, nodata, qa_flags,
     footprint)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        ST_MakeEnvelope(%s, %s, %s, %s, 25832))
"""
CURRENT_SQL = """
UPDATE staging.terrain_tile t
SET is_current = NOT t.is_current
WHERE t.source_id = %s
  AND t.is_current <> ((t.name, t.file_sha256) IN (
      SELECT n, s FROM unnest(%s::text[], %s::text[]) AS u(n, s)))
"""
FLAGS_SQL = """
SELECT flag, count(*) FROM staging.terrain_tile, unnest(qa_flags) AS flag
WHERE source_id = %s AND is_current GROUP BY flag ORDER BY flag
"""
# Findings about the set as a whole, from the footprints.
DUPLICATES_SQL = """
SELECT count(*) FROM (
    SELECT x_min, y_min FROM staging.terrain_tile WHERE source_id = %s AND is_current
    GROUP BY x_min, y_min HAVING count(*) > 1) d
"""
OVERLAPS_SQL = """
SELECT count(*) FROM staging.terrain_tile a JOIN staging.terrain_tile b
  ON a.source_id = b.source_id AND a.tile_id < b.tile_id
  AND ST_Intersects(a.footprint, b.footprint)
  AND ST_Area(ST_Intersection(a.footprint, b.footprint)) > 0
WHERE a.source_id = %s AND a.is_current AND b.is_current
"""
EXTENT_SQL = """
SELECT count(*), min(x_min), min(y_min), max(x_max), max(y_max),
       round((sum((x_max - x_min) * (y_max - y_min)) / 1e6)::numeric, 1)
FROM staging.terrain_tile WHERE source_id = %s AND is_current
"""
# Holes in the area the tiles cover: a missing tile with tiles on every side shows as a hole.
HOLES_SQL = """
SELECT count(*), coalesce(sum(ST_Area(ST_MakePolygon(ST_InteriorRingN(geom, n)))), 0)
FROM (
    SELECT (ST_Dump(ST_Union(footprint))).geom AS geom
    FROM staging.terrain_tile WHERE source_id = %s AND is_current
) parts, LATERAL generate_series(1, ST_NumInteriorRings(geom)) AS n
"""


class CatalogueError(Exception):
    """The tiles of a source cannot be catalogued."""


@dataclass(frozen=True)
class CatalogueResult:
    source_id: str
    tiles: int
    new: int
    flags: dict[str, int]
    duplicate_corners: int
    overlaps: int
    holes: int
    hole_km2: float
    extent: tuple[float, float, float, float]
    area_km2: float
    unreadable: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "tiles": self.tiles,
            "new": self.new,
            "flags": self.flags,
            "duplicate_corners": self.duplicate_corners,
            "overlaps": self.overlaps,
            "holes": self.holes,
            "hole_km2": self.hole_km2,
            "extent": list(self.extent),
            "area_km2": self.area_km2,
            "unreadable": dict(list(self.unreadable.items())[:MAX_UNREADABLE_LISTED]),
            "unreadable_count": len(self.unreadable),
        }


def _off_grid(value: float) -> bool:
    return abs(value - round(value / GRID) * GRID) > GRID_TOLERANCE


def findings(name: str, info: geotiff.GeoTiff) -> list[str]:
    """What is unusual about one tile, judged from its header and its name."""
    flags = []
    if info.epsg != EXPECTED_EPSG:
        flags.append("crs_unexpected")
    if info.pixel_x != EXPECTED_PIXEL or info.pixel_y != EXPECTED_PIXEL:
        flags.append("pixel_size_unexpected")
    if (info.width, info.height) != (EXPECTED_SIZE, EXPECTED_SIZE):
        flags.append("size_unexpected")
    if _off_grid(info.x_min) or _off_grid(info.y_min):
        flags.append("off_grid")
    km = NAME_KM.search(name)
    if km:
        x_km, y_km = int(km.group(1)) * 1000, int(km.group(2)) * 1000
        if abs(info.x_min - x_km) > GRID_TOLERANCE or abs(info.y_min - y_km) > GRID_TOLERANCE:
            flags.append("name_extent_mismatch")
    if info.nodata is None:
        flags.append("nodata_missing")
    if info.sample_format != 3:
        flags.append("not_float")
    return flags


def _insert(
    conn: psycopg.Connection[Any],
    spec: SourceSpec,
    name: str,
    sha256: str,
    size: int,
    info: geotiff.GeoTiff,
) -> None:
    conn.execute(
        INSERT_SQL,
        (
            spec.source_id,
            name,
            sha256,
            size,
            info.epsg,
            info.width,
            info.height,
            info.pixel_x,
            info.pixel_y,
            info.x_min,
            info.y_min,
            info.x_max,
            info.y_max,
            info.bits_per_sample,
            info.sample_format,
            info.compression,
            info.nodata,
            findings(name, info),
            info.x_min,
            info.y_min,
            info.x_max,
            info.y_max,
        ),
    )


def _from_tiles(
    conn: psycopg.Connection[Any], spec: SourceSpec, raw: Path, known: set[tuple[str, str]]
) -> tuple[list[tuple[str, str]], int, dict[str, str]]:
    """The tiles recorded by the tile fetcher; new ones get their header read.

    A tile whose file is missing, whose size is not the one recorded when it was fetched, or whose
    header cannot be described is reported, not catalogued, and does not stop the others.
    (Re-hashing every file is the job of ``tiles --verify``.)
    """
    current: list[tuple[str, str]] = []
    unreadable: dict[str, str] = {}
    new = 0
    for name, local_path, sha256, size in conn.execute(TILES_SQL, (spec.source_id,)).fetchall():
        if (str(name), str(sha256)) in known:
            current.append((str(name), str(sha256)))
            continue
        path = raw / str(local_path)
        try:
            found = path.stat().st_size
            if found != int(size):
                msg = f"the file is {found} bytes, the fetch recorded {int(size)}"
                raise geotiff.GeoTiffError(msg)
            with path.open("rb") as handle:
                info = geotiff.parse(handle.read(geotiff.PREFIX), str(name))
        except (OSError, geotiff.GeoTiffError) as exc:
            unreadable[str(name)] = str(exc)
            continue
        current.append((str(name), str(sha256)))
        _insert(conn, spec, str(name), str(sha256), int(size), info)
        new += 1
    return current, new, unreadable


def _is_metadata(member: str) -> bool:
    """Files that archive tools add: ``__MACOSX/`` folders and names that start with a dot."""
    parts = PurePosixPath(member).parts
    return "__MACOSX" in parts or parts[-1].startswith(".")


def tif_members(archive: zipfile.ZipFile) -> list[tuple[str, int]]:
    """The tile files of a zip: name and size. Archive metadata is not a tile."""
    return [
        (i.filename, i.file_size)
        for i in archive.infolist()
        if i.filename.lower().endswith(".tif") and not i.is_dir() and not _is_metadata(i.filename)
    ]


def inner_zip(archive: zipfile.ZipFile, label: str) -> str:
    """The name of the one zip inside a zip that holds no tiles itself."""
    zips = [i.filename for i in archive.infolist() if i.filename.lower().endswith(".zip")]
    if len(zips) != 1:
        msg = f"{label}: no tile files and {len(zips)} zips inside; expected tiles or one zip"
        raise CatalogueError(msg)
    return zips[0]


def _read_members(
    conn: psycopg.Connection[Any],
    spec: SourceSpec,
    archive: zipfile.ZipFile,
    known: set[tuple[str, str]],
) -> tuple[list[tuple[str, str]], int, dict[str, str]]:
    current: list[tuple[str, str]] = []
    unreadable: dict[str, str] = {}
    seen: dict[str, str] = {}
    new = 0
    for member, size in tif_members(archive):
        name = Path(member).name
        if not is_safe_name(name):
            # shown as Python writes it, so a control character cannot reach a terminal
            unreadable[ascii(name)] = "not a plain file name"
            continue
        with archive.open(member) as handle:
            head = handle.read(geotiff.PREFIX)
            digest = hashlib.sha256(head)
            while chunk := handle.read(1 << 22):
                digest.update(chunk)
        sha256 = digest.hexdigest()
        if name in seen:
            if seen[name] != sha256:
                msg = f"{name} occurs twice in the zip with different content"
                raise CatalogueError(msg)
            continue  # the same file in two folders is one tile
        seen[name] = sha256
        if (name, sha256) in known:
            current.append((name, sha256))
            continue
        try:
            info = geotiff.parse(head, name)
        except geotiff.GeoTiffError as exc:
            unreadable[name] = str(exc)
            continue
        current.append((name, sha256))
        _insert(conn, spec, name, sha256, size, info)
        new += 1
    return current, new, unreadable


def _from_zip(
    conn: psycopg.Connection[Any], spec: SourceSpec, raw: Path, known: set[tuple[str, str]]
) -> tuple[list[tuple[str, str]], int, dict[str, str]]:
    """The tiles of the newest snapshot, a zip, or of the zip inside it."""
    row = conn.execute(SNAPSHOT_SQL, (spec.source_id,)).fetchone()
    if row is None:
        msg = f"{spec.source_id}: nothing was fetched"
        raise CatalogueError(msg)
    path = load.snapshot_file(raw, str(row[0]))
    with zipfile.ZipFile(path) as outer:
        if tif_members(outer):
            return _read_members(conn, spec, outer, known)
        inner = inner_zip(outer, path.name)
        if outer.getinfo(inner).file_size > MAX_INNER_ZIP_BYTES:
            msg = f"{inner}: the zip inside unpacks to more than {MAX_INNER_ZIP_BYTES:,} bytes"
            raise CatalogueError(msg)
        with tempfile.TemporaryDirectory(prefix="cabler-terrain-") as scratch:
            unpacked = Path(scratch) / Path(inner).name
            with outer.open(inner) as source, unpacked.open("wb") as target:
                shutil.copyfileobj(source, target, 1 << 22)
            with zipfile.ZipFile(unpacked) as archive:
                if not tif_members(archive):
                    msg = f"{unpacked.name}: the zip holds no tile files"
                    raise CatalogueError(msg)
                return _read_members(conn, spec, archive, known)


def catalogue_source(conn: psycopg.Connection[Any], spec: SourceSpec, raw: Path) -> CatalogueResult:
    """Catalogue the current tiles of one source and check the set as a whole."""
    if spec.catalogue_kind != "geotiff":
        msg = f"{spec.source_id}: no [catalogue] section of kind 'geotiff'"
        raise CatalogueError(msg)
    with runs.recorded(conn, "catalogue", spec.source_id) as run:
        known = {(str(n), str(s)) for n, s in conn.execute(KNOWN_SQL, (spec.source_id,)).fetchall()}
        with conn.transaction():
            reader = _from_tiles if spec.tiles_kind is not None else _from_zip
            current, new, unreadable = reader(conn, spec, raw, known)
            if not current:
                msg = f"{spec.source_id}: there are no tiles to catalogue"
                if unreadable:
                    first, why = next(iter(unreadable.items()))
                    msg += f"; all {len(unreadable):,} could not be read (first: {first}: {why})"
                raise CatalogueError(msg)
            conn.execute(
                CURRENT_SQL, (spec.source_id, [n for n, _ in current], [s for _, s in current])
            )
            conn.execute("ANALYZE staging.terrain_tile")
        flags = {str(f): int(c) for f, c in conn.execute(FLAGS_SQL, (spec.source_id,)).fetchall()}
        dup = conn.execute(DUPLICATES_SQL, (spec.source_id,)).fetchone()
        overlaps = conn.execute(OVERLAPS_SQL, (spec.source_id,)).fetchone()
        holes = conn.execute(HOLES_SQL, (spec.source_id,)).fetchone()
        extent = conn.execute(EXTENT_SQL, (spec.source_id,)).fetchone()
        if extent is None or dup is None or overlaps is None or holes is None:
            msg = f"{spec.source_id}: the checks of the tile set returned nothing"
            raise CatalogueError(msg)
        result = CatalogueResult(
            source_id=spec.source_id,
            tiles=int(extent[0]),
            new=new,
            flags=flags,
            duplicate_corners=int(dup[0]),
            overlaps=int(overlaps[0]),
            holes=int(holes[0]),
            hole_km2=round(float(holes[1]) / 1e6, 3),
            extent=(float(extent[1]), float(extent[2]), float(extent[3]), float(extent[4])),
            area_km2=float(extent[5]),
            unreadable=unreadable,
        )
        run.result = result.to_json()
    return result
