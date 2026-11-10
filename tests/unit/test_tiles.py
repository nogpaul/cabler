import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from cabler import fetch, remote, sources, tiles
from cabler.sources import SourceConfigError, SourceSpec
from cabler.tiles import KnownTile, TileEntry, TilesError

SpecFactory = Callable[..., SourceSpec]

INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<opengeodata><datasets><dataset name="x"><files>
  <file name="b.tif" size="20" timestamp="2023-05-22T08:43:46" />
  <file name="a.tif" size="10" timestamp="2023-05-22T08:43:45" />
</files></dataset></datasets></opengeodata>
"""


def at(second: int) -> datetime:
    return datetime(2023, 5, 22, 8, 43, second, tzinfo=UTC)


def test_the_index_is_read_and_sorted_by_name() -> None:
    entries = tiles.parse_index(INDEX)
    assert [e.name for e in entries] == ["a.tif", "b.tif"]
    assert entries[0] == TileEntry("a.tif", 10, at(45))


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"not xml", "not valid XML"),
        (b"<opengeodata/>", "lists no files"),
        (b'<r><file name="a.tif" size="x" timestamp="2023-05-22T08:43:45"/></r>', "unusable"),
        (b'<r><file size="1" timestamp="2023-05-22T08:43:45"/></r>', "unusable"),
    ],
)
def test_a_bad_index_is_refused(body: bytes, message: str) -> None:
    with pytest.raises(TilesError, match=message):
        tiles.parse_index(body)


@pytest.mark.parametrize(
    "name",
    [
        "a.tif",
        "gru_vereinf_05117000_Mülheim_ad_Ruhr_EPSG25832_GeoPackage.zip",
        "x-1_2.y.zip",
        "Ñandú.zip",
    ],
)
def test_file_names_in_any_language_are_accepted(name: str) -> None:
    assert tiles.is_safe_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        ".hidden",
        "../x.tif",
        "a/b.tif",
        "a\\b.tif",
        "a b.tif",
        "a\nb.tif",
        "a\x00b.tif",
        "a?x=1.tif",
        "a#b.tif",
        "a%2fb.tif",
        "x" * 201,
    ],
)
def test_names_that_could_leave_a_folder_or_a_url_are_refused(name: str) -> None:
    assert not tiles.is_safe_name(name)


def test_an_index_with_an_unsafe_name_is_refused_before_anything_is_downloaded() -> None:
    body = b'<r><file name="../escape.tif" size="1" timestamp="2023-05-22T08:43:45"/></r>'
    with pytest.raises(TilesError, match="not a plain file name"):
        tiles.parse_index(body)


def known(name: str, size: int, second: int) -> KnownTile:
    return KnownTile(name, at(second), size, f"tiles/{name}", "a" * 64)


def test_tiles_are_sorted_into_new_changed_unchanged_and_removed() -> None:
    index = [
        TileEntry("new.tif", 5, at(1)),
        TileEntry("same.tif", 10, at(2)),
        TileEntry("resized.tif", 11, at(3)),
        TileEntry("retimed.tif", 10, at(9)),
    ]
    have = {
        "same.tif": known("same.tif", 10, 2),
        "resized.tif": known("resized.tif", 10, 3),
        "retimed.tif": known("retimed.tif", 10, 3),
        "gone.tif": known("gone.tif", 10, 1),
    }
    result = tiles.plan(index, have)
    assert [e.name for e in result.new] == ["new.tif"]
    assert [e.name for e in result.changed] == ["resized.tif", "retimed.tif"]
    assert result.unchanged == 1
    assert result.removed == ("gone.tif",)
    assert [e.name for e in result.todo] == ["new.tif", "resized.tif", "retimed.tif"]


def tile_spec(make_spec: SpecFactory) -> SourceSpec:
    return dataclasses.replace(
        make_spec(max_bytes=1000, url="https://data.example.org/tiles/"), tiles_kind="x"
    )


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return remote.make_client(httpx.MockTransport(handler))


def test_a_flaky_tile_is_retried_until_it_arrives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_spec: SpecFactory
) -> None:
    monkeypatch.setattr(tiles.time, "sleep", lambda seconds: None)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, stream=httpx.ByteStream(b"12345"))

    got = tiles._download_one(
        client_for(handler), tile_spec(make_spec), TileEntry("a.tif", 5, at(1)), tmp_path
    )
    assert len(calls) == 3
    assert got.size == 5


def test_a_tile_of_the_wrong_size_is_refused(tmp_path: Path, make_spec: SpecFactory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b"123"))

    with pytest.raises(fetch.DownloadError, match="the index says 5"):
        tiles._download_one(
            client_for(handler), tile_spec(make_spec), TileEntry("a.tif", 5, at(1)), tmp_path
        )
    assert list(tmp_path.iterdir()) == []


def test_a_tile_that_keeps_failing_gives_up_with_the_last_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_spec: SpecFactory
) -> None:
    monkeypatch.setattr(tiles.time, "sleep", lambda seconds: None)
    client = client_for(lambda request: httpx.Response(503))
    with pytest.raises(remote.RemoteStatusError):
        tiles._download_one(client, tile_spec(make_spec), TileEntry("a.tif", 5, at(1)), tmp_path)


def test_a_foreign_host_is_not_retried(tmp_path: Path, make_spec: SpecFactory) -> None:
    spec = dataclasses.replace(tile_spec(make_spec), allowed_hosts=("other.org",))
    client = client_for(lambda request: httpx.Response(200))
    with pytest.raises(remote.HostNotAllowedError):
        tiles._download_one(client, spec, TileEntry("a.tif", 5, at(1)), tmp_path)


def test_a_source_without_a_tiles_section_is_refused(make_spec: SpecFactory) -> None:
    with pytest.raises(TilesError, match=r"no \[tiles\] section"):
        tiles.fetch_tiles(None, None, make_spec(), Path("."))  # type: ignore[arg-type]


def test_the_tiles_section_is_validated(tmp_path: Path) -> None:
    text = """\
source_id = "demo"
title = "Demo"
publisher = "Someone"
licence_id = "dl-de/zero-2-0"
licence_url = "https://www.govdata.de/dl-de/zero-2-0"
attribution = "Demo credit"

[download]
url = "https://example.org/tiles/"
allowed_hosts = ["example.org"]
max_bytes = 1000
format = "tif"
"""
    path = tmp_path / "demo.toml"
    path.write_text(text + '\n[tiles]\nkind = "opengeodata_index"\n', encoding="utf-8")
    assert sources.load_source(path).tiles_kind == "opengeodata_index"
    path.write_text(text + '\n[tiles]\nkind = "ftp"\n', encoding="utf-8")
    with pytest.raises(SourceConfigError, match=r"tiles\.kind must be one of"):
        sources.load_source(path)
    path.write_text('tiles = "yes"\n' + text, encoding="utf-8")
    with pytest.raises(SourceConfigError, match=r"\[tiles\] must be a table"):
        sources.load_source(path)


@pytest.mark.parametrize("name", ["../x.tif", "sub/a.tif", ".hidden", "a b.tif"])
def test_the_store_checks_a_name_again_with_the_rule_of_the_index(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(TilesError, match="plain file name"):
        tiles._target(tmp_path, name)


def test_a_tiles_folder_that_is_a_link_to_another_disk_is_accepted(tmp_path: Path) -> None:
    elsewhere = tmp_path / "other_disk"
    elsewhere.mkdir()
    folder = tmp_path / "tiles"
    folder.symlink_to(elsewhere)
    assert tiles._target(folder, "a.tif") == folder / "a.tif"
