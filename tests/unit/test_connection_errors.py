from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from cabler import fetch, remote
from cabler.fetch import DownloadError
from cabler.remote import ConnectionFailedError, RemoteError
from cabler.sources import SourceSpec

SpecFactory = Callable[..., SourceSpec]


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return remote.make_client(httpx.MockTransport(handler))


def test_a_timeout_becomes_a_remote_error_the_commands_can_report(make_spec: SpecFactory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no answer", request=request)

    with pytest.raises(ConnectionFailedError, match="ReadTimeout") as caught:
        remote.head(client_for(handler), make_spec())
    assert isinstance(caught.value, RemoteError)


def test_an_unreachable_host_is_reported_the_same_way(make_spec: SpecFactory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed", request=request)

    with pytest.raises(ConnectionFailedError, match="ConnectError"):
        remote.head(client_for(handler), make_spec())


class Breaking(httpx.SyncByteStream):
    """A body that delivers some bytes and then loses the connection."""

    def __iter__(self) -> Iterator[bytes]:
        yield b"partial"
        raise httpx.ReadError("connection reset")


def test_a_connection_that_breaks_mid_download_is_a_download_error(
    tmp_path: Path, make_spec: SpecFactory
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Breaking())

    with pytest.raises(DownloadError, match=r"the connection broke after \d+ bytes"):
        fetch.download(client_for(handler), make_spec(), tmp_path)
