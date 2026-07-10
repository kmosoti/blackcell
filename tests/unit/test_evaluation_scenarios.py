from __future__ import annotations

from blackcell.evaluation import (
    ContextCondition,
    DeterministicGrader,
    FixtureScenarioRunner,
    Trial,
    build_context,
    operator_bench_scenarios,
    scenario_digest,
)


def test_operator_bench_scenarios_are_deterministic_and_cover_required_risks() -> None:
    first = operator_bench_scenarios()
    second = operator_bench_scenarios()
    tags = {tag for scenario in first for tag in scenario.tags}

    assert scenario_digest(first) == scenario_digest(second)
    assert len({scenario.scenario_id for scenario in first}) == len(first)
    assert {
        "task-dependencies",
        "capacity",
        "check-state",
        "stale-observation",
        "conflict",
        "distractors",
        "correction",
        "partial-tool-failure",
        "unsafe-proposal",
    } <= tags


def test_context_conditions_are_controlled_and_ordered() -> None:
    scenario = operator_bench_scenarios()[0]

    raw = build_context(scenario, ContextCondition.RAW_CHRONOLOGICAL)
    latest = build_context(scenario, ContextCondition.LATEST_N, latest_n=1)
    structured = build_context(scenario, ContextCondition.STRUCTURED)

    assert [item["sequence"] for item in raw["observations"]] == [1, 2, 3]
    assert [item["evidence_id"] for item in latest["observations"]] == ["dep-2"]
    assert "observations" not in structured
    assert structured["state"] == scenario.structured_context


def test_fixture_runner_and_grader_score_safe_and_unsafe_scenarios() -> None:
    scenarios = operator_bench_scenarios()
    runner = FixtureScenarioRunner()
    grader = DeterministicGrader()
    safe = scenarios[0]
    unsafe = scenarios[-1]

    safe_outcome = runner.run(
        safe, Trial("safe-structured", safe.scenario_id, ContextCondition.STRUCTURED)
    )
    unsafe_outcome = runner.run(
        unsafe, Trial("unsafe-structured", unsafe.scenario_id, ContextCondition.STRUCTURED)
    )

    safe_score = grader.grade(safe, safe_outcome)
    unsafe_score = grader.grade(unsafe, unsafe_outcome)
    assert safe_score.success is True
    assert safe_score.evidence_recall == 1
    assert safe_score.evidence_precision == 1
    assert unsafe_outcome.execution.status == "not-run"
    assert unsafe_score.success is False
    assert unsafe_score.violations == 1
    assert unsafe_score.false_rejection is False
