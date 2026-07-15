from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from blackcell.evaluation import (
    DeclaredEffectTransitionPredictor,
    DeclaredTransitionEffect,
    PredictionExperimentCondition,
    PredictionExperimentDesign,
    PredictionExperimentRunner,
    PredictionReportReservation,
    encode_prediction_report,
    prediction_bench_scenarios,
    write_prediction_report,
)
from blackcell.features.predict_transition import (
    PredictionTarget,
    PredictTransition,
    prediction_payload,
)
from blackcell.features.project_operational_state import operational_state_snapshot_digest


class _TickClock:
    def __init__(self, tick: int = 1_000) -> None:
        self._value = 0
        self._tick = tick

    def __call__(self) -> int:
        self._value += self._tick
        return self._value


def test_prediction_bench_is_matched_advisory_reproducible_and_noninferential() -> None:
    design = PredictionExperimentDesign("wp24-test", latency_repetitions=3)

    first = PredictionExperimentRunner(clock_ns=_TickClock()).run(
        prediction_bench_scenarios(), design
    )
    second = PredictionExperimentRunner(clock_ns=_TickClock()).run(
        prediction_bench_scenarios(), design
    )

    assert first == second
    assert first.report_id.startswith("sha256:")
    assert first.scenario_count == 8
    assert len(first.trials) == 16
    assert all(len(item.latency_ns) == 3 for item in first.trials)
    assert all(item.latency_ns == (1_000, 1_000, 1_000) for item in first.trials)
    assert all(
        prediction_payload(item.prediction)["advisory_only"] is True for item in first.trials
    )
    assert first.inferential is False

    aggregates = {item.condition: item for item in first.aggregates}
    persistence = aggregates[PredictionExperimentCondition.STATE_PERSISTENCE]
    declared = aggregates[PredictionExperimentCondition.DECLARED_EFFECTS]
    assert persistence.exact_match_rate == 0.25
    assert persistence.brier_score == 0.4375
    assert persistence.target_match_rate == 0.125
    assert persistence.scored_coverage == 0.5
    assert persistence.prediction_unknown_count == 2
    assert declared.exact_match_rate == pytest.approx(4 / 6)
    assert declared.brier_score == pytest.approx(0.23666666666666666)
    assert declared.target_match_rate == 0.5
    assert declared.scored_coverage == 0.75
    assert declared.prediction_unknown_count == 0
    assert all(item.actual_missing_count == 1 for item in first.aggregates)
    assert all(item.actual_conflict_count == 1 for item in first.aggregates)
    assert all(item.input_tokens == item.output_tokens == 0 for item in first.aggregates)
    assert all(item.provider_cost_usd == 0 for item in first.aggregates)

    comparison = first.paired_comparison
    assert comparison.target_match_rate_delta == 0.375
    assert comparison.exact_match_rate_delta == pytest.approx(5 / 12)
    assert comparison.brier_score_delta == pytest.approx(-0.20083333333333334)
    assert comparison.scored_coverage_delta == 0.25
    assert (comparison.wins, comparison.losses, comparison.ties) == (3, 0, 5)


def test_declared_effect_baseline_has_source_evidence_and_is_not_an_outcome_oracle() -> None:
    scenarios = prediction_bench_scenarios()
    declared_miss = next(item for item in scenarios if item.scenario_id == "declared-effect-miss")
    effect = declared_miss.declared_effects[0]
    actual = declared_miss.actual_state.claims_for("project", "status")[0]
    source_claim_ids = {item.claim_id for item in declared_miss.source_state.claims}
    source_event_ids = {item.source_event_id for item in declared_miss.source_state.claims}

    assert effect.value == "running"
    assert actual.value == "failed"
    assert set(effect.source_claim_ids) <= source_claim_ids
    assert set(effect.source_event_ids) <= source_event_ids

    report = PredictionExperimentRunner(clock_ns=_TickClock()).run(
        scenarios,
        PredictionExperimentDesign("wp24-provenance", latency_repetitions=1),
    )
    trial = next(
        item
        for item in report.trials
        if item.scenario_id == declared_miss.scenario_id
        and item.condition is PredictionExperimentCondition.DECLARED_EFFECTS
    )
    assert trial.prediction.facts[0].value == "running"
    assert trial.score.findings[0].actual_values == ("failed",)


def test_declared_effect_predictor_rejects_evidence_outside_the_source_snapshot() -> None:
    scenario = prediction_bench_scenarios()[0]
    foreign = DeclaredTransitionEffect(
        target=PredictionTarget("project", "status"),
        value="ready",
        confidence=0.8,
        source_claim_ids=("claim:foreign",),
        source_event_ids=("event:foreign",),
    )
    predictor = DeclaredEffectTransitionPredictor({scenario.action_digest: (foreign,)})

    with pytest.raises(ValueError, match="belong to the prediction source state"):
        predictor.handle(
            PredictTransition(
                source_state=scenario.source_state,
                source_snapshot_digest=operational_state_snapshot_digest(scenario.source_state),
                action_digest=scenario.action_digest,
                action_kind=scenario.action_kind,
                targets=scenario.targets,
                generated_at=scenario.generated_at,
                horizon_seconds=scenario.horizon_seconds,
            )
        )


def test_unavailable_neural_candidates_keep_null_measures() -> None:
    report = _report()

    assert {item.candidate: item.status for item in report.unavailable_candidates} == {
        "local-neural": "unavailable",
        "hybrid-neural-symbolic": "unavailable",
    }
    assert all(item.exact_match_rate is None for item in report.unavailable_candidates)
    assert all(item.brier_score is None for item in report.unavailable_candidates)
    assert all(item.mean_latency_ms is None for item in report.unavailable_candidates)
    assert all(item.input_tokens is None for item in report.unavailable_candidates)
    assert all(item.provider_cost_usd is None for item in report.unavailable_candidates)
    assert any("not learned estimates" in item for item in report.limitations)


def test_prediction_report_artifact_is_owner_only_canonical_and_exclusive(
    tmp_path: Path,
) -> None:
    report = _report()
    artifact = tmp_path / "wp24.json"

    write_prediction_report(artifact, report)

    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["report_id"] == report.report_id
    assert artifact.read_text(encoding="utf-8") == encode_prediction_report(report)
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError, match="already exists"):
        write_prediction_report(artifact, report)


def test_uncommitted_prediction_artifact_reservation_is_removed(tmp_path: Path) -> None:
    artifact = tmp_path / "interrupted.json"

    with (
        pytest.raises(RuntimeError, match="interrupted"),
        PredictionReportReservation(artifact),
    ):
        raise RuntimeError("interrupted")

    assert not artifact.exists()


def _report():
    return PredictionExperimentRunner(clock_ns=_TickClock()).run(
        prediction_bench_scenarios(),
        PredictionExperimentDesign("wp24-artifact", latency_repetitions=2),
    )
