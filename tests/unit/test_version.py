import importlib
from importlib.metadata import PackageNotFoundError

import pytest

import cabler


def test_version_is_exposed() -> None:
    assert cabler.__version__


def test_version_falls_back_when_package_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_not_found(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr("importlib.metadata.version", raise_not_found)
    try:
        reloaded = importlib.reload(cabler)
        assert reloaded.__version__ == "0.0.0+unknown"
    finally:
        monkeypatch.undo()
        importlib.reload(cabler)
