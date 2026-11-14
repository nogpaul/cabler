"""Reader for vector datasets in a zip (GeoPackage, GML, ...), through GDAL/OGR via pyogrio.

A *theme* names the member inside the zip, the layer inside that file, the layer name under which
its features are stored, and the attribute that identifies a feature (or none, then the row number
of the file is the identifier). Every attribute becomes text, as delivered; the geometry is kept as
WKB with the coordinate system of the file.

Small members are read straight from the zip. A member that is large and has to be read in
batches (a GeoPackage is a database, and reading it from a compressed zip means decompressing from
the start for every jump: minutes per batch) is unpacked once into a temporary folder, verified by
the zip's own checksum, read, and deleted again.
"""

from __future__ import annotations

import fnmatch
import math
import shutil
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyogrio.errors
import pyogrio.raw

from cabler import tileset
from cabler.features import Feature

BATCH = 20_000
MAX_UNPACKED_BYTES = 8 << 30  # no member may declare more, whether it is unpacked or streamed


class OgrError(Exception):
    """The dataset is not what the source says it is, or a feature is unusable."""


@dataclass(frozen=True)
class Theme:
    """One layer of one member of the zip and where its features are stored."""

    member: str
    layer: str
    stored_as: str
    key: str | None = None  # attribute that identifies a feature; None: the row number
    unpack: bool = False  # unpack the member first and read it in batches


THEMES: dict[str, Theme] = {
    "land_cover": Theme(
        "clc5_2021/CLC5ha_2021.gpkg", "clc5ha_2021", "land_cover", key=None, unpack=True
    ),
    "nature_reserve": Theme(
        "Naturschutzgebiete.xml", "Naturschutzgebiete", "nature_reserve", key="gml_id"
    ),
    "habitat_area": Theme(
        "Fauna_Flora_Habitat_Gebiete.xml",
        "Fauna_Flora_Habitat_Gebiete",
        "habitat_area",
        key="gml_id",
    ),
    "bird_area": Theme("Vogelschutzgebiete.xml", "Vogelschutzgebiete", "bird_area", key="gml_id"),
    "national_park": Theme("Nationalparke.xml", "Nationalparke", "national_park", key="gml_id"),
    "landscape_area": Theme(
        "Landschaftsschutzgebiete.xml", "Landschaftsschutzgebiete", "landscape_area", key="gml_id"
    ),
    # One GeoPackage per district, named after it, so the member is found by its extension.
    "parcel": Theme("*.gpkg", "Flurstueck", "parcel", key="flstkennz", unpack=True),
    # BKG VG250: one GeoPackage with every level in a folder named after the edition. OBJID is the
    # only identifier that is unique (the municipality key repeats for a land and a water part).
    "municipality": Theme("*DE_VG250.gpkg", "vg250_gem", "municipality", key="OBJID", unpack=True),
    "district": Theme("*DE_VG250.gpkg", "vg250_krs", "district", key="OBJID", unpack=True),
}


def text(value: Any) -> str | None:
    """An attribute value as text, or None when the file has none (missing, NaN, NaT)."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    result = str(value)
    if result in ("NaT", "nan"):
        return None
    return result.strip() or None


def srid_of(crs: str | None, where: str) -> int:
    """The EPSG number of a layer's coordinate system; anything else is an error."""
    if crs and crs.upper().startswith("EPSG:") and crs[5:].isdigit():
        return int(crs[5:])
    msg = f"{where}: the coordinate system {crs!r} is not an EPSG code"
    raise OgrError(msg)


def _rows(
    path: str, theme: Theme, skip: int, limit: int | None
) -> tuple[int, list[tuple[int, bytes | None, dict[str, str | None]]]]:
    """One batch of (row number, WKB, attributes) and the SRID of the layer."""
    try:
        meta, fids, geometry, data = pyogrio.raw.read(
            path,
            layer=theme.layer,
            skip_features=skip,
            max_features=limit,
            return_fids=True,
        )
    except pyogrio.errors.DataLayerError as exc:
        msg = f"{theme.member}: no layer {theme.layer}"
        raise OgrError(msg) from exc
    srid = srid_of(meta.get("crs"), theme.member)
    names = [str(n) for n in meta["fields"]]
    rows = []
    for i, fid in enumerate(fids):
        wkb = geometry[i]
        attributes = {name: text(column[i]) for name, column in zip(names, data, strict=True)}
        rows.append((int(fid), None if wkb is None else bytes(wkb), attributes))
    return srid, rows


def _features(path: str, theme: Theme, *, batched: bool) -> Iterator[Feature]:
    seen: set[str] = set()
    skip = 0
    while True:
        srid, rows = _rows(path, theme, skip, BATCH if batched else None)
        for fid, wkb, attributes in rows:
            key = str(fid) if theme.key is None else attributes.get(theme.key)
            if not key:
                msg = f"{theme.member}: a feature without {theme.key or 'a row number'}"
                raise OgrError(msg)
            if key in seen:
                msg = f"{theme.member}: {theme.key or 'row number'} {key} occurs twice"
                raise OgrError(msg)
            seen.add(key)
            yield Feature(theme.stored_as, key, attributes, wkb, srid if wkb else 0)
        if not batched or not rows:
            return
        skip += len(rows)


def features(archive_path: Path, theme: Theme) -> Iterator[Feature]:
    """The features of one theme of one zip."""
    try:
        archive = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as exc:
        msg = f"{archive_path} is not a zip file"
        raise OgrError(msg) from exc
    with archive:
        member = _member(archive.namelist(), theme.member)
        if archive.getinfo(member).file_size > MAX_UNPACKED_BYTES:
            msg = f"{member}: the member unpacks to more than {MAX_UNPACKED_BYTES:,} bytes"
            raise OgrError(msg)
        if not theme.unpack:
            yield from _features(f"/vsizip/{archive_path}/{member}", theme, batched=False)
            return
        with tempfile.TemporaryDirectory(prefix="cabler-ogr-") as scratch:
            unpacked = Path(scratch) / Path(member).name
            with archive.open(member) as source, unpacked.open("wb") as target:
                shutil.copyfileobj(source, target, 1 << 22)
            yield from _features(str(unpacked), theme, batched=True)


def _member(names: list[str], wanted: str) -> str:
    """The member that is named ``wanted``, or, for a pattern such as ``*.gpkg``, the one match."""
    found = [n for n in names if fnmatch.fnmatchcase(n, wanted)]
    if len(found) != 1:
        msg = f"the archive has {len(found)} members matching {wanted}, expected exactly one"
        raise OgrError(msg)
    return found[0]


def _check_themes(themes: tuple[str, ...]) -> None:
    unknown = [name for name in themes if name not in THEMES]
    if unknown:
        msg = f"unknown themes {unknown}; known: {sorted(THEMES)}"
        raise OgrError(msg)


def read_dataset(path: Path, themes: tuple[str, ...]) -> Iterator[Feature]:
    """All features of the chosen themes from a zip."""
    _check_themes(themes)
    for name in themes:
        yield from features(path, THEMES[name])


def read_tileset(listing: Path, themes: tuple[str, ...]) -> Iterator[Feature]:
    """All features of the chosen themes from every tile of a tile-set snapshot.

    Each tile is checked against the checksum recorded for it before it is read. The identifier of
    a feature must be unique over the whole set; the database enforces that on insert.
    """
    _check_themes(themes)
    try:
        tiles = tileset.verified_tiles(listing)
    except tileset.TileSetError as exc:
        raise OgrError(str(exc)) from exc
    for _tile, path in tiles:
        for name in themes:
            yield from features(path, THEMES[name])
