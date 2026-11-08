from typing import Any, Self

import pytest

from cabler import pipeline, sources


def test_list_shows_every_source_with_its_licence(capsys: pytest.CaptureFixture[str]) -> None:
    assert pipeline.main(["list"]) == 0
    out = capsys.readouterr().out
    for spec in sources.load_all().values():
        assert spec.source_id in out
        assert spec.licence_id in out


class FakeResult:
    def fetchone(self) -> tuple[bool]:
        return (True,)


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: str, params: Any = None) -> FakeResult:
        self.calls.append(params)
        return FakeResult()


def test_register_writes_every_source_as_the_app_role(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = FakeConnection()
    roles: list[str] = []

    def fake_connect(role: str, environ: Any = None) -> FakeConnection:
        roles.append(role)
        return conn

    monkeypatch.setattr(pipeline.db, "connect", fake_connect)
    assert pipeline.main(["register", "dev"]) == 0
    assert roles == ["app"]
    assert len(conn.calls) == len(sources.load_all())
    assert "created" in capsys.readouterr().out


def test_an_unknown_environment_is_rejected() -> None:
    with pytest.raises(SystemExit):
        pipeline.main(["register", "nowhere"])
