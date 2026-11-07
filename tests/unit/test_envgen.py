import stat
from pathlib import Path

import pytest

from cabler import envgen


def test_render_replaces_every_placeholder_with_distinct_secrets() -> None:
    out = envgen.render("A=__GENERATE__\nB=__GENERATE__\nC=keep\n")
    lines = out.strip().splitlines()
    assert "__GENERATE__" not in out
    assert lines[2] == "C=keep"
    assert lines[0].split("=")[1] != lines[1].split("=")[1]
    assert len(lines[0].split("=")[1]) >= 24


def test_main_writes_a_private_file_and_refuses_to_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "dev.env.example"
    template.write_text("CABLER_ENV=dev\nSECRET=__GENERATE__\n", encoding="utf-8")
    monkeypatch.setattr(envgen, "ENV_DIR", tmp_path)

    assert envgen.main(["dev"]) == 0
    target = tmp_path / "dev.env"
    assert "__GENERATE__" not in target.read_text(encoding="utf-8")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600

    first = target.read_text(encoding="utf-8")
    assert envgen.main(["dev"]) == 1
    assert target.read_text(encoding="utf-8") == first
    assert envgen.main(["dev", "--force"]) == 0
    assert target.read_text(encoding="utf-8") != first


def test_every_template_names_its_environment_and_has_a_unique_port() -> None:
    ports: dict[str, str] = {}
    for name in envgen.NAMES:
        text = (envgen.ENV_DIR / f"{name}.env.example").read_text(encoding="utf-8")
        values = dict(
            line.split("=", 1)
            for line in text.splitlines()
            if "=" in line and not line.startswith("#")
        )
        assert values["CABLER_ENV"] == name
        ports[name] = values["DB_PORT"]
    assert len(set(ports.values())) == len(ports)
