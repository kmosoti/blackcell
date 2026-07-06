from __future__ import annotations

from dataclasses import dataclass

FeatureMap = dict[str, int | float | str | bool]


@dataclass(frozen=True, slots=True)
class LatentState:
    """Inspectable latent capsule for JEPA-inspired state prediction."""

    state_id: str
    source: str
    semantic: tuple[float, ...]
    structural: FeatureMap
    telemetry: FeatureMap
    policy: FeatureMap
    symbolic: FeatureMap
    encoder_version: str = "latent-v0-deterministic"


@dataclass(frozen=True, slots=True)
class LatentAction:
    action_id: str
    kind: str
    summary: str
    changed_zones: tuple[str, ...]
    runtime: str = "dry-run"
    planned_checks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LatentPrediction:
    prediction_id: str
    from_state_id: str
    action: LatentAction
    predicted_state: LatentState
    confidence: float
    confidence_label: str
    sample_count: int
    likely_surprises: tuple[str, ...]
    required_checks: tuple[str, ...]
    predictor_version: str = "transition-memory-v0-non-parametric"


@dataclass(frozen=True, slots=True)
class PredictionSet:
    state: LatentState
    predictions: tuple[LatentPrediction, ...]


@dataclass(frozen=True, slots=True)
class LatentPredictionError:
    error_id: str
    prediction_id: str
    predicted_state_id: str
    actual_state_id: str
    semantic_distance: float
    structural_delta: FeatureMap
    symbolic_delta: FeatureMap
    surprise: str


@dataclass(frozen=True, slots=True)
class LatentTransition:
    transition_id: str
    from_state_id: str
    action_id: str
    predicted_state_id: str
    actual_state_id: str
    error_id: str
    outcome: str
    evidence_run_id: str | None = None
    evidence_event_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SelfSupervisionSample:
    sample_id: str
    task: str
    context_state_id: str
    action_id: str
    target_state_id: str
    prediction_id: str
    loss: FeatureMap
    accepted_for_training: bool = False


@dataclass(frozen=True, slots=True)
class LatentSimulation:
    state: LatentState
    prediction: LatentPrediction
    actual_state: LatentState
    error: LatentPredictionError
    transition: LatentTransition
    self_supervision_sample: SelfSupervisionSample
