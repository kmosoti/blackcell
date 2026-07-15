from __future__ import annotations

import json
import math
import os
import platform
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any, Protocol

from blackcell.features.predict_transition import (
    DeterministicTransitionPredictor,
    PredictedFact,
    PredictionDisposition,
    PredictionFindingOutcome,
    PredictionTarget,
    PredictTransition,
    ScoreTransitionPrediction,
    TransitionPrediction,
    TransitionPredictionScore,
    TransitionPredictionScorer,
)
from blackcell.features.project_operational_state import (
    BeliefClaim,
    BeliefConflict,
    OperationalBeliefState,
    OperationalStateScope,
    operational_state_snapshot_digest,
    operational_state_snapshot_payload,
)
from blackcell.kernel import JsonScalar
from blackcell.kernel._json import json_digest

_BASE = datetime(2026, 7, 14, 12, tzinfo=UTC)
_CONDITIONS = (
    "state-persistence",
    "declared-effects",
)


class PredictionExperimentCondition(StrEnum):
    STATE_PERSISTENCE = "state-persistence"
    DECLARED_EFFECTS = "declared-effects"


class TransitionPredictor(Protocol):
    def handle(self, command: PredictTransition) -> TransitionPrediction: ...


@dataclass(frozen=True, slots=True)
class DeclaredTransitionEffect:
    target: PredictionTarget
    value: JsonScalar
    confidence: float
    source_claim_ids: tuple[str, ...]
    source_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.target, PredictionTarget):
            raise TypeError("declared effect target must be a PredictionTarget")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, int | float)
            or not math.isfinite(self.confidence)
            or not 0.0 < self.confidence <= 1.0
        ):
            raise ValueError("declared effect confidence must be finite and positive")
        claim_ids = tuple(sorted(set(self.source_claim_ids)))
        event_ids = tuple(sorted(set(self.source_event_ids)))
        if (
            not claim_ids
            or not event_ids
            or any(not item.strip() for item in (*claim_ids, *event_ids))
        ):
            raise ValueError("declared effects require non-empty source identities")
        object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "source_claim_ids", claim_ids)
        object.__setattr__(self, "source_event_ids", event_ids)


@dataclass(frozen=True, slots=True)
class PredictionBenchmarkScenario:
    scenario_id: str
    description: str
    tags: tuple[str, ...]
    source_state: OperationalBeliefState
    actual_state: OperationalBeliefState
    action_digest: str
    action_kind: str
    targets: tuple[PredictionTarget, ...]
    declared_effects: tuple[DeclaredTransitionEffect, ...]
    generated_at: datetime
    scored_at: datetime
    horizon_seconds: int = 60

    def __post_init__(self) -> None:
        if (
            not self.scenario_id.strip()
            or not self.description.strip()
            or not self.action_kind.strip()
        ):
            raise ValueError("prediction scenario identity and action must not be empty")
        _require_digest(self.action_digest, "action_digest")
        if self.source_state.scope != self.actual_state.scope:
            raise ValueError("prediction scenario states must share one scope")
        if (
            self.actual_state.cutoff_global_position <= self.source_state.cutoff_global_position
            or self.actual_state.last_source_stream_sequence
            <= self.source_state.last_source_stream_sequence
        ):
            raise ValueError("prediction scenario actual state must be later")
        generated_at = _aware(self.generated_at, "generated_at")
        scored_at = _aware(self.scored_at, "scored_at")
        source_time = self.source_state.effective_time_cutoff
        actual_time = self.actual_state.effective_time_cutoff
        if source_time is None or actual_time is None:
            raise ValueError("prediction scenario states require effective-time cutoffs")
        if generated_at < source_time or actual_time < generated_at or scored_at < actual_time:
            raise ValueError("prediction scenario times must follow source, prediction, outcome")
        targets = tuple(sorted(self.targets))
        if not targets or len(targets) != len(set(targets)):
            raise ValueError("prediction scenario targets must be non-empty and unique")
        effects = tuple(sorted(self.declared_effects, key=lambda item: item.target))
        effect_targets = tuple(item.target for item in effects)
        if len(effect_targets) != len(set(effect_targets)) or not set(effect_targets) <= set(
            targets
        ):
            raise ValueError("declared effects must uniquely target requested facts")
        source_claim_ids = {claim.claim_id for claim in self.source_state.claims}
        source_event_ids = {claim.source_event_id for claim in self.source_state.claims}
        if any(
            not set(effect.source_claim_ids) <= source_claim_ids
            or not set(effect.source_event_ids) <= source_event_ids
            for effect in effects
        ):
            raise ValueError("declared effect evidence must belong to the source state")
        if isinstance(self.horizon_seconds, bool) or self.horizon_seconds < 1:
            raise ValueError("prediction scenario horizon must be positive")
        object.__setattr__(self, "generated_at", generated_at)
        object.__setattr__(self, "scored_at", scored_at)
        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "declared_effects", effects)


@dataclass(frozen=True, slots=True)
class PredictionExperimentDesign:
    experiment_id: str
    conditions: tuple[PredictionExperimentCondition, ...] = (
        PredictionExperimentCondition.STATE_PERSISTENCE,
        PredictionExperimentCondition.DECLARED_EFFECTS,
    )
    latency_repetitions: int = 50

    def __post_init__(self) -> None:
        if not self.experiment_id.strip():
            raise ValueError("prediction experiment identity must not be empty")
        if tuple(item.value for item in self.conditions) != _CONDITIONS:
            raise ValueError("WP24 requires both canonical prediction conditions in order")
        if self.latency_repetitions < 1:
            raise ValueError("latency repetitions must be positive")


@dataclass(frozen=True, slots=True)
class PredictionScenarioManifest:
    scenario_id: str
    description: str
    tags: tuple[str, ...]
    action_digest: str
    action_kind: str
    targets: tuple[PredictionTarget, ...]
    declared_effects: tuple[DeclaredTransitionEffect, ...]
    source_snapshot: Mapping[str, object]
    actual_snapshot: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PredictionTrialRecord:
    scenario_id: str
    condition: PredictionExperimentCondition
    source_snapshot_digest: str
    actual_snapshot_digest: str
    prediction: TransitionPrediction
    score: TransitionPredictionScore
    latency_ns: tuple[int, ...]
    mean_latency_ms: float


@dataclass(frozen=True, slots=True)
class PredictionConditionAggregate:
    condition: PredictionExperimentCondition
    scenario_count: int
    target_count: int
    matched_count: int
    mismatch_count: int
    actual_missing_count: int
    actual_conflict_count: int
    prediction_unknown_count: int
    scored_count: int
    exact_match_rate: float | None
    brier_score: float | None
    target_match_rate: float
    scored_coverage: float
    mean_latency_ms: float
    p95_latency_ms: float
    input_tokens: int
    output_tokens: int
    provider_cost_usd: float


@dataclass(frozen=True, slots=True)
class PredictionPairedComparison:
    left: PredictionExperimentCondition
    right: PredictionExperimentCondition
    scenario_count: int
    target_match_rate_delta: float
    exact_match_rate_delta: float
    brier_score_delta: float
    scored_coverage_delta: float
    mean_latency_ms_delta: float
    wins: int
    losses: int
    ties: int


@dataclass(frozen=True, slots=True)
class PredictionCandidateAvailability:
    candidate: str
    status: str
    reason: str
    exact_match_rate: None = None
    brier_score: None = None
    mean_latency_ms: None = None
    input_tokens: None = None
    output_tokens: None = None
    provider_cost_usd: None = None


@dataclass(frozen=True, slots=True)
class PredictionExperimentReport:
    experiment_id: str
    scenario_digest: str
    scenario_count: int
    design: PredictionExperimentDesign
    environment: Mapping[str, str]
    scenarios: tuple[PredictionScenarioManifest, ...]
    trials: tuple[PredictionTrialRecord, ...]
    aggregates: tuple[PredictionConditionAggregate, ...]
    paired_comparison: PredictionPairedComparison
    unavailable_candidates: tuple[PredictionCandidateAvailability, ...]
    inferential: bool
    limitations: tuple[str, ...]
    schema_version: str = "prediction-bench-report/v1"
    report_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != "prediction-bench-report/v1":
            raise ValueError("prediction experiment report schema is unsupported")
        if not self.scenarios or not self.trials or len(self.aggregates) != 2:
            raise ValueError("prediction experiment reports require matched evidence")
        object.__setattr__(self, "report_id", json_digest(_report_payload(self)))


class DeclaredEffectTransitionPredictor:
    """Experiment-only symbolic effects with conservative persistence fallback."""

    name = "declared-effects-plus-state-persistence"

    def __init__(
        self,
        effects_by_action: Mapping[str, tuple[DeclaredTransitionEffect, ...]],
    ) -> None:
        validated: dict[str, tuple[DeclaredTransitionEffect, ...]] = {}
        for action_digest, effects in effects_by_action.items():
            _require_digest(action_digest, "declared effect action digest")
            effect_targets = tuple(effect.target for effect in effects)
            if len(effect_targets) != len(set(effect_targets)):
                raise ValueError("declared effects must have unique targets per action")
            validated[action_digest] = tuple(effects)
        self._effects_by_action = validated
        self._fallback = DeterministicTransitionPredictor()

    def handle(self, command: PredictTransition) -> TransitionPrediction:
        baseline = self._fallback.handle(command)
        declared = self._effects_by_action.get(command.action_digest, ())
        source_claim_ids = {claim.claim_id for claim in command.source_state.claims}
        source_event_ids = {claim.source_event_id for claim in command.source_state.claims}
        if any(
            not set(effect.source_claim_ids) <= source_claim_ids
            or not set(effect.source_event_ids) <= source_event_ids
            for effect in declared
        ):
            raise ValueError("declared effect evidence must belong to the prediction source state")
        effects = {effect.target: effect for effect in declared}
        facts = tuple(
            _declared_fact(effects[fact.target]) if fact.target in effects else fact
            for fact in baseline.facts
        )
        return TransitionPrediction(
            source_snapshot_digest=baseline.source_snapshot_digest,
            source_domain=baseline.source_domain,
            source_stream_id=baseline.source_stream_id,
            source_cutoff_global_position=baseline.source_cutoff_global_position,
            source_stream_sequence=baseline.source_stream_sequence,
            source_effective_time=baseline.source_effective_time,
            action_digest=baseline.action_digest,
            action_kind=baseline.action_kind,
            generated_at=baseline.generated_at,
            horizon_seconds=baseline.horizon_seconds,
            facts=facts,
            predictor_version="declared-effects-plus-state-persistence/v1",
        )


class PredictionExperimentRunner:
    def __init__(self, *, clock_ns: Callable[[], int] = time.perf_counter_ns) -> None:
        self._clock_ns = clock_ns

    def run(
        self,
        scenarios: Sequence[PredictionBenchmarkScenario],
        design: PredictionExperimentDesign,
    ) -> PredictionExperimentReport:
        fixtures = tuple(scenarios)
        _validate_scenario_set(fixtures)
        effects_by_action = {
            scenario.action_digest: scenario.declared_effects for scenario in fixtures
        }
        predictors: dict[PredictionExperimentCondition, TransitionPredictor] = {
            PredictionExperimentCondition.STATE_PERSISTENCE: (DeterministicTransitionPredictor()),
            PredictionExperimentCondition.DECLARED_EFFECTS: (
                DeclaredEffectTransitionPredictor(effects_by_action)
            ),
        }
        scorer = TransitionPredictionScorer()
        records: list[PredictionTrialRecord] = []
        for scenario in fixtures:
            command = _prediction_command(scenario)
            actual_digest = operational_state_snapshot_digest(scenario.actual_state)
            for condition in design.conditions:
                prediction: TransitionPrediction | None = None
                score: TransitionPredictionScore | None = None
                latencies: list[int] = []
                for _ in range(design.latency_repetitions):
                    started = self._clock_ns()
                    candidate_prediction = predictors[condition].handle(command)
                    candidate_score = scorer.handle(
                        ScoreTransitionPrediction(
                            candidate_prediction,
                            scenario.actual_state,
                            actual_digest,
                            scenario.scored_at,
                        )
                    )
                    elapsed = self._clock_ns() - started
                    if elapsed < 0:
                        raise ValueError("prediction experiment clock moved backwards")
                    latencies.append(elapsed)
                    if prediction is None:
                        prediction = candidate_prediction
                        score = candidate_score
                    elif prediction != candidate_prediction or score != candidate_score:
                        raise ValueError(
                            "deterministic prediction condition changed across repeats"
                        )
                if prediction is None or score is None:  # pragma: no cover - design validates
                    raise ValueError("prediction experiment produced no trial")
                records.append(
                    PredictionTrialRecord(
                        scenario_id=scenario.scenario_id,
                        condition=condition,
                        source_snapshot_digest=operational_state_snapshot_digest(
                            scenario.source_state
                        ),
                        actual_snapshot_digest=actual_digest,
                        prediction=prediction,
                        score=score,
                        latency_ns=tuple(latencies),
                        mean_latency_ms=sum(latencies) / len(latencies) / 1_000_000,
                    )
                )
        manifests = tuple(_manifest(item) for item in fixtures)
        aggregates = tuple(_aggregate(tuple(records), condition) for condition in design.conditions)
        return PredictionExperimentReport(
            experiment_id=design.experiment_id,
            scenario_digest=json_digest(_jsonable(manifests)),
            scenario_count=len(fixtures),
            design=design,
            environment=_environment(),
            scenarios=manifests,
            trials=tuple(records),
            aggregates=aggregates,
            paired_comparison=_paired_comparison(tuple(records), aggregates),
            unavailable_candidates=_unavailable_candidates(),
            inferential=False,
            limitations=_limitations(len(fixtures)),
        )


class PredictionReportReservation:
    """Reserve one owner-only prediction artifact before measurements begin."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise FileExistsError(f"experiment artifact already exists: {path}") from None
        self._stream = os.fdopen(descriptor, "wb")
        self._committed = False

    def __enter__(self) -> PredictionReportReservation:
        return self

    def commit(self, report: PredictionExperimentReport) -> None:
        if self._committed or self._stream.closed:
            raise RuntimeError("experiment artifact reservation is already closed")
        self._stream.write(encode_prediction_report(report).encode("utf-8"))
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._stream.close()
        self._committed = True

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if not self._stream.closed:
            self._stream.close()
        if not self._committed:
            self.path.unlink(missing_ok=True)


def prediction_bench_scenarios() -> tuple[PredictionBenchmarkScenario, ...]:
    return (
        _stable_scenario(),
        _declared_change_scenario(),
        _declared_miss_scenario(),
        _unexpected_change_scenario(),
        _source_missing_scenario(),
        _source_conflict_scenario(),
        _actual_missing_scenario(),
        _actual_conflict_scenario(),
    )


def encode_prediction_report(report: PredictionExperimentReport) -> str:
    return json.dumps(_jsonable(report), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_prediction_report(path: Path, report: PredictionExperimentReport) -> None:
    with PredictionReportReservation(path) as reservation:
        reservation.commit(report)


def _prediction_command(scenario: PredictionBenchmarkScenario) -> PredictTransition:
    return PredictTransition(
        source_state=scenario.source_state,
        source_snapshot_digest=operational_state_snapshot_digest(scenario.source_state),
        action_digest=scenario.action_digest,
        action_kind=scenario.action_kind,
        targets=scenario.targets,
        generated_at=scenario.generated_at,
        horizon_seconds=scenario.horizon_seconds,
    )


def _declared_fact(effect: DeclaredTransitionEffect) -> PredictedFact:
    return PredictedFact(
        target=effect.target,
        disposition=PredictionDisposition.PREDICTED,
        value=effect.value,
        confidence=effect.confidence,
        assumptions=("developer-declared-action-effect", "experiment-only"),
        source_claim_ids=effect.source_claim_ids,
        source_event_ids=effect.source_event_ids,
    )


def _manifest(scenario: PredictionBenchmarkScenario) -> PredictionScenarioManifest:
    return PredictionScenarioManifest(
        scenario_id=scenario.scenario_id,
        description=scenario.description,
        tags=scenario.tags,
        action_digest=scenario.action_digest,
        action_kind=scenario.action_kind,
        targets=scenario.targets,
        declared_effects=scenario.declared_effects,
        source_snapshot=operational_state_snapshot_payload(scenario.source_state),
        actual_snapshot=operational_state_snapshot_payload(scenario.actual_state),
    )


def _aggregate(
    records: tuple[PredictionTrialRecord, ...],
    condition: PredictionExperimentCondition,
) -> PredictionConditionAggregate:
    selected = tuple(item for item in records if item.condition is condition)
    findings = tuple(finding for item in selected for finding in item.score.findings)
    matched = sum(item.outcome is PredictionFindingOutcome.MATCH for item in findings)
    mismatched = sum(item.outcome is PredictionFindingOutcome.MISMATCH for item in findings)
    missing = sum(item.outcome is PredictionFindingOutcome.ACTUAL_MISSING for item in findings)
    conflicted = sum(item.outcome is PredictionFindingOutcome.ACTUAL_CONFLICT for item in findings)
    unknown = sum(item.outcome is PredictionFindingOutcome.PREDICTION_UNKNOWN for item in findings)
    scored = matched + mismatched
    brier = (
        None
        if not scored
        else sum(
            (
                item.predicted_confidence
                - (1.0 if item.outcome is PredictionFindingOutcome.MATCH else 0.0)
            )
            ** 2
            for item in findings
            if item.scored
        )
        / scored
    )
    latencies = tuple(value for item in selected for value in item.latency_ns)
    target_count = len(findings)
    return PredictionConditionAggregate(
        condition=condition,
        scenario_count=len(selected),
        target_count=target_count,
        matched_count=matched,
        mismatch_count=mismatched,
        actual_missing_count=missing,
        actual_conflict_count=conflicted,
        prediction_unknown_count=unknown,
        scored_count=scored,
        exact_match_rate=None if not scored else matched / scored,
        brier_score=brier,
        target_match_rate=matched / target_count,
        scored_coverage=scored / target_count,
        mean_latency_ms=sum(latencies) / len(latencies) / 1_000_000,
        p95_latency_ms=_percentile(latencies, 0.95) / 1_000_000,
        input_tokens=0,
        output_tokens=0,
        provider_cost_usd=0.0,
    )


def _paired_comparison(
    records: tuple[PredictionTrialRecord, ...],
    aggregates: tuple[PredictionConditionAggregate, ...],
) -> PredictionPairedComparison:
    by_key = {(item.scenario_id, item.condition): item for item in records}
    left_condition = PredictionExperimentCondition.STATE_PERSISTENCE
    right_condition = PredictionExperimentCondition.DECLARED_EFFECTS
    scenario_ids = sorted({item.scenario_id for item in records})
    wins = losses = ties = 0
    for scenario_id in scenario_ids:
        left_match = any(
            item.outcome is PredictionFindingOutcome.MATCH
            for item in by_key[(scenario_id, left_condition)].score.findings
        )
        right_match = any(
            item.outcome is PredictionFindingOutcome.MATCH
            for item in by_key[(scenario_id, right_condition)].score.findings
        )
        if right_match and not left_match:
            wins += 1
        elif left_match and not right_match:
            losses += 1
        else:
            ties += 1
    by_condition = {item.condition: item for item in aggregates}
    left = by_condition[left_condition]
    right = by_condition[right_condition]
    if (
        left.exact_match_rate is None
        or right.exact_match_rate is None
        or left.brier_score is None
        or right.brier_score is None
    ):
        raise ValueError("WP24 paired conditions require scored outcomes")
    return PredictionPairedComparison(
        left=left_condition,
        right=right_condition,
        scenario_count=len(scenario_ids),
        target_match_rate_delta=right.target_match_rate - left.target_match_rate,
        exact_match_rate_delta=right.exact_match_rate - left.exact_match_rate,
        brier_score_delta=right.brier_score - left.brier_score,
        scored_coverage_delta=right.scored_coverage - left.scored_coverage,
        mean_latency_ms_delta=right.mean_latency_ms - left.mean_latency_ms,
        wins=wins,
        losses=losses,
        ties=ties,
    )


def _validate_scenario_set(scenarios: tuple[PredictionBenchmarkScenario, ...]) -> None:
    if not scenarios:
        raise ValueError("prediction experiments require scenarios")
    scenario_ids = tuple(item.scenario_id for item in scenarios)
    action_digests = tuple(item.action_digest for item in scenarios)
    if len(scenario_ids) != len(set(scenario_ids)):
        raise ValueError("prediction scenario identities must be unique")
    if len(action_digests) != len(set(action_digests)):
        raise ValueError("prediction scenario actions must be unique")


def _unavailable_candidates() -> tuple[PredictionCandidateAvailability, ...]:
    return (
        PredictionCandidateAvailability(
            candidate="local-neural",
            status="unavailable",
            reason=(
                "WP11 found no installed offline runtime, configured gateway prediction route, "
                "or pinned predictor to evaluate"
            ),
        ),
        PredictionCandidateAvailability(
            candidate="hybrid-neural-symbolic",
            status="unavailable",
            reason=(
                "no neural transition predictor exists, and the WP12 Clingo adapter validates "
                "action policy rather than predicting outcomes"
            ),
        ),
    )


def _limitations(scenario_count: int) -> tuple[str, ...]:
    return (
        f"the dataset contains {scenario_count} author-crafted synthetic one-step scenarios",
        "declared effects are developer-authored inputs, not learned estimates or hidden outcomes",
        "the comparison has no held-out scenario families and is not inferential",
        "microbenchmark latency is environment-specific and excludes provider, network, "
        "and model cost",
        "deterministic conditions use zero model tokens and zero provider cost; unavailable neural "
        "candidates retain null measures",
        "the experiment does not test multi-step rollout quality or downstream planning utility",
    )


def _environment() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "system": platform.system(),
        "machine": platform.machine(),
        "clock": "time.perf_counter_ns",
    }


def _percentile(values: tuple[int, ...], quantile: float) -> int:
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def _report_payload(report: PredictionExperimentReport) -> dict[str, Any]:
    return {
        item.name: _jsonable(getattr(report, item.name))
        for item in fields(report)
        if item.name != "report_id"
    }


def _jsonable(value: object) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def _stable_scenario() -> PredictionBenchmarkScenario:
    scenario_id = "stable-state"
    source = _state(scenario_id, (_claim(scenario_id, "status", "ready", 1),), 1, _BASE)
    actual = _state(
        scenario_id,
        (
            _claim(
                scenario_id,
                "status-actual",
                "ready",
                2,
                predicate="status",
                at=_BASE + timedelta(seconds=60),
            ),
        ),
        2,
        _BASE + timedelta(seconds=60),
    )
    return _scenario(
        scenario_id,
        "A read-only inspection leaves current state stable.",
        source,
        actual,
        "inspect",
    )


def _declared_change_scenario() -> PredictionBenchmarkScenario:
    scenario_id = "declared-change"
    status = _claim(scenario_id, "status", "pending", 1)
    intent = _claim(scenario_id, "intent", "running", 2, predicate="declared-status")
    source = _state(scenario_id, (status, intent), 2, _BASE)
    actual = _state(
        scenario_id,
        (
            _claim(
                scenario_id,
                "status-actual",
                "running",
                3,
                predicate="status",
                at=_BASE + timedelta(seconds=60),
            ),
        ),
        3,
        _BASE + timedelta(seconds=60),
    )
    effect = _effect("status", "running", 0.85, intent)
    return _scenario(
        scenario_id, "A declared start effect occurs.", source, actual, "start", (effect,)
    )


def _declared_miss_scenario() -> PredictionBenchmarkScenario:
    scenario_id = "declared-effect-miss"
    status = _claim(scenario_id, "status", "pending", 1)
    intent = _claim(scenario_id, "intent", "running", 2, predicate="declared-status")
    source = _state(scenario_id, (status, intent), 2, _BASE)
    actual = _state(
        scenario_id,
        (
            _claim(
                scenario_id,
                "status-actual",
                "failed",
                3,
                predicate="status",
                at=_BASE + timedelta(seconds=60),
            ),
        ),
        3,
        _BASE + timedelta(seconds=60),
    )
    effect = _effect("status", "running", 0.85, intent)
    return _scenario(
        scenario_id,
        "A declared effect fails, proving that the symbolic baseline is not an oracle.",
        source,
        actual,
        "start",
        (effect,),
    )


def _unexpected_change_scenario() -> PredictionBenchmarkScenario:
    scenario_id = "unexpected-change"
    source = _state(scenario_id, (_claim(scenario_id, "count", 1, 1, predicate="count"),), 1, _BASE)
    actual = _state(
        scenario_id,
        (
            _claim(
                scenario_id,
                "count-actual",
                2,
                2,
                predicate="count",
                at=_BASE + timedelta(seconds=60),
            ),
        ),
        2,
        _BASE + timedelta(seconds=60),
    )
    return _scenario(
        scenario_id,
        "An undeclared external change defeats persistence.",
        source,
        actual,
        "refresh",
        target="count",
    )


def _source_missing_scenario() -> PredictionBenchmarkScenario:
    scenario_id = "source-missing-created"
    intent = _claim(scenario_id, "intent", "created", 1, predicate="declared-status")
    source = _state(scenario_id, (intent,), 1, _BASE)
    actual = _state(
        scenario_id,
        (
            _claim(
                scenario_id,
                "status-actual",
                "created",
                2,
                predicate="status",
                at=_BASE + timedelta(seconds=60),
            ),
        ),
        2,
        _BASE + timedelta(seconds=60),
    )
    effect = _effect("status", "created", 0.9, intent)
    return _scenario(
        scenario_id,
        "A declared create effect supplies a target missing from source state.",
        source,
        actual,
        "create",
        (effect,),
    )


def _source_conflict_scenario() -> PredictionBenchmarkScenario:
    scenario_id = "source-conflict-resolved"
    mode_a = _claim(scenario_id, "mode-a", "a", 1, predicate="mode")
    mode_b = _claim(scenario_id, "mode-b", "b", 2, predicate="mode")
    intent = _claim(scenario_id, "intent", "b", 3, predicate="declared-mode")
    conflict = _conflict("mode", mode_a, mode_b)
    source = _state(scenario_id, (mode_a, mode_b, intent), 3, _BASE, (conflict,))
    actual = _state(
        scenario_id,
        (
            _claim(
                scenario_id,
                "mode-actual",
                "b",
                4,
                predicate="mode",
                at=_BASE + timedelta(seconds=60),
            ),
        ),
        4,
        _BASE + timedelta(seconds=60),
    )
    effect = _effect("mode", "b", 0.8, intent)
    return _scenario(
        scenario_id,
        "A declared resolution disambiguates conflicted source evidence.",
        source,
        actual,
        "resolve",
        (effect,),
        target="mode",
    )


def _actual_missing_scenario() -> PredictionBenchmarkScenario:
    scenario_id = "actual-missing"
    source = _state(scenario_id, (_claim(scenario_id, "status", "present", 1),), 1, _BASE)
    actual = _state(
        scenario_id,
        (
            _claim(
                scenario_id,
                "other",
                "retained",
                2,
                predicate="other",
                at=_BASE + timedelta(seconds=60),
            ),
        ),
        2,
        _BASE + timedelta(seconds=60),
    )
    return _scenario(
        scenario_id,
        "The requested target is absent from the outcome snapshot.",
        source,
        actual,
        "expire",
    )


def _actual_conflict_scenario() -> PredictionBenchmarkScenario:
    scenario_id = "actual-conflict"
    source = _state(scenario_id, (_claim(scenario_id, "status", "pending", 1),), 1, _BASE)
    running = _claim(
        scenario_id, "running", "running", 2, predicate="status", at=_BASE + timedelta(seconds=60)
    )
    failed = _claim(
        scenario_id, "failed", "failed", 3, predicate="status", at=_BASE + timedelta(seconds=60)
    )
    actual = _state(
        scenario_id,
        (running, failed),
        3,
        _BASE + timedelta(seconds=60),
        (_conflict("status", running, failed),),
    )
    return _scenario(
        scenario_id, "The outcome contains contradictory target values.", source, actual, "observe"
    )


def _scenario(
    scenario_id: str,
    description: str,
    source: OperationalBeliefState,
    actual: OperationalBeliefState,
    action_kind: str,
    effects: tuple[DeclaredTransitionEffect, ...] = (),
    *,
    target: str = "status",
) -> PredictionBenchmarkScenario:
    return PredictionBenchmarkScenario(
        scenario_id=scenario_id,
        description=description,
        tags=_scenario_tags(scenario_id),
        source_state=source,
        actual_state=actual,
        action_digest=json_digest({"scenario_id": scenario_id, "action_kind": action_kind}),
        action_kind=action_kind,
        targets=(PredictionTarget("project", target),),
        declared_effects=effects,
        generated_at=_BASE + timedelta(seconds=1),
        scored_at=_BASE + timedelta(seconds=61),
    )


def _scenario_tags(scenario_id: str) -> tuple[str, ...]:
    tags = {
        "stable-state": ("persistence",),
        "declared-change": ("declared-effect", "change"),
        "declared-effect-miss": ("declared-effect", "unexpected-outcome"),
        "unexpected-change": ("unmodeled-change",),
        "source-missing-created": ("source-missing", "declared-effect"),
        "source-conflict-resolved": ("source-conflict", "declared-effect"),
        "actual-missing": ("actual-missing",),
        "actual-conflict": ("actual-conflict",),
    }
    return tags[scenario_id]


def _state(
    scenario_id: str,
    claims: tuple[BeliefClaim, ...],
    position: int,
    effective: datetime,
    conflicts: tuple[BeliefConflict, ...] = (),
) -> OperationalBeliefState:
    return OperationalBeliefState(
        scope=OperationalStateScope("prediction-bench", f"prediction-bench:{scenario_id}"),
        claims=claims,
        conflicts=conflicts,
        cutoff_global_position=position,
        last_source_stream_sequence=position,
        effective_time_cutoff=effective,
    )


def _claim(
    scenario_id: str,
    identity: str,
    value: JsonScalar,
    position: int,
    *,
    predicate: str = "status",
    at: datetime = _BASE - timedelta(seconds=1),
) -> BeliefClaim:
    return BeliefClaim(
        claim_id=f"claim:{scenario_id}:{identity}",
        subject="project",
        predicate=predicate,
        value=value,
        confidence=1.0,
        effective_at=at,
        recorded_at=at,
        source_event_id=f"event:{scenario_id}:{identity}",
        source="prediction-bench",
        actor="fixture",
        correlation_id=f"scenario:{scenario_id}",
        domain="prediction-bench",
        stream_id=f"prediction-bench:{scenario_id}",
        stream_sequence=position,
        global_position=position,
    )


def _effect(
    predicate: str,
    value: JsonScalar,
    confidence: float,
    support: BeliefClaim,
) -> DeclaredTransitionEffect:
    return DeclaredTransitionEffect(
        target=PredictionTarget("project", predicate),
        value=value,
        confidence=confidence,
        source_claim_ids=(support.claim_id,),
        source_event_ids=(support.source_event_id,),
    )


def _conflict(predicate: str, *claims: BeliefClaim) -> BeliefConflict:
    return BeliefConflict(
        subject="project",
        predicate=predicate,
        source_event_ids=tuple(item.source_event_id for item in claims),
        claim_ids=tuple(item.claim_id for item in claims),
        values=tuple(item.value for item in claims),
    )


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _require_digest(value: str, label: str) -> None:
    hexadecimal = value.removeprefix("sha256:")
    if not value.startswith("sha256:") or len(hexadecimal) != 64:
        raise ValueError(f"{label} must be a SHA-256 digest")
    try:
        int(hexadecimal, 16)
    except ValueError as error:
        raise ValueError(f"{label} must be a SHA-256 digest") from error


__all__ = [
    "DeclaredEffectTransitionPredictor",
    "DeclaredTransitionEffect",
    "PredictionBenchmarkScenario",
    "PredictionCandidateAvailability",
    "PredictionConditionAggregate",
    "PredictionExperimentCondition",
    "PredictionExperimentDesign",
    "PredictionExperimentReport",
    "PredictionExperimentRunner",
    "PredictionPairedComparison",
    "PredictionReportReservation",
    "PredictionScenarioManifest",
    "PredictionTrialRecord",
    "encode_prediction_report",
    "prediction_bench_scenarios",
    "write_prediction_report",
]
