from collections.abc import Callable

import httpx
import pytest

from cabler import remote
from cabler.remote import (
    HostNotAllowedError,
    RemoteStatusError,
    TooLargeError,
    TooManyRedirectsError,
)
from cabler.sources import SourceSpec

SpecFactory = Callable[..., SourceSpec]

URL = "https://data.example.org/file.bin"


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return remote.make_client(httpx.MockTransport(handler))


def test_head_reads_the_validators(make_spec: SpecFactory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "HEAD"
        return httpx.Response(
            200,
            headers={
                "ETag": '"abc"',
                "Last-Modified": "Sat, 07 Nov 2026 02:08:45 GMT",
                "Content-Length": "54130091",
            },
        )

    state = remote.head(client_for(handler), make_spec(max_bytes=100_000_000))
    assert state.etag == '"abc"'
    assert state.content_length == 54130091
    assert state.last_modified is not None
    assert state.last_modified.isoformat() == "2026-11-07T02:08:45+00:00"
    assert state.url == URL


def test_redirects_to_an_allowed_host_are_followed(make_spec: SpecFactory) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "data.example.org":
            return httpx.Response(307, headers={"Location": "https://cdn.example.org/real.bin"})
        return httpx.Response(200, headers={"Content-Length": "10"})

    state = remote.head(client_for(handler), make_spec())
    assert state.url == "https://cdn.example.org/real.bin"
    assert seen == [URL, "https://cdn.example.org/real.bin"]


def test_a_redirect_to_a_foreign_host_is_refused_and_never_requested(
    make_spec: SpecFactory,
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://evil.example.net/x.bin"})

    with pytest.raises(HostNotAllowedError, match=r"evil\.example\.net"):
        remote.head(client_for(handler), make_spec())
    assert seen == ["data.example.org"]


def test_a_downgrade_to_plain_http_is_refused(make_spec: SpecFactory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"Location": "http://data.example.org/file.bin"})

    with pytest.raises(HostNotAllowedError, match="not an https URL"):
        remote.head(client_for(handler), make_spec())


def test_redirect_loops_stop(make_spec: SpecFactory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": URL})

    with pytest.raises(TooManyRedirectsError):
        remote.head(client_for(handler), make_spec())


def test_error_statuses_are_reported(make_spec: SpecFactory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(RemoteStatusError, match="404"):
        remote.head(client_for(handler), make_spec())


def test_servers_that_reject_head_are_asked_with_get(make_spec: SpecFactory) -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(200, headers={"ETag": '"x"'}, content=b"body that is never read")

    state = remote.head(client_for(handler), make_spec())
    assert methods == ["HEAD", "GET"]
    assert state.etag == '"x"'


def test_an_announced_size_above_the_limit_is_refused(make_spec: SpecFactory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": "5000"})

    with pytest.raises(TooLargeError, match="exceeds the limit"):
        remote.head(client_for(handler), make_spec(max_bytes=1000))


def test_unusable_headers_become_none(make_spec: SpecFactory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Last-Modified": "garbage", "Content-Length": "x"})

    state = remote.head(client_for(handler), make_spec())
    assert state.last_modified is None
    assert state.content_length is None
    assert state.etag is None
