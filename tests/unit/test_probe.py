from pathlib import Path

import httpx
import pytest

from cabler import checks, remote, sources
from cabler.remote import HostNotAllowedError, RemoteState
from cabler.sources import SourceConfigError, SourceSpec

VALID = """\
source_id = "demo"
title = "Demo"
publisher = "Someone"
licence_id = "dl-de/zero-2-0"
licence_url = "https://www.govdata.de/dl-de/zero-2-0"
attribution = "Demo credit"

[download]
url = "https://example.org/wfs?service=WFS"
allowed_hosts = ["example.org"]
max_bytes = 1000
format = "gpkg.zip"
"""


def spec_with(tmp_path: Path, extra: str) -> SourceSpec:
    path = tmp_path / "demo.toml"
    path.write_text(
        VALID.replace('format = "gpkg.zip"\n', 'format = "gpkg.zip"\n' + extra), encoding="utf-8"
    )
    return sources.load_source(path)


def test_a_source_is_probed_with_head_by_default(tmp_path: Path) -> None:
    assert spec_with(tmp_path, "").probe == "head"


def test_a_source_can_opt_out_of_probing(tmp_path: Path) -> None:
    assert spec_with(tmp_path, 'probe = "none"\n').probe == "none"


def test_an_unknown_probe_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SourceConfigError, match=r"download\.probe must be one of"):
        spec_with(tmp_path, 'probe = "ping"\n')


def test_a_source_without_probing_sends_no_request(tmp_path: Path) -> None:
    spec = spec_with(tmp_path, 'probe = "none"\n')
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.method)
        return httpx.Response(500)

    client = remote.make_client(httpx.MockTransport(handler))
    state = remote.head(client, spec)
    assert sent == []
    assert state == RemoteState(url=spec.url, etag=None, last_modified=None, content_length=None)


def test_a_source_without_probing_still_obeys_the_host_list(tmp_path: Path) -> None:
    spec = spec_with(tmp_path, 'probe = "none"\n')
    elsewhere = SourceSpec(**{**spec.__dict__, "allowed_hosts": ("other.org",)})
    client = remote.make_client(httpx.MockTransport(lambda request: httpx.Response(200)))
    with pytest.raises(HostNotAllowedError):
        remote.head(client, elsewhere)


def test_nothing_known_about_a_probeless_source_means_unverifiable_once_fetched() -> None:
    state = RemoteState(
        url="https://example.org/x", etag=None, last_modified=None, content_length=None
    )
    known = checks.Known(version_id=3, etag=None, last_modified=None, content_length=None)
    assert checks.decide(state, None) == "new"
    assert checks.decide(state, known) == "unverifiable"
