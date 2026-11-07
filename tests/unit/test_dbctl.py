import subprocess
from pathlib import Path

import pytest

from cabler import dbctl


class FakeRun:
    """Stands in for subprocess.run and records the commands it was asked to run."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr(dbctl.subprocess, "run", fake)
    return fake


@pytest.fixture
def fake_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "env").mkdir()
    for name in ("dev", "staging"):
        (tmp_path / "env" / f"{name}.env").write_text(f"CABLER_ENV={name}\n", encoding="utf-8")
    monkeypatch.setattr(dbctl, "ROOT", tmp_path)
    monkeypatch.setattr(dbctl, "COMPOSE_FILE", tmp_path / "docker" / "compose.yaml")
    return tmp_path


def test_up_builds_waits_and_uses_the_environment_file(fake_root: Path, fake_run: FakeRun) -> None:
    assert dbctl.main(["up", "dev"]) == 0
    (cmd,) = fake_run.calls
    assert cmd[:2] == ["docker", "compose"]
    assert str(fake_root / "env" / "dev.env") in cmd
    assert cmd[-4:] == ["up", "-d", "--build", "--wait"]


@pytest.mark.parametrize(
    ("action", "expected_tail"),
    [
        ("down", ["down"]),
        ("status", ["ps"]),
        ("logs", ["logs", "--tail", "100", "db"]),
    ],
)
def test_simple_actions_map_to_compose_commands(
    fake_root: Path, fake_run: FakeRun, action: str, expected_tail: list[str]
) -> None:
    assert dbctl.main([action, "dev"]) == 0
    assert fake_run.calls[0][-len(expected_tail) :] == expected_tail


def test_missing_env_file_gives_a_helpful_error(
    fake_root: Path, fake_run: FakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    assert dbctl.main(["up", "production"]) == 2
    assert fake_run.calls == []
    assert "python -m cabler.envgen production" in capsys.readouterr().err


def test_destroy_is_free_for_dev_but_guarded_elsewhere(fake_root: Path, fake_run: FakeRun) -> None:
    assert dbctl.main(["destroy", "dev"]) == 0
    assert fake_run.calls[-1][-2:] == ["down", "--volumes"]

    fake_run.calls.clear()
    assert dbctl.main(["destroy", "staging"]) == 3
    assert fake_run.calls == []

    assert dbctl.main(["destroy", "staging", "--yes-delete-data"]) == 0
    assert fake_run.calls[-1][-2:] == ["down", "--volumes"]
