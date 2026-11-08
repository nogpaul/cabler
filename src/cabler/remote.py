"""HTTP access with a host allow-list.

Every request, and every redirect hop, must target an ``https`` URL on a host named in the source's
``allowed_hosts``. A publisher that redirects to somewhere else is refused, never followed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx

from cabler import __version__
from cabler.sources import SourceSpec

MAX_REDIRECTS = 5
FALLBACK_STATUSES = frozenset({405, 501})
TIMEOUT = httpx.Timeout(180.0, connect=10.0)


class RemoteError(Exception):
    """Base class for problems talking to a publisher."""


class HostNotAllowedError(RemoteError):
    """A URL or redirect target is not https on an allowed host."""


class TooManyRedirectsError(RemoteError):
    """The publisher redirected more often than allowed."""


class RemoteStatusError(RemoteError):
    """The publisher answered with an error status."""


class ConnectionFailedError(RemoteError):
    """The publisher could not be reached, or stopped answering (timeout, reset, DNS)."""


class TooLargeError(RemoteError):
    """The announced size is above the limit set for the source."""


@dataclass(frozen=True)
class RemoteState:
    """What the publisher says about a file, without downloading it."""

    url: str
    etag: str | None
    last_modified: datetime | None
    content_length: int | None


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """An HTTP client that never follows redirects by itself."""
    return httpx.Client(
        headers={
            "User-Agent": f"cabler-pipeline/{__version__}",
            "Accept-Encoding": "identity",
        },
        timeout=TIMEOUT,
        follow_redirects=False,
        transport=transport,
    )


def _allowed(url: str, spec: SourceSpec) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname in spec.allowed_hosts


def send(
    client: httpx.Client, method: str, spec: SourceSpec, *, stream: bool = False
) -> httpx.Response:
    """Send a request to the source URL, following redirects only to allowed hosts."""
    url = spec.url
    for _ in range(MAX_REDIRECTS + 1):
        if not _allowed(url, spec):
            msg = f"refusing {url}: not an https URL on an allowed host {list(spec.allowed_hosts)}"
            raise HostNotAllowedError(msg)
        try:
            response = client.send(client.build_request(method, url), stream=stream)
        except httpx.TransportError as exc:
            msg = f"{url}: {type(exc).__name__}: {exc}"
            raise ConnectionFailedError(msg) from exc
        if not response.is_redirect:
            return response
        location = response.headers["location"]
        response.close()
        url = urljoin(url, location)
    msg = f"more than {MAX_REDIRECTS} redirects for {spec.url}"
    raise TooManyRedirectsError(msg)


def _to_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _to_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def state_of(response: httpx.Response, spec: SourceSpec) -> RemoteState:
    """Read the validators from a response and enforce the source's size limit."""
    if not response.is_success:
        msg = f"{spec.url}: the server answered {response.status_code}"
        raise RemoteStatusError(msg)
    state = RemoteState(
        url=str(response.url),
        etag=response.headers.get("etag"),
        last_modified=_to_datetime(response.headers.get("last-modified")),
        content_length=_to_int(response.headers.get("content-length")),
    )
    if state.content_length is not None and state.content_length > spec.max_bytes:
        msg = (
            f"{spec.url}: announced size {state.content_length} exceeds the limit {spec.max_bytes}"
        )
        raise TooLargeError(msg)
    return state


def head(client: httpx.Client, spec: SourceSpec) -> RemoteState:
    """Ask the publisher about the file. Falls back to a GET whose body is never read.

    A source with ``probe = "none"`` (a service that cannot answer HEAD, such as a WFS)
    is not asked: nothing is known about it, so the check says so and a fetch downloads.
    """
    if spec.probe == "none":
        if not _allowed(spec.url, spec):
            msg = f"refusing {spec.url}: not an https URL on an allowed host"
            raise HostNotAllowedError(msg)
        return RemoteState(url=spec.url, etag=None, last_modified=None, content_length=None)
    response = send(client, "HEAD", spec)
    if response.status_code in FALLBACK_STATUSES:
        response.close()
        response = send(client, "GET", spec, stream=True)
    try:
        return state_of(response, spec)
    finally:
        response.close()
