import json
from typing import Any, Self

import httpx
import pytest

from cabler import pipeline, remote, sources

# Keep the real factory: the tests replace remote.make_client with one that uses a mock server.
REAL_MAKE_CLIENT = remote.make_client


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class FakeConnection:
    def __init__(self, registered: list[str]) -> None:
        self.registered = registered
        self.autocommit = False
        self.calls: list[tuple[str, Any]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: str, params: Any = None) -> FakeResult:
        self.calls.append((query, params))
        if "FROM meta.source WHERE source_id = ANY" in query:
            return FakeResult([(i,) for i in params[0] if i in self.registered])
        if "INSERT INTO meta.pipeline_run" in query:
            return FakeResult([(9,)])
        return FakeResult([])


def wire(
    monkeypatch: pytest.MonkeyPatch, handler: Any, registered: list[str] | None = None
) -> FakeConnection:
    conn = FakeConnection(list(sources.load_all()) if registered is None else registered)
    monkeypatch.setattr(pipeline.db, "connect", lambda role, environ=None: conn)
    monkeypatch.setattr(
        pipeline.remote, "make_client", lambda: REAL_MAKE_CLIENT(httpx.MockTransport(handler))
    )
    return conn


def ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"ETag": '"v1"', "Content-Length": "10"})


def test_check_prints_one_line_per_source(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = wire(monkeypatch, ok_handler)
    assert pipeline.main(["check", "dev"]) == 0
    out = capsys.readouterr().out
    assert conn.autocommit is True
    for source_id in sources.load_all():
        assert source_id in out
    assert out.count("new") == len(sources.load_all())


def test_check_can_print_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wire(monkeypatch, ok_handler)
    assert pipeline.main(["check", "dev", "osm_hamburg", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [item["source_id"] for item in data] == ["osm_hamburg"]
    assert data[0]["outcome"] == "new"
    assert data[0]["remote"]["etag"] == '"v1"'


def test_check_rejects_an_unknown_source(monkeypatch: pytest.MonkeyPatch) -> None:
    wire(monkeypatch, ok_handler)
    with pytest.raises(SystemExit, match="unknown source"):
        pipeline.main(["check", "dev", "nope"])


def test_check_asks_for_registration_first(monkeypatch: pytest.MonkeyPatch) -> None:
    wire(monkeypatch, ok_handler, registered=[])
    with pytest.raises(SystemExit, match=r"Run: python -m cabler\.pipeline register dev"):
        pipeline.main(["check", "dev", "osm_hamburg"])


def test_a_failing_publisher_gives_a_nonzero_exit_and_continues(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("hamburg-latest.osm.pbf"):
            return httpx.Response(500)
        return httpx.Response(200, headers={"ETag": '"v1"'})

    wire(monkeypatch, handler)
    assert pipeline.main(["check", "dev"]) == 1
    captured = capsys.readouterr()
    assert "ERROR osm_hamburg" in captured.err
    assert "osm_nrw" in captured.out
