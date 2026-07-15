from __future__ import annotations

from dataclasses import replace

import pytest

from blackcell.evaluation import (
    ContextCondition,
    DeterministicGrader,
    FixtureScenarioRunner,
    Trial,
    aggregate_scores,
    operator_bench_scenarios,
    paired_bootstrap_delta,
    paired_delta,
    wilson_interval,
)


def _score(condition: ContextCondition, *, success: bool, scenario_id: str = "s"):
    scenario = operator_bench_scenarios()[0]
    outcome = FixtureScenarioRunner().run(
        scenario, Trial(f"{scenario_id}-{condition}", scenario.scenario_id, condition)
    )
    score = DeterministicGrader().grade(scenario, outcome)
    return replace(score, scenario_id=scenario_id, success=success)


def test_wilson_interval_is_bounded_and_contains_observed_rate() -> None:
    lower, upper = wilson_interval(8, 10)

    assert 0 <= lower < 0.8 < upper <= 1


def test_aggregation_reports_wilson_intervals_and_continuous_metrics() -> None:
    scores = [
        _score(ContextCondition.STRUCTURED, success=True, scenario_id="one"),
        _score(ContextCondition.STRUCTURED, success=False, scenario_id="two"),
    ]

    aggregate = aggregate_scores(scores)[0]

    success = aggregate.metric("success")
    assert success.mean == 0.5
    assert success.lower is not None and success.lower < success.mean
    assert success.upper is not None and success.upper > success.mean
    assert aggregate.metric("context_chars").count == 2


def test_paired_delta_matches_scenario_and_replicate() -> None:
    scores = [
        _score(ContextCondition.RAW_CHRONOLOGICAL, success=False, scenario_id="one"),
        _score(ContextCondition.STRUCTURED, success=True, scenario_id="one"),
        _score(ContextCondition.RAW_CHRONOLOGICAL, success=True, scenario_id="two"),
        _score(ContextCondition.STRUCTURED, success=True, scenario_id="two"),
    ]

    delta = paired_delta(
        scores,
        left=ContextCondition.RAW_CHRONOLOGICAL,
        right=ContextCondition.STRUCTURED,
        metric="success",
    )

    assert delta.pair_count == 2
    assert delta.mean_delta == 0.5
    assert (delta.wins, delta.ties, delta.losses) == (1, 1, 0)


def test_paired_delta_requires_pairs() -> None:
    with pytest.raises(ValueError, match="no paired trials"):
        paired_delta(
            [_score(ContextCondition.STRUCTURED, success=True)],
            left=ContextCondition.RAW_CHRONOLOGICAL,
            right=ContextCondition.STRUCTURED,
            metric="success",
        )


def test_paired_bootstrap_delta_is_seeded_bounded_and_paired() -> None:
    scores = [
        _score(ContextCondition.RAW_CHRONOLOGICAL, success=False, scenario_id="one"),
        _score(ContextCondition.STRUCTURED, success=True, scenario_id="one"),
        _score(ContextCondition.RAW_CHRONOLOGICAL, success=True, scenario_id="two"),
        _score(ContextCondition.STRUCTURED, success=True, scenario_id="two"),
    ]

    first = paired_bootstrap_delta(
        scores,
        left=ContextCondition.RAW_CHRONOLOGICAL,
        right=ContextCondition.STRUCTURED,
        metric="success",
        samples=200,
        seed=23,
    )
    second = paired_bootstrap_delta(
        scores,
        left=ContextCondition.RAW_CHRONOLOGICAL,
        right=ContextCondition.STRUCTURED,
        metric="success",
        samples=200,
        seed=23,
    )

    assert first == second
    assert first.pair_count == 2
    assert first.lower <= first.mean_delta <= first.upper
    assert first.samples == 200
    assert first.confidence == 0.95


@pytest.mark.parametrize(
    ("samples", "confidence", "message"),
    ((0, 0.95, "samples"), (10, 0, "confidence"), (10, 1, "confidence")),
)
def test_paired_bootstrap_delta_rejects_invalid_design(
    samples: int,
    confidence: float,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        paired_bootstrap_delta(
            [
                _score(ContextCondition.RAW_CHRONOLOGICAL, success=True),
                _score(ContextCondition.STRUCTURED, success=True),
            ],
            left=ContextCondition.RAW_CHRONOLOGICAL,
            right=ContextCondition.STRUCTURED,
            metric="success",
            samples=samples,
            confidence=confidence,
        )
