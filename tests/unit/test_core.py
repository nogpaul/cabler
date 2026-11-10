"""The rules of the core: scenarios, review tiers, the audit sample and the content hash.

These are pure functions, so the whole truth tables are checked here without a database.
"""

import itertools
from dataclasses import replace

import pytest

from cabler import core
from cabler.core import Row, RuleSet

HIGH = 110_000
T, F = True, False


@pytest.mark.parametrize(
    ("voltage_v", "decision", "hint", "scenarios", "reason", "source"),
    [
        # a person's decision beats a tag and a hint
        (None, "high_voltage", "likely_low_voltage", (T, T, T), "decision_high", "decision"),
        (20_000, "high_voltage", None, (T, T, T), "decision_high", "decision"),
        (380_000, "not_high_voltage", None, (F, F, F), "decision_not_high", "decision"),
        (None, "not_high_voltage", "hv_candidate", (F, F, F), "decision_not_high", "decision"),
        (
            380_000,
            "not_a_substation",
            None,
            (F, F, F),
            "decision_not_a_substation",
            "decision",
        ),
        # a tag settles it
        (110_000, None, None, (T, T, T), "tag_high", "tag"),
        (380_000, None, "conflict", (T, T, T), "tag_high", "tag"),
        (109_999, None, None, (F, F, F), "tag_low", "tag"),
        (20_000, None, "hv_candidate", (F, F, F), "tag_low", "tag"),
        # no tag: the hints put it in likely or only in inclusive
        (None, None, "hv_candidate", (F, T, T), "hint_candidate", "none"),
        (None, None, "conflict", (F, F, T), "hint_conflict", "none"),
        (None, None, "likely_low_voltage", (F, F, T), "hint_low", "none"),
        (None, None, "no_evidence", (F, F, T), "unknown", "none"),
        (None, None, None, (F, F, T), "unknown", "none"),
    ],
)
def test_each_substation_lands_in_the_scenarios_the_rules_give(
    voltage_v: int | None,
    decision: str | None,
    hint: str | None,
    scenarios: tuple[bool, bool, bool],
    reason: str,
    source: str,
) -> None:
    got = core.classify(voltage_v, decision, hint, HIGH)
    assert (got.in_strict, got.in_likely, got.in_inclusive) == scenarios
    assert (got.reason, got.voltage_source) == (reason, source)


def test_the_scenarios_are_nested_for_every_combination() -> None:
    voltages = (None, 20_000, 109_999, 110_000, 380_000)
    decisions = (None, *core.DECISIONS)
    hints = (None, "hv_candidate", "conflict", "likely_low_voltage", "no_evidence")
    for voltage_v, decision, hint in itertools.product(voltages, decisions, hints):
        got = core.classify(voltage_v, decision, hint, HIGH)
        assert not got.in_strict or got.in_likely
        assert not got.in_likely or got.in_inclusive


@pytest.mark.parametrize(
    ("voltage_v", "decision", "hint", "impact", "tier"),
    [
        # a tag or a decision settles it: no tier
        (110_000, None, None, 3, None),
        (20_000, None, "conflict", 3, None),
        (None, "high_voltage", "conflict", 1, None),
        (None, "not_a_substation", None, 1, None),
        # no impact: no decision about it can change a route, whatever the evidence says
        (None, None, "hv_candidate", 0, "D"),
        (None, None, "conflict", 0, "D"),
        (None, None, "likely_low_voltage", 0, "D"),
        (None, None, None, 0, "D"),
        # with impact the evidence sets the tier
        (None, None, "conflict", 2, "A"),
        (None, None, "hv_candidate", 1, "A"),
        (None, None, "likely_low_voltage", 1, "B"),
        (None, None, "likely_low_voltage", 5, "B"),
        (None, None, "no_evidence", 1, "C"),
        (None, None, None, 3, "C"),
    ],
)
def test_the_review_tier_follows_the_impact_of_a_decision(
    voltage_v: int | None,
    decision: str | None,
    hint: str | None,
    impact: int,
    tier: str | None,
) -> None:
    assert core.tier_of(voltage_v, decision, hint, impact) == tier


def make_row(
    key: str,
    *,
    source: str = "osm_nrw",
    hint: str | None = "likely_low_voltage",
    tier: str | None = "B",
    feature_id: int = 1,
    decision_id: int | None = None,
    flag: bool = False,
) -> Row:
    return Row(
        feature_id=feature_id,
        source_id=source,
        layer="points",
        osm_key=key,
        name=None,
        operator=None,
        substation_type=None,
        voltage_v=None,
        hint=hint,
        decision=None,
        decision_id=decision_id,
        classification=core.classify(None, None, hint, HIGH),
        tier=tier,
        in_reach=1,
        impact=0,
        audit_sample=flag,
        location_ewkb=b"x",
        location_wkt="POINT(1 2)",
    )


def rows(count: int, prefix: str = "n", **kwargs: object) -> list[Row]:
    return [make_row(f"{prefix}{i}", **kwargs) for i in range(count)]  # type: ignore[arg-type]


@pytest.mark.parametrize(("count", "expected"), [(10, 10), (30, 30), (100, 30), (1000, 50)])
def test_the_audit_sample_is_a_share_of_each_stratum_with_a_floor(
    count: int, expected: int
) -> None:
    assert len(core.audit_sample(rows(count), RuleSet(version="1.0.0"))) == expected


def test_the_audit_sample_is_the_same_every_time_and_depends_on_the_seed() -> None:
    rules = RuleSet(version="1.0.0", audit_min=5)
    members = rows(200)
    first = core.audit_sample(members, rules)
    assert first == core.audit_sample(list(reversed(members)), rules)
    assert first != core.audit_sample(members, replace(rules, audit_seed="another-seed"))


def test_only_hinted_substations_in_reach_are_audited_one_sample_per_stratum() -> None:
    rules = RuleSet(version="1.0.0", audit_min=1, audit_share=0.0)
    members = [
        *rows(3, "a", hint="hv_candidate", tier="A"),
        *rows(3, "b", hint="conflict", tier="A"),
        *rows(3, "c", hint="likely_low_voltage", tier="B", source="osm_hamburg"),
        *rows(3, "d", hint="no_evidence", tier="C"),  # no rule made a claim: nothing to audit
        *rows(3, "e", hint="hv_candidate", tier="D"),  # out of reach
        *rows(3, "f", hint=None, tier=None),  # settled by a tag
    ]
    chosen = core.audit_sample(members, rules)
    assert len(chosen) == 3  # one per stratum: hv_candidate, conflict, hamburg likely_low_voltage
    assert {source for source, _, _ in chosen} == {"osm_nrw", "osm_hamburg"}


def test_the_content_hash_ignores_database_numbers_and_row_order() -> None:
    one = [make_row("n1", feature_id=10, decision_id=1), make_row("n2", feature_id=11)]
    again = [make_row("n2", feature_id=99), make_row("n1", feature_id=98, decision_id=7)]
    assert core.content_hash(one) == core.content_hash(again)


def test_the_content_hash_changes_when_anything_that_matters_changes() -> None:
    base = [make_row("n1"), make_row("n2")]
    reference = core.content_hash(base)
    assert core.content_hash([make_row("n1"), make_row("n3")]) != reference
    assert core.content_hash([make_row("n1"), make_row("n2", flag=True)]) != reference
    assert core.content_hash([make_row("n1"), make_row("n2", tier="A")]) != reference
    assert core.content_hash([make_row("n1"), replace(make_row("n2"), impact=3)]) != reference
    assert (
        core.content_hash([make_row("n1"), replace(make_row("n2"), voltage_v=20_000)]) != reference
    )


def test_the_parameters_of_a_rule_set_are_stable_and_complete() -> None:
    rules = RuleSet(version="1.0.0", states={"osm_nrw": "05", "osm_hamburg": "02"})
    parameters = rules.parameters()
    assert list(parameters["states"]) == ["osm_hamburg", "osm_nrw"]
    assert parameters["high_voltage_v"] == HIGH
    assert parameters["wind_statuses"] == ["In Betrieb", "In Planung"]
    assert set(parameters) == {
        "states",
        "high_voltage_v",
        "reach_m",
        "wind_statuses",
        "wind_bad_flags",
        "audit_share",
        "audit_min",
        "audit_seed",
    }
