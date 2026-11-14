"""The command-line side of the core: what is printed, and when the exit code is not zero.

The core functions are replaced by canned results, so these tests need no database.
"""

from typing import Any

import pytest

from cabler import core, pipeline
from cabler.core import CoreError
from cabler.evaluate import Cell, Evaluation
from cabler.places import Places, SubstationPlaces, WindAgreement


@pytest.fixture(autouse=True)
def wired(monkeypatch: pytest.MonkeyPatch, fake_connection: Any) -> None:
    conn = fake_connection()
    monkeypatch.setattr(pipeline.db, "connect", lambda role, environ=None: conn)


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = pipeline.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def build_result() -> core.BuildResult:
    counts = {
        "substations": 20_092,
        "strict": 983,
        "likely": 1_209,
        "inclusive": 19_500,
        "audit_sample": 61,
        "tier_A": 400,
        "tier_B": 300,
        "tier_C": 200,
        "tier_D": 15_000,
    }
    return core.BuildResult(7, "ab" * 32, counts, {"osm_nrw": 397, "wind_units": 12})


def test_core_prints_the_scenarios_the_tiers_and_the_hash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pipeline.core, "build_substations", lambda conn: build_result())
    code, out, _ = run(["core", "dev"], capsys)
    assert code == 0
    assert "core build 7  hash abababababababab" in out
    assert "osm_nrw 397, wind_units 12" in out
    assert "substations 20,092: strict 983, likely 1,209, inclusive 19,500" in out
    assert "review tiers: A 400, B 300, C 200, D 15,000; audit sample 61" in out


def test_a_refused_build_exits_with_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(conn: Any) -> Any:
        raise CoreError("osm_nrw: no validated snapshot; run validate first")

    monkeypatch.setattr(pipeline.core, "build_substations", refuse)
    code, _, err = run(["core", "dev"], capsys)
    assert code == 1
    assert "ERROR core: osm_nrw: no validated snapshot" in err


def test_the_queue_lists_the_cases_with_their_impact(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[str | None, int]] = []
    rows = [
        (
            "osm_nrw",
            "points",
            "n1",
            "Umspannwerk Nord",
            "A",
            12,
            14,
            "hv_candidate",
            True,
            7.1,
            51.2,
        ),
        ("osm_nrw", "points", "n2", "", "C", 0, 2, "", False, 7.2, 51.3),
    ]

    def queue(conn: Any, tier: str | None, limit: int) -> list[Any]:
        seen.append((tier, limit))
        return rows

    monkeypatch.setattr(pipeline.core, "queue_rows", queue)
    code, out, _ = run(["queue", "dev", "--tier", "A", "--limit", "5"], capsys)
    assert code == 0
    assert seen == [("A", 5)]
    assert "A osm_nrw" in out
    assert "impact 12" in out
    assert "audit" in out
    assert "C osm_nrw" in out


def test_text_from_a_file_is_shown_with_its_control_characters_escaped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    name = "Werk M\u00fcller \x1b[2J\x07"
    rows = [("osm_nrw", "points", "n1", name, "A", 3, 4, "conflict", False, 7.1, 51.2)]
    monkeypatch.setattr(pipeline.core, "queue_rows", lambda conn, tier, limit: rows)
    code, out, _ = run(["queue", "dev"], capsys)
    assert code == 0
    assert "\x1b" not in out
    assert "\x07" not in out
    assert "M\u00fcller" in out
    assert "\\x1b[2J" in out


def test_an_empty_queue_says_what_that_means(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pipeline.core, "queue_rows", lambda conn, tier, limit: [])
    code, out, _ = run(["queue", "dev"], capsys)
    assert code == 0
    assert "nothing to review" in out


def test_visible_escapes_what_a_terminal_would_obey() -> None:
    assert pipeline._visible("plain \u00e4\u00f6\u00fc") == "plain \u00e4\u00f6\u00fc"
    assert pipeline._visible("a\x1b]0;x\x07b") == "a\\x1b]0;x\\x07b"
    assert pipeline._visible("a\u202eb") == "a\\u202eb"
    assert pipeline._visible(None) == "None"


def test_a_decision_is_recorded_with_its_reason_and_reviewer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def record(conn: Any, source: str, layer: str, key: str, decision: str, **kwargs: Any) -> int:
        seen.update(source=source, layer=layer, key=key, decision=decision, **kwargs)
        return 12

    monkeypatch.setattr(pipeline.core, "record_decision", record)
    argv = ["decide", "dev", "osm_nrw", "points", "n1", "high_voltage"]
    code, out, _ = run(
        [*argv, "--reason", "named 110 kV on the operator's map", "--reviewer", "P"], capsys
    )
    assert code == 0
    assert "decision 12: osm_nrw/points/n1 -> high_voltage" in out
    assert seen["reason"] == "named 110 kV on the operator's map"
    assert seen["reviewer"] == "P"


def test_a_refused_decision_exits_with_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> int:
        raise CoreError("osm_nrw/points/n1 is not a current substation")

    monkeypatch.setattr(pipeline.core, "record_decision", refuse)
    argv = ["decide", "dev", "osm_nrw", "points", "n1", "not_a_substation"]
    code, _, err = run([*argv, "--reason", "r", "--reviewer", "p"], capsys)
    assert code == 1
    assert "ERROR decide: osm_nrw/points/n1 is not a current substation" in err


def test_evaluate_prints_the_measured_shares(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = Evaluation({"osm_nrw": 397}, [Cell("osm_nrw", "hv_candidate", 10, 5)], [], 3)
    monkeypatch.setattr(pipeline.evaluate, "evaluate_rules", lambda conn: result)
    code, out, _ = run(["evaluate", "dev"], capsys)
    assert code == 0
    assert "hv_candidate" in out
    assert "50.0%" in out


def test_a_refused_evaluation_exits_with_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(conn: Any) -> Any:
        raise CoreError("osm_nrw: no validated snapshot; run validate first")

    monkeypatch.setattr(pipeline.evaluate, "evaluate_rules", refuse)
    code, _, err = run(["evaluate", "dev"], capsys)
    assert code == 1
    assert "ERROR evaluate: osm_nrw: no validated snapshot" in err


def test_places_prints_the_measurements(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = Places(
        11_094,
        [SubstationPlaces("osm_nrw", 100, 99, 2)],
        [WindAgreement("05", 200, 198, 160, 0, (5, 10, 12, 10, 3))],
    )
    monkeypatch.setattr(pipeline.places, "check_places", lambda conn: result)
    code, out, _ = run(["places", "dev"], capsys)
    assert code == 0
    assert "11,094 municipalities" in out
    assert "80.0%" in out


def test_places_without_boundaries_exit_with_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(conn: Any) -> Any:
        raise CoreError("bkg_vg250: no boundaries are applied; run: fetch, load, diff, apply")

    monkeypatch.setattr(pipeline.places, "check_places", refuse)
    code, _, err = run(["places", "dev"], capsys)
    assert code == 1
    assert "ERROR places: bkg_vg250: no boundaries are applied" in err
