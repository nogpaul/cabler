import hashlib
from collections import namedtuple
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cabler import fetch, remote
from cabler.fetch import DownloadError, NotEnoughSpaceError
from cabler.remote import TooLargeError
from cabler.sources import SourceSpec

SpecFactory = Callable[..., SourceSpec]
BODY = b"0123456789" * 100  # 1000 bytes
SHA = hashlib.sha256(BODY).hexdigest()


def server(
    body: bytes = BODY, *, etag: str = '"v1"', announce: int | None = None
) -> tuple[httpx.Client, list[str]]:
    """A pretend publisher. Returns the client and the list of request methods it received."""
    methods: list[str] = []
    length = len(body) if announce is None else announce

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        headers = {"ETag": etag, "Content-Length": str(length)}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))

    return remote.make_client(httpx.MockTransport(handler)), methods


def test_download_stores_the_exact_bytes_and_their_checksum(
    tmp_path: Path, make_spec: SpecFactory
) -> None:
    client, _ = server()
    got = fetch.download(client, make_spec(), tmp_path)
    assert got.sha256 == SHA
    assert got.size == len(BODY)
    assert got.path.name == "file.bin"
    assert got.path.read_bytes() == BODY


def test_download_stops_when_more_bytes_arrive_than_allowed(
    tmp_path: Path, make_spec: SpecFactory
) -> None:
    client, _ = server(b"x" * 5000, announce=10)
    with pytest.raises(TooLargeError, match="more than 1000 bytes"):
        fetch.download(client, make_spec(max_bytes=1000), tmp_path)


def test_an_incomplete_download_is_rejected(tmp_path: Path, make_spec: SpecFactory) -> None:
    client, _ = server(b"short", announce=100)
    with pytest.raises(DownloadError, match="incomplete download"):
        fetch.download(client, make_spec(), tmp_path)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://data.example.org/a/hamburg-261107.osm.pbf", "hamburg-261107.osm.pbf"),
        ("https://data.example.org/a/dir/", "demo.bin"),
        ("https://data.example.org/a/we ird.bin", "demo.bin"),
    ],
)
def test_file_names_are_safe(url: str, expected: str, make_spec: SpecFactory) -> None:
    assert fetch.filename_for(make_spec(), url) == expected


def test_not_enough_disk_space_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(fetch.shutil, "disk_usage", lambda path: usage(100, 99, 1))
    with pytest.raises(NotEnoughSpaceError, match="only 1 bytes free"):
        fetch.ensure_space(tmp_path, 1000)


def test_the_data_directory_can_be_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CABLER_DATA_DIR", str(tmp_path))
    assert fetch.raw_dir("dev") == tmp_path / "dev" / "raw"
    monkeypatch.delenv("CABLER_DATA_DIR")
    assert fetch.data_root().name == "data"
