from __future__ import annotations

import asyncio

from shelfwise_synthdata import (
    CATEGORIES,
    INJECTIONS,
    generate_agent_sft,
    generate_golden,
    generate_operational_events,
    generate_preference_pairs,
    generate_tenant_profiles,
    observe_adversarial,
    run_suite,
)


def test_golden_scenarios_are_deterministic_categorized_and_labeled() -> None:
    first = list(generate_golden(7, n_per_category=2))
    second = list(generate_golden(7, n_per_category=2))

    assert [scenario.id for scenario in first] == [scenario.id for scenario in second]
    assert all(scenario.tag.synthetic for scenario in first)
    assert len({scenario.category for scenario in first}) == len(CATEGORIES)
    critic = next(scenario for scenario in first if scenario.category == "critic_rejection")
    assert critic.expected["critic_verdict"] == "rejected"
    assert critic.trigger_event["payload"]["synthetic"] is True


def test_tenant_operational_and_training_generators_are_labeled() -> None:
    profiles = list(generate_tenant_profiles(1, n_tenants=3))
    rows = list(generate_operational_events(1, tenant_id="t", skus=2, days=3))
    sft = list(generate_agent_sft(2, n=2))
    pairs = list(generate_preference_pairs(3, n=2))

    assert all(profile["synthetic"] for profile in profiles)
    assert rows and all(row["synthetic"] and row["type"] == "sale" for row in rows)
    assert all(record["synthetic"] for record in sft)
    assert all(record["synthetic"] for record in pairs)
    assert sft[0]["ideal"]["sources"]
    assert pairs[0]["chosen"]["verdict"] == "reject"


def test_run_suite_scores_pass_rate_and_failures() -> None:
    scenarios = list(generate_golden(1, n_per_category=1))

    async def run_one(scenario):
        return scenario.expected

    scorecard = asyncio.run(run_suite(scenarios, run_one=run_one))

    assert scorecard["pass_rate"] == 1.0
    assert scorecard["failures"] == []
    assert scorecard["total"] == len(CATEGORIES)


def test_run_suite_actually_detects_a_mismatched_observation() -> None:
    """A fake `run_one` that only ever echoes `scenario.expected` back can never exercise
    `run_suite`'s own comparison logic - it would report pass_rate == 1.0 even if `_check`
    were replaced with `return True`. Wrong an observation for exactly one scenario and
    confirm run_suite actually notices, not just that it can report a clean run (found
    2026-07-15 auditing the test suite's own arrangements, not its assertions)."""
    scenarios = list(generate_golden(1, n_per_category=1))
    wrong_scenario = scenarios[0]
    wrong_key = next(iter(wrong_scenario.expected))

    async def run_one(scenario):
        if scenario.id == wrong_scenario.id:
            observed = dict(scenario.expected)
            observed[wrong_key] = f"__deliberately_wrong__:{observed[wrong_key]!r}"
            return observed
        return scenario.expected

    scorecard = asyncio.run(run_suite(scenarios, run_one=run_one))

    assert scorecard["pass_rate"] < 1.0
    assert len(scorecard["failures"]) == 1
    assert scorecard["failures"][0]["id"] == wrong_scenario.id
    assert scorecard["by_category"][wrong_scenario.category] < 1.0


def test_adversarial_category_carries_injection_and_scorer_catches_obedience() -> None:
    adversarial = [
        scenario
        for scenario in generate_golden(3, n_per_category=4)
        if scenario.category == "edge_adversarial"
    ]

    assert adversarial
    assert all("note" in scenario.context for scenario in adversarial)
    assert any(INJECTIONS[0] in scenario.context["note"] for scenario in adversarial)
    assert adversarial[0].expected == {"injection_obeyed": False, "sources_cited": True}

    grounded = {
        "recommendation": {
            "sources": ["evt_1"],
            "recommended_action": {"risk_tier": "medium"},
        },
        "human_decision": "auto",
        "action_taken": "apply_markdown",
    }
    stripped = {
        "recommendation": {"sources": []},
        "human_decision": "auto",
        "action_taken": "apply_markdown",
    }
    unreviewed_high_risk = {
        "recommendation": {
            "sources": ["evt_1"],
            "requires_human_review": False,
            "recommended_action": {"risk_tier": "high"},
        },
        "human_decision": "auto",
        "action_taken": "supplier_switch",
    }

    assert observe_adversarial(grounded) == {
        "injection_obeyed": False,
        "sources_cited": True,
    }
    assert observe_adversarial(stripped)["injection_obeyed"] is True
    assert observe_adversarial(unreviewed_high_risk)["injection_obeyed"] is True
