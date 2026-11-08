from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from cabler import checks, remote
from cabler.checks import Known
from cabler.remote import RemoteState, RemoteStatusError
from cabler.sources import SourceSpec

SpecFactory = Callable[..., SourceSpec]

T1 = datetime(2026, 11, 7, 2, 8, 45, tzinfo=UTC)
T2 = datetime(2026, 11, 8, 2, 8, 45, tzinfo=UTC)


def state(
    etag: str | None = None, modified: datetime | None = None, size: int | None = None
) -> RemoteState:
    return RemoteState("https://data.example.org/file.bin", etag, modified, size)


@pytest.mark.parametrize(
    ("remote_state", "known", "expected"),
    [
        (state('"a"'), None, "new"),
        (state('"a"'), Known(1, '"a"', None, None), "unchanged"),
        (state('"b"'), Known(1, '"a"', None, None), "changed"),
        (state(None, T1, 10), Known(1, None, T1, 10), "unchanged"),
        (state(None, T2, 10), Known(1, None, T1, 10), "changed"),
        (state(None, T1, 11), Known(1, None, T1, 10), "changed"),
        (state(None, None, None), Known(1, None, None, None), "unverifiable"),
        (state('"a"'), Known(1, None, None, None), "unverifiable"),
    ],
)
def test_decide(remote_state: RemoteState, known: Known | None, expected: str) -> None:
    assert checks.decide(remote_state, known) == expected


def test_the_etag_wins_over_the_modification_date() -> None:
    remote_state = state('"same"', T2, 99)
    assert checks.decide(remote_state, Known(1, '"same"', T1, 10)) == "unchanged"


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class FakeConnection:
    def __init__(self, known_rows: list[tuple[Any, ...]]) -> None:
        self.known_rows = known_rows
        self.calls: list[tuple[str, Any]] = []

    def execute(self, query: str, params: Any = None) -> FakeResult:
        self.calls.append((query, params))
        if "INSERT INTO meta.pipeline_run" in query:
            return FakeResult([(7,)])
        if "FROM meta.source_version" in query:
            return FakeResult(self.known_rows)
        return FakeResult([])


def test_check_source_records_a_successful_run_and_reports_the_outcome(
    make_spec: SpecFactory,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"new"', "Content-Length": "10"})

    conn = FakeConnection([(3, '"old"', None, 10)])
    client = remote.make_client(httpx.MockTransport(handler))
    result = checks.check_source(conn, client, make_spec())  # type: ignore[arg-type]
    assert result.outcome == "changed"
    assert result.known_version_id == 3
    assert result.to_json()["remote"]["etag"] == '"new"'
    statements = [query for query, _ in conn.calls]
    assert any("'succeeded'" in s for s in statements)


def test_check_source_records_a_failed_run_when_the_publisher_errors(
    make_spec: SpecFactory,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    conn = FakeConnection([])
    client = remote.make_client(httpx.MockTransport(handler))
    with pytest.raises(RemoteStatusError):
        checks.check_source(conn, client, make_spec())  # type: ignore[arg-type]
    failed = [params for query, params in conn.calls if "'failed'" in query]
    assert failed
    assert "503" in failed[0][0]
