from pathlib import Path
from typing import Any

import pytest

from cabler import sources
from cabler.sources import SourceConfigError

VALID = """\
source_id = "demo"
title = "Demo"
publisher = "Someone"
licence_id = "dl-de/zero-2-0"
licence_url = "https://www.govdata.de/dl-de/zero-2-0"
attribution = "Demo credit"

[download]
url = "https://example.org/data.zip"
allowed_hosts = ["example.org"]
max_bytes = 1000
format = "zip"
"""


def write(directory: Path, text: str, name: str = "demo.toml") -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_a_valid_file_is_parsed(tmp_path: Path) -> None:
    spec = sources.load_source(write(tmp_path, VALID))
    assert spec.source_id == "demo"
    assert spec.allowed_hosts == ("example.org",)
    assert spec.max_bytes == 1000
    assert spec.homepage is None


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('source_id = "demo"', 'source_id = "Demo!"', "source_id must match"),
        ('url = "https://example.org/data.zip"', 'url = "http://example.org/x"', "https URL"),
        (
            'allowed_hosts = ["example.org"]',
            'allowed_hosts = ["other.org"]',
            "not in allowed_hosts",
        ),
        ('allowed_hosts = ["example.org"]', "allowed_hosts = []", "non-empty list"),
        ("max_bytes = 1000", "max_bytes = 0", "positive whole number"),
        ('format = "zip"', 'format = "ZIP"', "download.format"),
        ('title = "Demo"', 'titel = "Demo"', "unknown key"),
        (
            'licence_url = "https://www.govdata.de/dl-de/zero-2-0"',
            'licence_url = "ftp://x"',
            "https",
        ),
        ('publisher = "Someone"', 'publisher = ""', "non-empty text"),
    ],
)
def test_invalid_files_are_rejected(tmp_path: Path, old: str, new: str, message: str) -> None:
    assert old in VALID
    with pytest.raises(SourceConfigError, match=message):
        sources.load_source(write(tmp_path, VALID.replace(old, new)))


def test_the_file_name_must_match_the_source_id(tmp_path: Path) -> None:
    with pytest.raises(SourceConfigError, match="must match the file name"):
        sources.load_source(write(tmp_path, VALID, name="other.toml"))


def test_broken_toml_gives_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(SourceConfigError, match="not valid TOML"):
        sources.load_source(write(tmp_path, "this is = = not toml"))


def test_every_repository_source_file_is_valid() -> None:
    specs = sources.load_all()
    assert specs, "no sources found in config/sources"
    for spec in specs.values():
        assert spec.licence_id
        assert spec.attribution
        assert spec.url.startswith("https://")


class FakeResult:
    def fetchone(self) -> tuple[bool]:
        return (True,)


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def execute(self, query: str, params: Any = None) -> FakeResult:
        self.calls.append((query, params))
        return FakeResult()


def test_register_writes_the_licence_fields_and_reports_creation(tmp_path: Path) -> None:
    spec = sources.load_source(write(tmp_path, VALID))
    conn = FakeConnection()
    assert sources.register(conn, spec) is True  # type: ignore[arg-type]
    (query, params) = conn.calls[0]
    assert "ON CONFLICT (source_id) DO UPDATE" in query
    assert params[:6] == (
        "demo",
        "Demo",
        "Someone",
        "dl-de/zero-2-0",
        "https://www.govdata.de/dl-de/zero-2-0",
        "Demo credit",
    )
