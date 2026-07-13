from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from blackcell.features.predict_transition.models import (
    PredictionTarget,
    TransitionPrediction,
)
from blackcell.features.predict_transition.ports import PredictionStateLike


@dataclass(frozen=True, slots=True)
class PredictTransition:
    source_state: PredictionStateLike
    source_snapshot_digest: str
    action_digest: str
    action_kind: str
    targets: tuple[PredictionTarget, ...]
    generated_at: datetime
    horizon_seconds: int
    confidence_cap: float = 0.75

    def __post_init__(self) -> None:
        if self.source_state.scope.stream_id is None:
            raise ValueError("prediction source state must have a bound stream scope")
        if (
            not self.source_snapshot_digest.strip()
            or not self.action_digest.strip()
            or not self.action_kind.strip()
        ):
            raise ValueError("prediction action identity must not be empty")
        targets = tuple(sorted(self.targets))
        if not targets or len(targets) != len(set(targets)):
            raise ValueError("prediction targets must be non-empty and unique")
        generated_at = _aware(self.generated_at, "generated_at")
        if isinstance(self.horizon_seconds, bool) or self.horizon_seconds < 1:
            raise ValueError("prediction horizon must be a positive integer")
        if (
            isinstance(self.confidence_cap, bool)
            or not isinstance(self.confidence_cap, int | float)
            or not math.isfinite(self.confidence_cap)
            or not 0.0 < self.confidence_cap <= 1.0
        ):
            raise ValueError("prediction confidence cap must be finite and positive")
        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "generated_at", generated_at)
        object.__setattr__(self, "confidence_cap", float(self.confidence_cap))


@dataclass(frozen=True, slots=True)
class ScoreTransitionPrediction:
    prediction: TransitionPrediction
    actual_state: PredictionStateLike
    actual_snapshot_digest: str
    scored_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.prediction, TransitionPrediction):
            raise TypeError("prediction score requires a TransitionPrediction")
        if not self.actual_snapshot_digest.strip():
            raise ValueError("prediction outcome snapshot digest must not be empty")
        object.__setattr__(self, "scored_at", _aware(self.scored_at, "scored_at"))


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


__all__ = ["PredictTransition", "ScoreTransitionPrediction"]
