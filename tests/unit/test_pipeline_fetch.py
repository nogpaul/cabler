from pathlib import Path
from typing import Any

import httpx
import pytest

from cabler import pipeline, remote

REAL_MAKE_CLIENT = remote.make_client
BODY = b"pbf-bytes" * 10


def wire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_connection: Any,
    known_rows: list[tuple[Any, ...]] | None = None,
) -> tuple[Any, list[str]]:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        headers = {"ETag": '"v1"', "Content-Length": str(len(BODY))}
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(BODY))

    conn = fake_connection(known_rows=known_rows)
    monkeypatch.setattr(pipeline.db, "connect", lambda role, environ=None: conn)
    monkeypatch.setattr(
        pipeline.remote, "make_client", lambda: REAL_MAKE_CLIENT(httpx.MockTransport(handler))
    )
    monkeypatch.setenv("CABLER_DATA_DIR", str(tmp_path))
    return conn, methods


def test_without_yes_only_the_plan_is_shown_and_nothing_is_downloaded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_connection: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, methods = wire(monkeypatch, tmp_path, fake_connection)
    assert pipeline.main(["fetch", "dev", "osm_hamburg"]) == 0
    out = capsys.readouterr().out
    assert "would download" in out
    assert "Nothing was downloaded" in out
    assert "GET" not in methods
    assert not (tmp_path / "dev").exists()


def test_with_yes_the_snapshot_is_downloaded_and_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_connection: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, methods = wire(monkeypatch, tmp_path, fake_connection)
    assert pipeline.main(["fetch", "dev", "osm_hamburg", "--yes"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("fetched")
    assert "GET" in methods
    snapshots = list((tmp_path / "dev" / "raw" / "osm_hamburg").iterdir())
    assert len(snapshots) == 1


def test_an_unchanged_source_is_reported_as_up_to_date(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_connection: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    known = [(3, '"v1"', None, len(BODY))]
    _, methods = wire(monkeypatch, tmp_path, fake_connection, known_rows=known)
    assert pipeline.main(["fetch", "dev", "osm_hamburg", "--yes"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("skipped")
    assert "up to date" in out
    assert methods == ["HEAD"]


def test_at_least_one_source_must_be_named(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_connection: Any
) -> None:
    wire(monkeypatch, tmp_path, fake_connection)
    with pytest.raises(SystemExit):
        pipeline.main(["fetch", "dev"])


def test_an_unknown_source_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_connection: Any
) -> None:
    wire(monkeypatch, tmp_path, fake_connection)
    with pytest.raises(SystemExit, match="unknown source"):
        pipeline.main(["fetch", "dev", "nope", "--yes"])


def test_a_failing_download_gives_a_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_connection: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wire(monkeypatch, tmp_path, fake_connection)
    monkeypatch.setattr(
        pipeline.remote,
        "make_client",
        lambda: REAL_MAKE_CLIENT(httpx.MockTransport(lambda request: httpx.Response(503))),
    )
    assert pipeline.main(["fetch", "dev", "osm_hamburg", "--yes"]) == 1
    assert "ERROR osm_hamburg" in capsys.readouterr().err
