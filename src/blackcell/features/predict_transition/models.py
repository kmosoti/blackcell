from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from blackcell.kernel import JsonScalar
from blackcell.kernel._json import canonical_json, freeze_json, json_digest

PREDICTION_SCHEMA_VERSION = "transition-prediction/v1"
PREDICTION_SCORE_SCHEMA_VERSION = "transition-prediction-score/v1"
DETERMINISTIC_PREDICTOR_VERSION = "state-persistence/v1"


class PredictionDisposition(StrEnum):
    PREDICTED = "predicted"
    UNKNOWN = "unknown"


class PredictionFindingOutcome(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    ACTUAL_MISSING = "actual-missing"
    ACTUAL_CONFLICT = "actual-conflict"
    PREDICTION_UNKNOWN = "prediction-unknown"


@dataclass(frozen=True, slots=True, order=True)
class PredictionTarget:
    subject: str
    predicate: str

    def __post_init__(self) -> None:
        if not self.subject.strip() or not self.predicate.strip():
            raise ValueError("prediction target subject and predicate must not be empty")

    @property
    def key(self) -> tuple[str, str]:
        return (self.subject, self.predicate)


@dataclass(frozen=True, slots=True)
class PredictedFact:
    target: PredictionTarget
    disposition: PredictionDisposition
    value: JsonScalar
    confidence: float
    assumptions: tuple[str, ...]
    source_claim_ids: tuple[str, ...]
    source_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.target, PredictionTarget):
            raise TypeError("predicted fact target must be a PredictionTarget")
        if not isinstance(self.disposition, PredictionDisposition):
            raise TypeError("prediction disposition must be recognized")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, int | float):
            raise TypeError("prediction confidence must be numeric")
        confidence = float(self.confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("prediction confidence must be finite and between zero and one")
        value = freeze_json(self.value, path="$.prediction.value")
        if isinstance(value, tuple):
            raise TypeError("prediction value must be a JSON scalar")
        assumptions = tuple(sorted(set(self.assumptions)))
        claim_ids = tuple(sorted(set(self.source_claim_ids)))
        event_ids = tuple(sorted(set(self.source_event_ids)))
        if not assumptions or any(not item.strip() for item in assumptions):
            raise ValueError("prediction assumptions must be non-empty text")
        if any(not item.strip() for item in (*claim_ids, *event_ids)):
            raise ValueError("prediction provenance identities must be non-empty text")
        if self.disposition is PredictionDisposition.PREDICTED:
            if not claim_ids or not event_ids or confidence <= 0.0:
                raise ValueError("a predicted fact requires provenance and positive confidence")
        elif value is not None or confidence != 0.0:
            raise ValueError("an unknown prediction requires null value and zero confidence")
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "assumptions", assumptions)
        object.__setattr__(self, "source_claim_ids", claim_ids)
        object.__setattr__(self, "source_event_ids", event_ids)


@dataclass(frozen=True, slots=True)
class TransitionPrediction:
    source_snapshot_digest: str
    source_domain: str
    source_stream_id: str
    source_cutoff_global_position: int
    source_stream_sequence: int
    source_effective_time: datetime | None
    action_digest: str
    action_kind: str
    generated_at: datetime
    horizon_seconds: int
    facts: tuple[PredictedFact, ...]
    predictor_version: str = DETERMINISTIC_PREDICTOR_VERSION
    schema_version: str = PREDICTION_SCHEMA_VERSION
    prediction_id: str = field(init=False)

    def __post_init__(self) -> None:
        _digest(self.source_snapshot_digest, "source_snapshot_digest")
        _digest(self.action_digest, "action_digest")
        for name in ("source_domain", "source_stream_id", "action_kind", "predictor_version"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.schema_version != PREDICTION_SCHEMA_VERSION:
            raise ValueError("unsupported transition prediction schema")
        if self.source_cutoff_global_position < 0 or self.source_stream_sequence < 0:
            raise ValueError("prediction source positions must be non-negative")
        if isinstance(self.horizon_seconds, bool) or self.horizon_seconds < 1:
            raise ValueError("prediction horizon must be a positive integer")
        generated_at = _timestamp(self.generated_at, "generated_at")
        source_time = (
            None
            if self.source_effective_time is None
            else _timestamp(self.source_effective_time, "source_effective_time")
        )
        if source_time is not None and generated_at < source_time:
            raise ValueError("prediction cannot precede its source state")
        facts = tuple(sorted(self.facts, key=lambda item: item.target))
        targets = tuple(item.target for item in facts)
        if not facts or len(targets) != len(set(targets)):
            raise ValueError("prediction facts must be non-empty with unique targets")
        object.__setattr__(self, "generated_at", generated_at)
        object.__setattr__(self, "source_effective_time", source_time)
        object.__setattr__(self, "facts", facts)
        object.__setattr__(self, "prediction_id", json_digest(prediction_payload(self)))

    @property
    def scope(self) -> tuple[str, str]:
        return (self.source_domain, self.source_stream_id)


@dataclass(frozen=True, slots=True)
class PredictionFinding:
    target: PredictionTarget
    outcome: PredictionFindingOutcome
    predicted_value: JsonScalar
    actual_values: tuple[JsonScalar, ...]
    predicted_confidence: float
    actual_claim_ids: tuple[str, ...]
    actual_source_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, PredictionFindingOutcome):
            raise TypeError("prediction finding outcome must be recognized")
        if not 0.0 <= self.predicted_confidence <= 1.0:
            raise ValueError("finding confidence must be between zero and one")
        values = tuple(
            value
            for _, value in sorted(
                {
                    canonical_json({"value": item}): freeze_json(
                        item,
                        path="$.finding.actual_values",
                    )
                    for item in self.actual_values
                }.items()
            )
        )
        if any(isinstance(item, tuple) for item in values):
            raise TypeError("finding actual values must be JSON scalars")
        object.__setattr__(self, "actual_values", values)
        object.__setattr__(self, "actual_claim_ids", tuple(sorted(set(self.actual_claim_ids))))
        object.__setattr__(
            self,
            "actual_source_event_ids",
            tuple(sorted(set(self.actual_source_event_ids))),
        )

    @property
    def scored(self) -> bool:
        return self.outcome in {
            PredictionFindingOutcome.MATCH,
            PredictionFindingOutcome.MISMATCH,
        }


@dataclass(frozen=True, slots=True)
class TransitionPredictionScore:
    prediction_id: str
    actual_snapshot_digest: str
    actual_cutoff_global_position: int
    actual_stream_sequence: int
    actual_effective_time: datetime
    scored_at: datetime
    findings: tuple[PredictionFinding, ...]
    matched_count: int
    scored_count: int
    exact_match_rate: float | None
    brier_score: float | None
    schema_version: str = PREDICTION_SCORE_SCHEMA_VERSION
    score_id: str = field(init=False)

    def __post_init__(self) -> None:
        _digest(self.prediction_id, "prediction_id")
        _digest(self.actual_snapshot_digest, "actual_snapshot_digest")
        if self.schema_version != PREDICTION_SCORE_SCHEMA_VERSION:
            raise ValueError("unsupported transition prediction score schema")
        if self.actual_cutoff_global_position < 1 or self.actual_stream_sequence < 1:
            raise ValueError("prediction outcome positions must be positive")
        actual_time = _timestamp(self.actual_effective_time, "actual_effective_time")
        scored_at = _timestamp(self.scored_at, "scored_at")
        if scored_at < actual_time:
            raise ValueError("prediction score cannot precede its outcome")
        findings = tuple(sorted(self.findings, key=lambda item: item.target))
        targets = tuple(item.target for item in findings)
        if not findings or len(targets) != len(set(targets)):
            raise ValueError("prediction score findings require unique targets")
        expected_scored = sum(item.scored for item in findings)
        expected_matched = sum(item.outcome is PredictionFindingOutcome.MATCH for item in findings)
        if self.scored_count != expected_scored or self.matched_count != expected_matched:
            raise ValueError("prediction score counts differ from its findings")
        expected_rate = None if expected_scored == 0 else expected_matched / expected_scored
        if self.exact_match_rate != expected_rate:
            raise ValueError("exact match rate differs from prediction findings")
        if (self.brier_score is None) != (expected_scored == 0):
            raise ValueError("Brier score presence differs from scored findings")
        if self.brier_score is not None and not 0.0 <= self.brier_score <= 1.0:
            raise ValueError("Brier score must be between zero and one")
        object.__setattr__(self, "actual_effective_time", actual_time)
        object.__setattr__(self, "scored_at", scored_at)
        object.__setattr__(self, "findings", findings)
        object.__setattr__(self, "score_id", json_digest(prediction_score_payload(self)))


def prediction_payload(prediction: TransitionPrediction) -> dict[str, object]:
    return {
        "schema_version": prediction.schema_version,
        "source_snapshot_digest": prediction.source_snapshot_digest,
        "source_domain": prediction.source_domain,
        "source_stream_id": prediction.source_stream_id,
        "source_cutoff_global_position": prediction.source_cutoff_global_position,
        "source_stream_sequence": prediction.source_stream_sequence,
        "source_effective_time": (
            None
            if prediction.source_effective_time is None
            else prediction.source_effective_time.isoformat()
        ),
        "action_digest": prediction.action_digest,
        "action_kind": prediction.action_kind,
        "generated_at": prediction.generated_at.isoformat(),
        "horizon_seconds": prediction.horizon_seconds,
        "predictor_version": prediction.predictor_version,
        "advisory_only": True,
        "facts": [
            {
                "subject": item.target.subject,
                "predicate": item.target.predicate,
                "disposition": item.disposition.value,
                "value": item.value,
                "confidence": item.confidence,
                "assumptions": list(item.assumptions),
                "source_claim_ids": list(item.source_claim_ids),
                "source_event_ids": list(item.source_event_ids),
            }
            for item in prediction.facts
        ],
    }


def prediction_score_payload(score: TransitionPredictionScore) -> dict[str, object]:
    return {
        "schema_version": score.schema_version,
        "prediction_id": score.prediction_id,
        "actual_snapshot_digest": score.actual_snapshot_digest,
        "actual_cutoff_global_position": score.actual_cutoff_global_position,
        "actual_stream_sequence": score.actual_stream_sequence,
        "actual_effective_time": score.actual_effective_time.isoformat(),
        "scored_at": score.scored_at.isoformat(),
        "matched_count": score.matched_count,
        "scored_count": score.scored_count,
        "exact_match_rate": score.exact_match_rate,
        "brier_score": score.brier_score,
        "findings": [
            {
                "subject": item.target.subject,
                "predicate": item.target.predicate,
                "outcome": item.outcome.value,
                "predicted_value": item.predicted_value,
                "actual_values": list(item.actual_values),
                "predicted_confidence": item.predicted_confidence,
                "actual_claim_ids": list(item.actual_claim_ids),
                "actual_source_event_ids": list(item.actual_source_event_ids),
            }
            for item in score.findings
        ],
    }


def _timestamp(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _digest(value: str, label: str) -> None:
    hexadecimal = value.removeprefix("sha256:")
    if not value.startswith("sha256:") or len(hexadecimal) != 64:
        raise ValueError(f"{label} must be a SHA-256 digest")
    try:
        int(hexadecimal, 16)
    except ValueError as error:
        raise ValueError(f"{label} must be a SHA-256 digest") from error


__all__ = [
    "DETERMINISTIC_PREDICTOR_VERSION",
    "PREDICTION_SCHEMA_VERSION",
    "PREDICTION_SCORE_SCHEMA_VERSION",
    "PredictedFact",
    "PredictionDisposition",
    "PredictionFinding",
    "PredictionFindingOutcome",
    "PredictionTarget",
    "TransitionPrediction",
    "TransitionPredictionScore",
    "prediction_payload",
    "prediction_score_payload",
]
