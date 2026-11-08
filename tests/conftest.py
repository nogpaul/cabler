"""Shared test fixtures."""

from collections.abc import Callable
from typing import Any, Self

import pytest

from cabler.sources import SourceSpec

SpecFactory = Callable[..., SourceSpec]


@pytest.fixture
def make_spec() -> SpecFactory:
    """Build a valid :class:`SourceSpec` for a made-up publisher, with optional overrides."""

    def build(max_bytes: int = 1000, url: str = "https://data.example.org/file.bin") -> SourceSpec:
        return SourceSpec(
            source_id="demo",
            title="Demo",
            publisher="Someone",
            licence_id="dl-de/zero-2-0",
            licence_url="https://www.govdata.de/dl-de/zero-2-0",
            attribution="Demo credit",
            homepage=None,
            url=url,
            allowed_hosts=("data.example.org", "cdn.example.org"),
            max_bytes=max_bytes,
            file_format="bin",
        )

    return build


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class FakeConnection:
    """A stand-in database connection that answers the pipeline's queries from memory."""

    def __init__(
        self,
        registered: list[str] | None = None,
        known_rows: list[tuple[Any, ...]] | None = None,
        duplicate_rows: list[tuple[Any, ...]] | None = None,
    ) -> None:
        self.registered = registered
        self.known_rows = known_rows or []
        self.duplicate_rows = duplicate_rows or []
        self.autocommit = False
        self.calls: list[tuple[str, Any]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def statements(self) -> list[str]:
        return [query for query, _ in self.calls]

    def execute(self, query: str, params: Any = None) -> FakeResult:
        self.calls.append((query, params))
        if "FROM meta.source WHERE source_id = ANY" in query:
            wanted = params[0]
            return FakeResult(
                [(i,) for i in wanted if self.registered is None or i in self.registered]
            )
        if "INSERT INTO meta.pipeline_run" in query:
            return FakeResult([(7,)])
        if "ORDER BY created_at DESC" in query:
            return FakeResult(self.known_rows)
        if "AND sha256 = %s" in query:
            return FakeResult(self.duplicate_rows)
        if "INSERT INTO meta.source_version" in query:
            return FakeResult([(42,)])
        return FakeResult([])


@pytest.fixture
def fake_connection() -> type[FakeConnection]:
    """The :class:`FakeConnection` class; call it to get a fresh fake database."""
    return FakeConnection
