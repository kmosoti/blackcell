from __future__ import annotations

from blackcell.features.predict_transition.command import (
    PredictTransition,
    ScoreTransitionPrediction,
)
from blackcell.features.predict_transition.models import (
    PredictedFact,
    PredictionDisposition,
    PredictionFinding,
    PredictionFindingOutcome,
    PredictionTarget,
    TransitionPrediction,
    TransitionPredictionScore,
)
from blackcell.features.predict_transition.ports import PredictionStateLike
from blackcell.kernel import JsonScalar
from blackcell.kernel._json import canonical_json


class DeterministicTransitionPredictor:
    """Predict explicitly requested facts by conservatively persisting current state."""

    def handle(self, command: PredictTransition) -> TransitionPrediction:
        state = command.source_state
        stream_id = state.scope.stream_id
        if stream_id is None:  # pragma: no cover - command owns the public invariant
            raise ValueError("prediction source state must have a bound stream scope")
        facts = tuple(self._predict(target, command) for target in command.targets)
        return TransitionPrediction(
            source_snapshot_digest=command.source_snapshot_digest,
            source_domain=state.scope.domain,
            source_stream_id=stream_id,
            source_cutoff_global_position=state.cutoff_global_position,
            source_stream_sequence=state.last_source_stream_sequence,
            source_effective_time=state.effective_time_cutoff,
            action_digest=command.action_digest,
            action_kind=command.action_kind,
            generated_at=command.generated_at,
            horizon_seconds=command.horizon_seconds,
            facts=facts,
        )

    @staticmethod
    def _predict(target: PredictionTarget, command: PredictTransition) -> PredictedFact:
        state = command.source_state
        conflict = next((item for item in state.conflicts if item.key == target.key), None)
        matching = state.claims_for(*target.key)
        if conflict is not None:
            return _unknown(
                target,
                assumptions=("source-state-conflicted",),
                claim_ids=conflict.claim_ids,
                event_ids=conflict.source_event_ids,
            )
        observed = tuple(item for item in matching if str(item.epistemic_status) == "observed")
        if not observed:
            return _unknown(
                target,
                assumptions=("no-current-observation",),
                claim_ids=tuple(item.claim_id for item in matching),
                event_ids=tuple(item.source_event_id for item in matching),
            )
        values = _unique_values(tuple(item.value for item in observed))
        if len(values) != 1:
            return _unknown(
                target,
                assumptions=("ambiguous-source-values",),
                claim_ids=tuple(item.claim_id for item in observed),
                event_ids=tuple(item.source_event_id for item in observed),
            )
        return PredictedFact(
            target=target,
            disposition=PredictionDisposition.PREDICTED,
            value=values[0],
            confidence=min(command.confidence_cap, *(item.confidence for item in observed)),
            assumptions=("action-effect-unmodeled", "state-persistence"),
            source_claim_ids=tuple(item.claim_id for item in observed),
            source_event_ids=tuple(item.source_event_id for item in observed),
        )


class TransitionPredictionScorer:
    """Score an advisory prediction against a later canonical outcome snapshot."""

    def handle(self, command: ScoreTransitionPrediction) -> TransitionPredictionScore:
        prediction = command.prediction
        actual = command.actual_state
        _validate_outcome(prediction, actual)
        findings = tuple(self._finding(item, actual) for item in prediction.facts)
        scored = tuple(item for item in findings if item.scored)
        matched_count = sum(item.outcome is PredictionFindingOutcome.MATCH for item in findings)
        exact_match_rate = None if not scored else matched_count / len(scored)
        brier_score = (
            None
            if not scored
            else sum(
                (
                    item.predicted_confidence
                    - (1.0 if item.outcome is PredictionFindingOutcome.MATCH else 0.0)
                )
                ** 2
                for item in scored
            )
            / len(scored)
        )
        effective_time = actual.effective_time_cutoff
        if effective_time is None:  # pragma: no cover - outcome validation owns this invariant
            raise ValueError("prediction outcome requires an effective-time cutoff")
        return TransitionPredictionScore(
            prediction_id=prediction.prediction_id,
            actual_snapshot_digest=command.actual_snapshot_digest,
            actual_cutoff_global_position=actual.cutoff_global_position,
            actual_stream_sequence=actual.last_source_stream_sequence,
            actual_effective_time=effective_time,
            scored_at=command.scored_at,
            findings=findings,
            matched_count=matched_count,
            scored_count=len(scored),
            exact_match_rate=exact_match_rate,
            brier_score=brier_score,
        )

    @staticmethod
    def _finding(fact: PredictedFact, actual: PredictionStateLike) -> PredictionFinding:
        matching = actual.claims_for(*fact.target.key)
        observed = tuple(item for item in matching if str(item.epistemic_status) == "observed")
        values = _unique_values(tuple(item.value for item in observed))
        claim_ids = tuple(item.claim_id for item in matching)
        event_ids = tuple(item.source_event_id for item in matching)
        if fact.disposition is PredictionDisposition.UNKNOWN:
            outcome = PredictionFindingOutcome.PREDICTION_UNKNOWN
        elif any(item.key == fact.target.key for item in actual.conflicts) or len(values) > 1:
            outcome = PredictionFindingOutcome.ACTUAL_CONFLICT
        elif not values:
            outcome = PredictionFindingOutcome.ACTUAL_MISSING
        elif _value_key(fact.value) == _value_key(values[0]):
            outcome = PredictionFindingOutcome.MATCH
        else:
            outcome = PredictionFindingOutcome.MISMATCH
        return PredictionFinding(
            target=fact.target,
            outcome=outcome,
            predicted_value=fact.value,
            actual_values=values,
            predicted_confidence=fact.confidence,
            actual_claim_ids=claim_ids,
            actual_source_event_ids=event_ids,
        )


def _validate_outcome(
    prediction: TransitionPrediction,
    actual: PredictionStateLike,
) -> None:
    stream_id = actual.scope.stream_id
    if actual.scope.domain != prediction.source_domain or stream_id != prediction.source_stream_id:
        raise ValueError("prediction outcome scope differs from its source state")
    if (
        actual.cutoff_global_position <= prediction.source_cutoff_global_position
        or actual.last_source_stream_sequence <= prediction.source_stream_sequence
    ):
        raise ValueError("prediction outcome must be a later same-stream state")
    effective_time = actual.effective_time_cutoff
    if effective_time is None or effective_time < prediction.generated_at:
        raise ValueError("prediction outcome must have a later effective-time cutoff")


def _unknown(
    target: PredictionTarget,
    *,
    assumptions: tuple[str, ...],
    claim_ids: tuple[str, ...],
    event_ids: tuple[str, ...],
) -> PredictedFact:
    return PredictedFact(
        target=target,
        disposition=PredictionDisposition.UNKNOWN,
        value=None,
        confidence=0.0,
        assumptions=assumptions,
        source_claim_ids=claim_ids,
        source_event_ids=event_ids,
    )


def _unique_values(values: tuple[JsonScalar, ...]) -> tuple[JsonScalar, ...]:
    keyed = {_value_key(value): value for value in values}
    return tuple(value for _, value in sorted(keyed.items()))


def _value_key(value: JsonScalar) -> str:
    return canonical_json({"value": value})


__all__ = ["DeterministicTransitionPredictor", "TransitionPredictionScorer"]
