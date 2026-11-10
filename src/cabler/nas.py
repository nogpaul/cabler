"""Reader for ALKIS exchange files (NAS) as Hamburg publishes them: a zip of XML parts.

GDAL has no NAS driver here, and its generic GML driver returns only the label point of a parcel
(``objektkoordinaten``), not its outline (``position``). This reader streams every part straight
from the zip, one parcel at a time, and builds the outline itself: a surface with one polygon
patch whose rings are chains of curve members, each a straight segment or a circular arc through
three points. Arcs are replaced by short straight segments that stay within 5 mm of the arc.

Every parcel becomes a :class:`cabler.features.Feature` whose attributes are the values of the
file as text and whose geometry is a MultiPolygon in EPSG:25832.
"""

from __future__ import annotations

import io
import math
import struct
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import IO
from xml.etree.ElementTree import Element

from defusedxml import ElementTree

from cabler.features import Feature

SRS = {"urn:adv:crs:ETRS89_UTM32": 25832}
ARC_TOLERANCE = 0.005  # metres between an arc and its straight segments
MAX_PART_BYTES = 1 << 30  # one XML part may not unpack to more than this
THEMES = {"parcel": "parcel_nas"}  # theme -> name under which the features are stored


class NasError(Exception):
    """The file is not an ALKIS export of the expected kind, or a parcel is unusable."""


def local(tag: str) -> str:
    """The name of an element without its namespace."""
    return tag.rsplit("}", 1)[-1]


def child(element: Element, name: str) -> Element | None:
    """The first direct child with this local name."""
    for item in element:
        if local(item.tag) == name:
            return item
    return None


def descend(element: Element | None, *names: str) -> Element | None:
    """Follow a chain of child names; None as soon as one is missing."""
    for name in names:
        if element is None:
            return None
        element = child(element, name)
    return element


def text_of(element: Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    return element.text.strip() or None


def arc_points(
    p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float]
) -> list[tuple[float, float]]:
    """Points along the circular arc p1 -> p2 -> p3, ending in p3 and not repeating p1."""
    ox, oy = p1  # work relative to the first point: squares of UTM coordinates lose precision
    ax, ay = 0.0, 0.0
    bx, by = p2[0] - ox, p2[1] - oy
    cx, cy = p3[0] - ox, p3[1] - oy
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:  # the three points are on a line: a straight segment
        return [p3]
    ux = (
        (ax**2 + ay**2) * (by - cy) + (bx**2 + by**2) * (cy - ay) + (cx**2 + cy**2) * (ay - by)
    ) / d
    uy = (
        (ax**2 + ay**2) * (cx - bx) + (bx**2 + by**2) * (ax - cx) + (cx**2 + cy**2) * (bx - ax)
    ) / d
    radius = math.hypot(ax - ux, ay - uy)
    start = math.atan2(ay - uy, ax - ux)
    mid = math.atan2(by - uy, bx - ux)
    end = math.atan2(cy - uy, cx - ux)
    # the sweep goes from start through mid to end, counter-clockwise or clockwise
    sweep = (end - start) % (2 * math.pi)
    if (mid - start) % (2 * math.pi) > sweep:
        sweep -= 2 * math.pi
    step = 2 * math.acos(max(0.0, 1.0 - ARC_TOLERANCE / radius)) if radius > ARC_TOLERANCE else 1.0
    pieces = max(1, math.ceil(abs(sweep) / max(step, 1e-6)))
    points = [
        (
            ox + ux + radius * math.cos(start + sweep * i / pieces),
            oy + uy + radius * math.sin(start + sweep * i / pieces),
        )
        for i in range(1, pieces)
    ]
    return [*points, p3]


def pairs(pos_list: str, where: str) -> list[tuple[float, float]]:
    numbers = pos_list.split()
    if len(numbers) % 2 or len(numbers) < 4:
        msg = f"{where}: a coordinate list of {len(numbers)} numbers"
        raise NasError(msg)
    try:
        values = [float(n) for n in numbers]
    except ValueError as exc:
        msg = f"{where}: a coordinate that is not a number"
        raise NasError(msg) from exc
    return list(zip(values[0::2], values[1::2], strict=True))


def ring(element: Element, where: str) -> list[tuple[float, float]]:
    """The closed ring of a ``gml:Ring``: its curve members joined end to start."""
    points: list[tuple[float, float]] = []
    for member in element:
        if local(member.tag) != "curveMember":
            continue
        segments = descend(member[0] if len(member) else None, "segments")
        if segments is None:
            msg = f"{where}: a ring member without segments"
            raise NasError(msg)
        for segment in segments:
            kind = local(segment.tag)
            coords = pairs(text_of(child(segment, "posList")) or "", where)
            if kind == "LineStringSegment":
                new = coords
            elif kind == "Arc" and len(coords) == 3:
                new = arc_points(*coords)
                new.insert(0, coords[0])
            else:
                msg = f"{where}: a segment of kind {kind} with {len(coords)} points"
                raise NasError(msg)
            if points and new[0] != points[-1]:
                msg = f"{where}: the segments of a ring do not join up"
                raise NasError(msg)
            points.extend(new if not points else new[1:])
    if len(points) < 4 or points[0] != points[-1]:
        msg = f"{where}: a ring that is not closed"
        raise NasError(msg)
    return points


def outline(position: Element, where: str) -> tuple[bytes, int]:
    """WKB (MultiPolygon, one polygon) and SRID of a ``position`` element."""
    surface = child(position, "Surface")
    if surface is None:
        msg = f"{where}: the position is not a surface"
        raise NasError(msg)
    name = surface.attrib.get("srsName", "")
    if name not in SRS:
        msg = f"{where}: the coordinate system {name!r} is not known"
        raise NasError(msg)
    patches = descend(surface, "patches")
    found = [p for p in patches if local(p.tag) == "PolygonPatch"] if patches is not None else []
    if len(found) > 1:
        msg = f"{where}: the surface has {len(found)} patches; only one is supported"
        raise NasError(msg)
    patch = found[0] if found else None
    exterior = descend(patch, "exterior", "Ring")
    if patch is None or exterior is None:
        msg = f"{where}: the surface has no outer ring"
        raise NasError(msg)
    if sum(local(item.tag) == "exterior" for item in patch) > 1:
        msg = f"{where}: the patch has more than one exterior"
        raise NasError(msg)
    rings = [ring(exterior, where)]
    rings += [ring(item[0], where) for item in patch if local(item.tag) == "interior" and len(item)]
    body = struct.pack("<BII", 1, 3, len(rings))
    for points in rings:
        body += struct.pack("<I", len(points)) + b"".join(struct.pack("<dd", *p) for p in points)
    return struct.pack("<BII", 1, 6, 1) + body, SRS[name]


def parcel(element: Element) -> Feature | None:
    """The feature of one ``AX_Flurstueck`` element."""
    key = text_of(child(element, "flurstueckskennzeichen"))
    if not key:
        msg = "a parcel without flurstueckskennzeichen"
        raise NasError(msg)
    gml_id = next((v for k, v in element.attrib.items() if local(k) == "id"), None)
    municipality = descend(element, "gemeindezugehoerigkeit", "AX_Gemeindekennzeichen")
    cadastral = descend(element, "gemarkung", "AX_Gemarkung_Schluessel")
    number = descend(element, "flurstuecksnummer", "AX_Flurstuecksnummer")
    area = child(element, "amtlicheFlaeche")
    attributes: dict[str, str | None] = {
        "gml_id": gml_id,
        "identifier": text_of(child(element, "identifier")),
        "beginnt": text_of(
            descend(element, "lebenszeitintervall", "AA_Lebenszeitintervall", "beginnt")
        ),
        "land": text_of(child(cadastral, "land")) if cadastral is not None else None,
        "gemarkungsnummer": text_of(child(cadastral, "gemarkungsnummer"))
        if cadastral is not None
        else None,
        "zaehler": text_of(child(number, "zaehler")) if number is not None else None,
        "nenner": text_of(child(number, "nenner")) if number is not None else None,
        "flurstueckskennzeichen": key,
        "amtlicheFlaeche": text_of(area),
        "amtlicheFlaeche_uom": area.attrib.get("uom") if area is not None else None,
        "zeitpunktDerEntstehung": text_of(child(element, "zeitpunktDerEntstehung")),
        "regierungsbezirk": text_of(child(municipality, "regierungsbezirk"))
        if municipality is not None
        else None,
        "kreis": text_of(child(municipality, "kreis")) if municipality is not None else None,
        "gemeinde": text_of(child(municipality, "gemeinde")) if municipality is not None else None,
    }
    position = child(element, "position")
    if position is None:
        return Feature(THEMES["parcel"], key, attributes)
    wkb, srid = outline(position, key)
    return Feature(THEMES["parcel"], key, attributes, wkb, srid)


def parcels(source: bytes | IO[bytes]) -> Iterator[Feature]:
    """The parcels of one XML part. Each ``member`` is released as soon as it was read."""
    handle = io.BytesIO(source) if isinstance(source, bytes) else source
    stack: list[Element] = []
    for event, element in ElementTree.iterparse(handle, events=("start", "end")):
        if event == "start":
            stack.append(element)
            continue
        stack.pop()
        if local(element.tag) != "member":
            continue
        for item in element:
            if local(item.tag) == "AX_Flurstueck":
                feature = parcel(item)
                if feature is not None:
                    yield feature
        element.clear()
        if stack:
            stack[-1].remove(element)  # the cleared shell would stay attached to its parent


def read_export(path: Path, themes: tuple[str, ...]) -> Iterator[Feature]:
    """All parcels of an export zip, part by part. A repeated parcel key is an error."""
    unknown = [name for name in themes if name not in THEMES]
    if unknown:
        msg = f"unknown themes {unknown}; known: {sorted(THEMES)}"
        raise NasError(msg)
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        msg = f"{path} is not a zip file"
        raise NasError(msg) from exc
    seen: set[str] = set()
    with archive:
        parts = sorted(n for n in archive.namelist() if n.lower().endswith(".xml"))
        if not parts:
            msg = f"{path} holds no XML part"
            raise NasError(msg)
        for part in parts:
            if archive.getinfo(part).file_size > MAX_PART_BYTES:
                msg = f"{part}: the part unpacks to more than {MAX_PART_BYTES:,} bytes"
                raise NasError(msg)
            with archive.open(part) as handle:
                for feature in parcels(handle):
                    if feature.key in seen:
                        msg = f"{part}: the parcel {feature.key} occurs twice"
                        raise NasError(msg)
                    seen.add(feature.key)
                    yield feature
