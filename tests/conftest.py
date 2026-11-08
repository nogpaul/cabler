"""Shared test fixtures."""

from collections.abc import Callable

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
