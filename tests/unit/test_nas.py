import itertools
import math
import struct
import zipfile
from pathlib import Path

import pytest

from cabler import nas
from cabler.nas import NasError

HEAD = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<AX_Bestandsdatenauszug xmlns="http://www.adv-online.de/namespaces/adv/gid/7.1"'
    ' xmlns:gml="http://www.opengis.net/gml/3.2"><enthaelt><gml:FeatureCollection>'
)
TAIL = "</gml:FeatureCollection></enthaelt></AX_Bestandsdatenauszug>"


def line(*points: tuple[float, float]) -> str:
    coords = " ".join(f"{x} {y}" for x, y in points)
    return (
        "<gml:curveMember><gml:Curve><gml:segments><gml:LineStringSegment>"
        f"<gml:posList>{coords}</gml:posList></gml:LineStringSegment></gml:segments></gml:Curve>"
        "</gml:curveMember>"
    )


def arc(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> str:
    coords = f"{a[0]} {a[1]} {b[0]} {b[1]} {c[0]} {c[1]}"
    return (
        "<gml:curveMember><gml:Curve><gml:segments>"
        f'<gml:Arc numArc="1"><gml:posList>{coords}</gml:posList></gml:Arc>'
        "</gml:segments></gml:Curve></gml:curveMember>"
    )


def surface(*rings: str, srs: str = "urn:adv:crs:ETRS89_UTM32") -> str:
    exterior = f"<gml:exterior><gml:Ring>{rings[0]}</gml:Ring></gml:exterior>"
    holes = "".join(f"<gml:interior><gml:Ring>{r}</gml:Ring></gml:interior>" for r in rings[1:])
    return (
        f'<position><gml:Surface gml:id="S" srsName="{srs}"><gml:patches>'
        f"<gml:PolygonPatch>{exterior}{holes}</gml:PolygonPatch></gml:patches></gml:Surface>"
        "</position>"
    )


def square_ring(x: float, y: float, size: float = 10.0) -> str:
    a, b, c, d = (x, y), (x + size, y), (x + size, y + size), (x, y + size)
    return line(a, b) + line(b, c) + line(c, d) + line(d, a)


def parcel_xml(key: str | None, geometry: str, area: str = "100") -> str:
    key_xml = f"<flurstueckskennzeichen>{key}</flurstueckskennzeichen>" if key else ""
    return (
        f'<member><AX_Flurstueck gml:id="ID{key}">'
        f'<gml:identifier codeSpace="x">urn:adv:oid:ID{key}</gml:identifier>'
        "<lebenszeitintervall><AA_Lebenszeitintervall><beginnt>2023-10-25T08:05:02Z</beginnt>"
        "</AA_Lebenszeitintervall></lebenszeitintervall>"
        f"{geometry}"
        "<gemarkung><AX_Gemarkung_Schluessel><land>02</land>"
        "<gemarkungsnummer>0607</gemarkungsnummer></AX_Gemarkung_Schluessel></gemarkung>"
        "<flurstuecksnummer><AX_Flurstuecksnummer><zaehler>8752</zaehler><nenner>3</nenner>"
        "</AX_Flurstuecksnummer></flurstuecksnummer>"
        f'{key_xml}<amtlicheFlaeche uom="urn:adv:uom:m2">{area}</amtlicheFlaeche>'
        "<zeitpunktDerEntstehung>1998-10-16</zeitpunktDerEntstehung>"
        "<gemeindezugehoerigkeit><AX_Gemeindekennzeichen><land>02</land>"
        "<regierungsbezirk>0</regierungsbezirk><kreis>00</kreis><gemeinde>000</gemeinde>"
        "</AX_Gemeindekennzeichen></gemeindezugehoerigkeit>"
        "</AX_Flurstueck></member>"
    )


def document(*members: str) -> bytes:
    return (HEAD + "".join(members) + TAIL).encode("utf-8")


def decode(wkb: bytes) -> list[list[tuple[float, float]]]:
    """The rings of a little-endian MultiPolygon with one polygon."""
    assert struct.unpack("<BII", wkb[:9]) == (1, 6, 1)
    assert struct.unpack("<BI", wkb[9:14]) == (1, 3)
    (count,) = struct.unpack("<I", wkb[14:18])
    offset, rings = 18, []
    for _ in range(count):
        (n,) = struct.unpack("<I", wkb[offset : offset + 4])
        offset += 4
        data = struct.unpack(f"<{2 * n}d", wkb[offset : offset + 16 * n])
        rings.append(list(zip(data[0::2], data[1::2], strict=True)))
        offset += 16 * n
    return rings


def test_a_parcel_gets_its_attributes_as_text_and_its_outline_in_utm32() -> None:
    xml = document(parcel_xml("020607___08752______", surface(square_ring(100, 200))))
    (found,) = list(nas.parcels(xml))
    assert (found.layer, found.key, found.srid) == ("parcel_nas", "020607___08752______", 25832)
    assert found.attributes == {
        "gml_id": "ID020607___08752______",
        "identifier": "urn:adv:oid:ID020607___08752______",
        "beginnt": "2023-10-25T08:05:02Z",
        "land": "02",
        "gemarkungsnummer": "0607",
        "zaehler": "8752",
        "nenner": "3",
        "flurstueckskennzeichen": "020607___08752______",
        "amtlicheFlaeche": "100",
        "amtlicheFlaeche_uom": "urn:adv:uom:m2",
        "zeitpunktDerEntstehung": "1998-10-16",
        "regierungsbezirk": "0",
        "kreis": "00",
        "gemeinde": "000",
    }
    assert found.wkb is not None
    (ring,) = decode(found.wkb)
    assert ring == [(100, 200), (110, 200), (110, 210), (100, 210), (100, 200)]


def test_a_hole_becomes_an_inner_ring() -> None:
    xml = document(parcel_xml("K", surface(square_ring(0, 0, 100), square_ring(10, 10, 5))))
    (found,) = list(nas.parcels(xml))
    assert found.wkb is not None
    outer, hole = decode(found.wkb)
    assert outer[0] == outer[-1] == (0, 0)
    assert hole[0] == hole[-1] == (10, 10)


def test_an_arc_is_replaced_by_segments_within_five_millimetres_of_the_circle() -> None:
    # a half circle of radius 50 around (50, 0), closed by its diameter
    ring = arc((0, 0), (50, 50), (100, 0)) + line((100, 0), (0, 0))
    xml = document(parcel_xml("K", surface(ring)))
    (found,) = list(nas.parcels(xml))
    assert found.wkb is not None
    (points,) = decode(found.wkb)
    assert points[0] == points[-1] == (0, 0)
    assert len(points) > 20
    on_arc = points[1:-2]
    assert all(abs(math.hypot(x - 50, y) - 50) < 1e-6 for x, y in on_arc)
    chords = [math.dist(a, b) for a, b in itertools.pairwise(on_arc)]
    sagitta = max(50 - math.sqrt(50**2 - (c / 2) ** 2) for c in chords)
    assert sagitta <= nas.ARC_TOLERANCE + 1e-9
    assert any(abs(x - 50) < 0.5 and y > 49 for x, y in points)  # it passes through the top


def test_the_arc_goes_the_way_its_middle_point_says() -> None:
    clockwise = nas.arc_points((0, 0), (50, -50), (100, 0))
    assert all(y <= 1e-9 for _, y in clockwise)
    counter = nas.arc_points((0, 0), (50, 50), (100, 0))
    assert all(y >= -1e-9 for _, y in counter)
    assert nas.arc_points((0, 0), (1, 1), (2, 2)) == [(2, 2)]  # three points on a line


def test_a_parcel_without_an_outline_has_no_geometry() -> None:
    (found,) = list(nas.parcels(document(parcel_xml("K", ""))))
    assert found.wkb is None
    assert found.srid == 0


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ([parcel_xml(None, surface(square_ring(0, 0)))], "without flurstueckskennzeichen"),
        ([parcel_xml("K", surface(square_ring(0, 0), srs="urn:other"))], "is not known"),
        ([parcel_xml("K", surface(line((0, 0), (1, 0), (1, 1))))], "not closed"),
        ([parcel_xml("K", surface(line((0, 0), (1, 0)) + line((5, 5), (0, 0))))], "do not join"),
        ([parcel_xml("K", surface(line((0, 0))))], "coordinate list"),
        ([parcel_xml("K", "<position><gml:Point/></position>")], "not a surface"),
    ],
)
def test_unusable_parcels_are_refused(members: list[str], message: str) -> None:
    with pytest.raises(NasError, match=message):
        list(nas.parcels(document(*members)))


def make_zip(tmp_path: Path, parts: dict[str, bytes], name: str = "export.zip") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return path


def test_every_part_of_an_export_is_read_in_name_order(tmp_path: Path) -> None:
    one = document(parcel_xml("B", surface(square_ring(0, 0))))
    two = document(parcel_xml("A", surface(square_ring(50, 0))))
    path = make_zip(tmp_path, {"part_2.xml": two, "part_1.xml": one, "notes.txt": b"x"})
    assert [f.key for f in nas.read_export(path, ("parcel",))] == ["B", "A"]


def test_a_parcel_that_occurs_in_two_parts_is_an_error(tmp_path: Path) -> None:
    part = document(parcel_xml("A", surface(square_ring(0, 0))))
    path = make_zip(tmp_path, {"part_1.xml": part, "part_2.xml": part})
    with pytest.raises(NasError, match="occurs twice"):
        list(nas.read_export(path, ("parcel",)))


def test_bad_archives_and_themes_are_refused(tmp_path: Path) -> None:
    good = make_zip(tmp_path, {"p.xml": document(parcel_xml("A", surface(square_ring(0, 0))))})
    with pytest.raises(NasError, match="unknown themes"):
        list(nas.read_export(good, ("buildings",)))
    plain = tmp_path / "plain.zip"
    plain.write_text("no")
    with pytest.raises(NasError, match="not a zip"):
        list(nas.read_export(plain, ("parcel",)))
    empty = make_zip(tmp_path, {"readme.txt": b"x"}, "empty.zip")
    with pytest.raises(NasError, match="no XML part"):
        list(nas.read_export(empty, ("parcel",)))
