"""Reader for the Marktstammdatenregister bulk export (a zip of UTF-16 XML files).

Only the files a theme needs are read, straight from the zip and one record at a time, so a 67 GB
export never has to be unpacked. Every record becomes a :class:`cabler.features.Feature` whose
attributes are the XML children as delivered. Coded values (operating status, state, manufacturer)
stay codes here; the code catalogue is loaded alongside so that staging can decode them.
"""

from __future__ import annotations

import struct
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from defusedxml import ElementTree

from cabler.features import Feature

WGS84 = 4326


class MastrError(Exception):
    """The export does not look like a MaStR bulk export, or a record is unusable."""


@dataclass(frozen=True)
class Theme:
    """One kind of record: where it lives in the zip and which child is its identifier."""

    member: str
    layer: str
    record: str
    key: str
    longitude: str | None = None
    latitude: str | None = None


THEMES: dict[str, Theme] = {
    "wind": Theme(
        "EinheitenWind.xml",
        "wind_unit",
        "EinheitWind",
        "EinheitMastrNummer",
        longitude="Laengengrad",
        latitude="Breitengrad",
    ),
}
CATALOGUES: tuple[Theme, ...] = (
    Theme("Katalogwerte.xml", "catalogue_value", "Katalogwert", "Id"),
    Theme("Katalogkategorien.xml", "catalogue_category", "Katalogkategorie", "Id"),
)


def records(archive: zipfile.ZipFile, theme: Theme) -> Iterator[dict[str, str | None]]:
    """Yield each record of a member as {child name: text}; empty children become None."""
    try:
        member = archive.open(theme.member)
    except KeyError as exc:
        msg = f"the export has no file {theme.member}"
        raise MastrError(msg) from exc
    with member:
        for _, element in ElementTree.iterparse(member, events=("end",)):
            if element.tag != theme.record:
                continue
            yield {
                child.tag: (child.text.strip() or None) if child.text is not None else None
                for child in element
            }
            element.clear()


def point(longitude: str | None, latitude: str | None) -> bytes | None:
    """A WKB point from two decimal strings, or None when either is missing or not a number."""
    if longitude is None or latitude is None:
        return None
    try:
        x, y = float(longitude), float(latitude)
    except ValueError:
        return None
    if x != x or y != y:  # NaN
        return None
    return struct.pack("<BIdd", 1, 1, x, y)


def features(archive: zipfile.ZipFile, theme: Theme) -> Iterator[Feature]:
    """The features of one theme. A repeated identifier is an error, never silently merged."""
    seen: set[str] = set()
    for record in records(archive, theme):
        key = record.get(theme.key)
        if not key:
            msg = f"{theme.member}: a record without {theme.key}"
            raise MastrError(msg)
        if key in seen:
            msg = f"{theme.member}: {theme.key} {key} occurs twice"
            raise MastrError(msg)
        seen.add(key)
        wkb = None
        if theme.longitude and theme.latitude:
            wkb = point(record.get(theme.longitude), record.get(theme.latitude))
        yield Feature(theme.layer, key, record, wkb, WGS84 if wkb else 0)


def read_export(path: Path, themes: tuple[str, ...]) -> Iterator[Feature]:
    """All features of the chosen themes plus the code catalogues, from a bulk-export zip."""
    unknown = [name for name in themes if name not in THEMES]
    if unknown:
        msg = f"unknown MaStR themes {unknown}; known: {sorted(THEMES)}"
        raise MastrError(msg)
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        msg = f"{path} is not a zip file"
        raise MastrError(msg) from exc
    with archive:
        for catalogue in CATALOGUES:
            yield from features(archive, catalogue)
        for name in themes:
            yield from features(archive, THEMES[name])
