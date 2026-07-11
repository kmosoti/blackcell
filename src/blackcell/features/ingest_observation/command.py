from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from blackcell.kernel import JsonScalar


@dataclass(frozen=True, slots=True)
class EvidencePointer:
    """Stable location or artifact identity supporting an observation."""

    locator: str | None = None
    artifact_id: str | None = None
    digest: str | None = None

    def __post_init__(self) -> None:
        values = (self.locator, self.artifact_id, self.digest)
        if not any(value is not None and value.strip() for value in values):
            raise ValueError("evidence requires a locator, artifact_id, or digest")
        if any(value is not None and not value.strip() for value in values):
            raise ValueError("evidence fields must not be blank")


@dataclass(frozen=True, slots=True)
class ObservedClaim:
    claim_id: str
    subject: str
    predicate: str
    value: JsonScalar
    confidence: float = 1.0
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("claim_id", "subject", "predicate"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if isinstance(self.confidence, bool) or not math.isfinite(self.confidence):
            raise ValueError("confidence must be a finite number")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between zero and one")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("claim values must be finite")
        if self.expires_at is not None and (
            self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None
        ):
            raise ValueError("expires_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ObservationInput:
    observation_id: str
    effective_at: datetime
    claims: tuple[ObservedClaim, ...]
    evidence: tuple[EvidencePointer, ...]
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if not self.observation_id.strip():
            raise ValueError("observation_id must not be empty")
        if self.effective_at.tzinfo is None or self.effective_at.utcoffset() is None:
            raise ValueError("effective_at must be timezone-aware")
        if not self.claims:
            raise ValueError("an observation requires at least one claim")
        if not self.evidence:
            raise ValueError("an observation requires explicit evidence")
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise ValueError("idempotency_key must not be blank")
        claim_ids = tuple(claim.claim_id for claim in self.claims)
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim ids must be unique within an observation")
        if any(
            claim.expires_at is not None and claim.expires_at < self.effective_at
            for claim in self.claims
        ):
            raise ValueError("claim expires_at cannot precede observation effective_at")


@dataclass(frozen=True, slots=True)
class CorrectionInput:
    """An append-only correction of claims already present in one state stream."""

    correction_id: str
    effective_at: datetime
    supersedes_claim_ids: tuple[str, ...]
    replacement: ObservedClaim
    reason: str
    evidence: tuple[EvidencePointer, ...]
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if not self.correction_id.strip():
            raise ValueError("correction_id must not be empty")
        if self.effective_at.tzinfo is None or self.effective_at.utcoffset() is None:
            raise ValueError("effective_at must be timezone-aware")
        if not self.supersedes_claim_ids:
            raise ValueError("a correction must supersede at least one claim")
        if any(not claim_id.strip() for claim_id in self.supersedes_claim_ids):
            raise ValueError("superseded claim ids must not be blank")
        if len(self.supersedes_claim_ids) != len(set(self.supersedes_claim_ids)):
            raise ValueError("superseded claim ids must be unique within a correction")
        if self.replacement.claim_id in self.supersedes_claim_ids:
            raise ValueError("a correction replacement requires a new claim id")
        if (
            self.replacement.expires_at is not None
            and self.replacement.expires_at < self.effective_at
        ):
            raise ValueError("replacement expires_at cannot precede correction effective_at")
        if not self.reason.strip():
            raise ValueError("a correction requires a reason")
        if not self.evidence:
            raise ValueError("a correction requires explicit evidence")
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise ValueError("idempotency_key must not be blank")


@dataclass(frozen=True, slots=True)
class IngestObservation:
    stream_id: str
    expected_sequence: int
    actor: str
    source: str
    correlation_id: str
    observations: tuple[ObservationInput, ...]
    causation_id: str | None = None
    domain: str = "repository"

    def __post_init__(self) -> None:
        for name in ("stream_id", "actor", "source", "correlation_id", "domain"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.causation_id is not None and not self.causation_id.strip():
            raise ValueError("causation_id must not be blank")
        if self.expected_sequence < 0:
            raise ValueError("expected_sequence must be non-negative")
        if not self.observations:
            raise ValueError("an ingestion command requires observations")
        keys = tuple(
            observation.idempotency_key or observation.observation_id
            for observation in self.observations
        )
        if len(keys) != len(set(keys)):
            raise ValueError("observation idempotency keys must be unique within a command")


@dataclass(frozen=True, slots=True)
class IngestCorrection:
    stream_id: str
    expected_sequence: int
    actor: str
    source: str
    correlation_id: str
    corrections: tuple[CorrectionInput, ...]
    causation_id: str | None = None
    domain: str = "repository"

    def __post_init__(self) -> None:
        for name in ("stream_id", "actor", "source", "correlation_id", "domain"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.causation_id is not None and not self.causation_id.strip():
            raise ValueError("causation_id must not be blank")
        if self.expected_sequence < 0:
            raise ValueError("expected_sequence must be non-negative")
        if not self.corrections:
            raise ValueError("a correction command requires corrections")
        correction_ids = tuple(correction.correction_id for correction in self.corrections)
        if len(correction_ids) != len(set(correction_ids)):
            raise ValueError("correction ids must be unique within a command")
        keys = tuple(
            correction.idempotency_key or correction.correction_id
            for correction in self.corrections
        )
        if len(keys) != len(set(keys)):
            raise ValueError("correction idempotency keys must be unique within a command")
