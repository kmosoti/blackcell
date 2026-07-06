from math import sqrt

from blackcell.latent.encoder import encode_world_state
from blackcell.latent.ids import stable_digest
from blackcell.latent.models import (
    FeatureMap,
    LatentAction,
    LatentPrediction,
    LatentPredictionError,
    LatentSimulation,
    LatentState,
    LatentTransition,
    PredictionSet,
    SelfSupervisionSample,
)
from blackcell.world.models import WorldSnapshot


def default_actions() -> tuple[LatentAction, ...]:
    return (
        LatentAction(
            action_id="action:observe-validate",
            kind="observe-validate",
            summary="Refresh world facts and validate NeSy constraints.",
            changed_zones=("world", "nesy"),
            planned_checks=("uv run blackcell world facts", "uv run blackcell nesy validate"),
        ),
        LatentAction(
            action_id="action:harness-dry-run",
            kind="harness-dry-run",
            summary="Run the harness through the dry-run adapter.",
            changed_zones=("harness", "runtime"),
            planned_checks=("uv run blackcell harness plan",),
        ),
        LatentAction(
            action_id="action:docs-spec-sync",
            kind="docs-spec-sync",
            summary="Synchronize durable docs/spec with observed architecture.",
            changed_zones=("docs", "spec"),
            planned_checks=("uv run pytest tests/unit/test_docs_graph.py",),
        ),
    )


def predict_next_states(
    state: LatentState,
    *,
    actions: tuple[LatentAction, ...] | None = None,
    transition_memory: tuple[LatentTransition, ...] = (),
    confidence_labels_by_action: dict[str, str] | None = None,
) -> PredictionSet:
    candidates = actions or default_actions()
    confidence_labels = confidence_labels_by_action or {}
    predictions = tuple(
        _predict_for_action(
            state,
            action,
            sample_count=_matching_transition_count(state, action, transition_memory),
            confidence_label=confidence_labels.get(action.action_id),
        )
        for action in candidates
    )
    return PredictionSet(state=state, predictions=predictions)


def simulate_transition(
    snapshot: WorldSnapshot,
    *,
    transition_memory: tuple[LatentTransition, ...] = (),
    confidence_labels_by_action: dict[str, str] | None = None,
) -> LatentSimulation:
    """Run the V0 latent loop without mutating repo or requiring a trained model."""

    state = encode_world_state(snapshot)
    prediction_set = predict_next_states(
        state,
        transition_memory=transition_memory,
        confidence_labels_by_action=confidence_labels_by_action,
    )
    prediction = prediction_set.predictions[0]
    actual = encode_world_state(
        snapshot,
        source="latent.simulate:after",
        policy={"last_action": prediction.action.kind},
    )
    error = compare_prediction(prediction, actual)
    transition = LatentTransition(
        transition_id=stable_digest(
            "latent-transition",
            (
                state.state_id,
                prediction.action.action_id,
                prediction.prediction_id,
                actual.state_id,
            ),
        ),
        from_state_id=state.state_id,
        action_id=prediction.action.action_id,
        predicted_state_id=prediction.predicted_state.state_id,
        actual_state_id=actual.state_id,
        error_id=error.error_id,
        outcome="simulated",
    )
    sample = SelfSupervisionSample(
        sample_id=stable_digest("self-supervision", (transition.transition_id, error.error_id)),
        task="next_state_prediction",
        context_state_id=state.state_id,
        action_id=prediction.action.action_id,
        target_state_id=actual.state_id,
        prediction_id=prediction.prediction_id,
        loss={
            "semantic_distance": error.semantic_distance,
            "structural_delta_count": len(error.structural_delta),
            "symbolic_delta_count": len(error.symbolic_delta),
        },
        accepted_for_training=False,
    )
    return LatentSimulation(
        state=state,
        prediction=prediction,
        actual_state=actual,
        error=error,
        transition=transition,
        self_supervision_sample=sample,
    )


def compare_prediction(
    prediction: LatentPrediction, actual_state: LatentState
) -> LatentPredictionError:
    predicted = prediction.predicted_state
    semantic_distance = _euclidean(predicted.semantic, actual_state.semantic)
    structural_delta = _feature_delta(predicted.structural, actual_state.structural)
    symbolic_delta = _feature_delta(predicted.symbolic, actual_state.symbolic)
    surprise = "none"
    if semantic_distance > 0.25 or structural_delta or symbolic_delta:
        surprise = "latent_prediction_mismatch"
    payload = {
        "prediction_id": prediction.prediction_id,
        "predicted_state_id": predicted.state_id,
        "actual_state_id": actual_state.state_id,
        "semantic_distance": semantic_distance,
        "structural_delta": structural_delta,
        "symbolic_delta": symbolic_delta,
    }
    return LatentPredictionError(
        error_id=stable_digest("latent-error", payload),
        prediction_id=prediction.prediction_id,
        predicted_state_id=predicted.state_id,
        actual_state_id=actual_state.state_id,
        semantic_distance=semantic_distance,
        structural_delta=structural_delta,
        symbolic_delta=symbolic_delta,
        surprise=surprise,
    )


def _predict_for_action(
    state: LatentState,
    action: LatentAction,
    *,
    sample_count: int,
    confidence_label: str | None = None,
) -> LatentPrediction:
    policy = dict(state.policy)
    policy.update(
        {
            "last_action": action.kind,
            "runtime": action.runtime,
            "training_enabled": False,
        }
    )
    structural = dict(state.structural)
    structural["predicted_changed_zone_count"] = len(action.changed_zones)
    predicted_payload = {
        "source": f"prediction:{action.kind}",
        "semantic": state.semantic,
        "structural": structural,
        "telemetry": state.telemetry,
        "policy": policy,
        "symbolic": state.symbolic,
        "encoder_version": state.encoder_version,
    }
    predicted_state = LatentState(
        state_id=stable_digest("latent-state", predicted_payload),
        source=f"prediction:{action.kind}",
        semantic=state.semantic,
        structural=structural,
        telemetry=state.telemetry,
        policy=policy,
        symbolic=state.symbolic,
        encoder_version=state.encoder_version,
    )
    confidence = round(0.2 + min(sample_count, 8) * 0.08, 2)
    label = _effective_confidence_label(sample_count, confidence_label)
    prediction_payload = (
        state.state_id,
        action.action_id,
        predicted_state.state_id,
        sample_count,
        label,
    )
    return LatentPrediction(
        prediction_id=stable_digest("latent-prediction", prediction_payload),
        from_state_id=state.state_id,
        action=action,
        predicted_state=predicted_state,
        confidence=confidence,
        confidence_label=label,
        sample_count=sample_count,
        likely_surprises=_likely_surprises(sample_count, label),
        required_checks=action.planned_checks,
    )


def _matching_transition_count(
    state: LatentState,
    action: LatentAction,
    transition_memory: tuple[LatentTransition, ...],
) -> int:
    return sum(
        1
        for transition in transition_memory
        if transition.action_id == action.action_id and transition.from_state_id == state.state_id
    )


def _confidence_label(sample_count: int) -> str:
    if sample_count == 0:
        return "cold"
    if sample_count < 3:
        return "warming"
    return "grounded"


def _effective_confidence_label(sample_count: int, external_label: str | None) -> str:
    local_label = _confidence_label(sample_count)
    if sample_count == 0:
        return local_label
    if external_label is None:
        return local_label
    ordered = ("cold", "warming", "grounded")
    if external_label not in ordered:
        return local_label
    return ordered[min(ordered.index(local_label), ordered.index(external_label))]


def _likely_surprises(sample_count: int, confidence_label: str) -> tuple[str, ...]:
    surprises: list[str] = []
    if sample_count == 0:
        surprises.append("low_sample_count")
    if confidence_label == "cold":
        surprises.append("cold_action_memory")
    return tuple(surprises)


def _euclidean(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return round(sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True))), 6)


def _feature_delta(left: FeatureMap, right: FeatureMap) -> FeatureMap:
    delta: FeatureMap = {}
    for key in sorted(set(left) | set(right)):
        if left.get(key) != right.get(key):
            delta[key] = f"{left.get(key)!r}->{right.get(key)!r}"
    return delta
