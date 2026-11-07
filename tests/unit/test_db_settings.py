from pathlib import Path

import pytest

from cabler import db


def test_load_env_file_ignores_comments_and_blanks(tmp_path: Path) -> None:
    f = tmp_path / "x.env"
    f.write_text("# comment\n\nA=1\nB = two \nnot a pair\n", encoding="utf-8")
    assert db.load_env_file(f) == {"A": "1", "B": "two"}


def test_resolve_settings_real_environment_overrides_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "unit.env").write_text("DB_PORT=1111\nDB_HOST=file\n", encoding="utf-8")
    monkeypatch.setattr(db, "REPO_ROOT", tmp_path)
    settings = db.resolve_settings({"CABLER_ENV": "unit", "DB_PORT": "2222"})
    assert settings["DB_PORT"] == "2222"
    assert settings["DB_HOST"] == "file"


def test_resolve_settings_unknown_env_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "REPO_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        db.resolve_settings({"CABLER_ENV": "nope"})


def test_connect_reports_missing_settings() -> None:
    with pytest.raises(KeyError, match="CABLER_APP_PASSWORD"):
        db.connect("app", environ={"DB_HOST": "h", "DB_PORT": "1", "DB_NAME": "d"})
