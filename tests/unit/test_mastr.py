import struct
import zipfile
from pathlib import Path

import pytest

from cabler import features, mastr
from cabler.features import Feature
from cabler.mastr import MastrError

UNITS = """<?xml version='1.0' encoding='UTF-16'?><EinheitenWind>
<EinheitWind><EinheitMastrNummer>SEE1</EinheitMastrNummer><Laengengrad>9.5</Laengengrad>
<Breitengrad>51.25</Breitengrad><EinheitBetriebsstatus>35</EinheitBetriebsstatus>
<NameWindpark>Park &amp; Co</NameWindpark><Typenbezeichnung> </Typenbezeichnung></EinheitWind>
<EinheitWind><EinheitMastrNummer>SEE2</EinheitMastrNummer><EinheitBetriebsstatus>31</EinheitBetriebsstatus>
</EinheitWind></EinheitenWind>"""
VALUES = """<?xml version='1.0' encoding='UTF-16'?><Katalogwerte>
<Katalogwert><Id>35</Id><Wert>In Betrieb</Wert>
<KatalogKategorieId>4</KatalogKategorieId></Katalogwert>
</Katalogwerte>"""
CATEGORIES = """<?xml version='1.0' encoding='UTF-16'?><Katalogkategorien>
<Katalogkategorie><Id>4</Id><Name>Betriebsstatus</Name></Katalogkategorie></Katalogkategorien>"""


def make_export(tmp_path: Path, units: str = UNITS) -> Path:
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("EinheitenWind.xml", units.encode("utf-16"))
        archive.writestr("Katalogwerte.xml", VALUES.encode("utf-16"))
        archive.writestr("Katalogkategorien.xml", CATEGORIES.encode("utf-16"))
    return path


def test_wind_units_and_the_catalogue_are_read_from_utf16_xml(tmp_path: Path) -> None:
    found = list(mastr.read_export(make_export(tmp_path), ("wind",)))
    assert [(f.layer, f.key) for f in found] == [
        ("catalogue_value", "35"),
        ("catalogue_category", "4"),
        ("wind_unit", "SEE1"),
        ("wind_unit", "SEE2"),
    ]
    first = found[2]
    assert first.attributes["NameWindpark"] == "Park & Co"
    assert first.attributes["Typenbezeichnung"] is None
    assert found[0].attributes == {"Id": "35", "Wert": "In Betrieb", "KatalogKategorieId": "4"}


def test_a_unit_with_coordinates_gets_a_wgs84_point_and_one_without_gets_none(
    tmp_path: Path,
) -> None:
    with_point, without = (
        f for f in mastr.read_export(make_export(tmp_path), ("wind",)) if f.layer == "wind_unit"
    )
    assert with_point.srid == mastr.WGS84
    assert with_point.wkb == struct.pack("<BIdd", 1, 1, 9.5, 51.25)
    assert without.wkb is None
    assert without.srid == 0


@pytest.mark.parametrize(
    ("longitude", "latitude", "expected"),
    [("9.5", "51.25", True), (None, "51", False), ("x", "51", False), ("nan", "51", False)],
)
def test_a_point_needs_two_real_numbers(
    longitude: str | None, latitude: str | None, expected: bool
) -> None:
    assert (mastr.point(longitude, latitude) is not None) is expected


def test_a_repeated_identifier_is_an_error(tmp_path: Path) -> None:
    doubled = UNITS.replace("SEE2", "SEE1")
    with pytest.raises(MastrError, match="SEE1 occurs twice"):
        list(mastr.read_export(make_export(tmp_path, doubled), ("wind",)))


def test_a_record_without_its_identifier_is_an_error(tmp_path: Path) -> None:
    broken = "<EinheitenWind><EinheitWind><Land>84</Land></EinheitWind></EinheitenWind>"
    with pytest.raises(MastrError, match="without EinheitMastrNummer"):
        list(mastr.read_export(make_export(tmp_path, broken), ("wind",)))


def test_unknown_themes_and_bad_archives_are_refused(tmp_path: Path) -> None:
    with pytest.raises(MastrError, match="unknown MaStR themes"):
        list(mastr.read_export(make_export(tmp_path), ("solar",)))
    not_a_zip = tmp_path / "plain.zip"
    not_a_zip.write_text("no")
    with pytest.raises(MastrError, match="not a zip"):
        list(mastr.read_export(not_a_zip, ("wind",)))
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as archive:
        archive.writestr("other.xml", "<a/>")
    with pytest.raises(MastrError, match=r"no file Katalogwerte\.xml"):
        list(mastr.read_export(empty, ("wind",)))


def test_the_content_hash_changes_with_attributes_geometry_and_system() -> None:
    base = Feature("l", "k", {"a": "1"}, b"\x01", 4326)
    assert base.content_hash == Feature("l", "k", {"a": "1"}, b"\x01", 4326).content_hash
    others = [
        Feature("l", "k", {"a": "2"}, b"\x01", 4326),
        Feature("l", "k", {"a": "1"}, b"\x02", 4326),
        Feature("l", "k", {"a": "1"}, b"\x01", 25832),
        Feature("l", "k", {"a": "1"}, None, 0),
    ]
    assert len({base.content_hash, *(o.content_hash for o in others)}) == 5
    assert features.content_hash({"b": "1", "a": "2"}, None, 0) == features.content_hash(
        {"a": "2", "b": "1"}, None, 0
    )
