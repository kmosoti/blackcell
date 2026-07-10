from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

type Scalar = str | int | float | bool | None


class EpistemicStatus(StrEnum):
    """How a claim entered the state estimate, not how likely it is to be true."""

    OBSERVED = "observed"
    REPORTED = "reported"
    INFERRED = "inferred"
    ASSUMED = "assumed"
    UNKNOWN = "unknown"


class SourceReliability(StrEnum):
    """A qualitative source class; these labels are not calibrated probabilities."""

    AUTHORITATIVE = "authoritative"
    TRUSTED = "trusted"
    UNVERIFIED = "unverified"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    event_id: str
    source: str
    sequence: int | None = None
    artifact_id: str | None = None
    locator: str | None = None
    digest: str | None = None

    def __post_init__(self) -> None:
        _require_text("event_id", self.event_id)
        _require_text("source", self.source)
        if self.sequence is not None and self.sequence < 0:
            raise ValueError("evidence sequence must be non-negative")


@dataclass(frozen=True, slots=True)
class Claim:
    claim_id: str
    subject: str
    predicate: str
    value: Scalar
    epistemic_status: EpistemicStatus
    source_reliability: SourceReliability
    evidence: tuple[EvidenceRef, ...]
    observed_at: datetime
    effective_at: datetime
    expires_at: datetime | None = None
    conflict_group: str | None = None
    derivation_version: str = "repository-observation/v1"
    schema_version: str = "claim/v1"

    def __post_init__(self) -> None:
        for name in ("claim_id", "subject", "predicate", "derivation_version", "schema_version"):
            _require_text(name, getattr(self, name))
        _require_aware("observed_at", self.observed_at)
        _require_aware("effective_at", self.effective_at)
        if self.expires_at is not None:
            _require_aware("expires_at", self.expires_at)
            if self.expires_at < self.effective_at:
                raise ValueError("expires_at cannot precede effective_at")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("claim values cannot contain non-finite floats")

    @property
    def key(self) -> tuple[str, str]:
        return self.subject, self.predicate

    def is_expired(self, at: datetime) -> bool:
        _require_aware("at", at)
        return self.expires_at is not None and self.expires_at <= at


@dataclass(frozen=True, slots=True)
class ClaimBatch:
    claims: tuple[Claim, ...]


@dataclass(frozen=True, slots=True)
class ClaimCorrection:
    correction_id: str
    supersedes_claim_ids: tuple[str, ...]
    replacement: Claim
    effective_at: datetime
    reason: str
    evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        _require_text("correction_id", self.correction_id)
        _require_text("reason", self.reason)
        _require_aware("effective_at", self.effective_at)
        if not self.supersedes_claim_ids:
            raise ValueError("a correction must supersede at least one claim")
        if any(not claim_id for claim_id in self.supersedes_claim_ids):
            raise ValueError("superseded claim ids must be non-empty")


@dataclass(frozen=True, slots=True)
class ClaimConflict:
    conflict_group: str
    claims: tuple[Claim, ...]

    def __post_init__(self) -> None:
        _require_text("conflict_group", self.conflict_group)
        if len(self.claims) < 2:
            raise ValueError("a conflict must contain at least two claims")


@dataclass(frozen=True, slots=True)
class OperationalStateEstimate:
    repository_id: str
    as_of_sequence: int
    as_of_time: datetime
    claims: tuple[Claim, ...]
    superseded_claims: tuple[Claim, ...]
    conflicts: tuple[ClaimConflict, ...]
    unknowns: tuple[Claim, ...]
    applied_corrections: tuple[str, ...] = ()
    schema_version: str = "repository-state/v1"
    state_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text("repository_id", self.repository_id)
        _require_aware("as_of_time", self.as_of_time)
        if self.as_of_sequence < 0:
            raise ValueError("as_of_sequence must be non-negative")
        object.__setattr__(self, "state_id", _state_digest(self))

    def find_claims(self, subject: str, predicate: str) -> tuple[Claim, ...]:
        return tuple(
            claim
            for claim in self.claims
            if claim.subject == subject and claim.predicate == predicate
        )

    @property
    def current_claims(self) -> tuple[Claim, ...]:
        return tuple(claim for claim in self.claims if not claim.is_expired(self.as_of_time))


@dataclass(frozen=True, slots=True)
class TaskEvidence:
    task_id: str
    status: str
    blocked: bool
    source: str = "task-system"
    reliability: SourceReliability = SourceReliability.AUTHORITATIVE


@dataclass(frozen=True, slots=True)
class CheckEvidence:
    name: str
    status: str
    required: bool = True
    source: str = "check-runner"
    reliability: SourceReliability = SourceReliability.AUTHORITATIVE


@dataclass(frozen=True, slots=True)
class ToolEvidence:
    subject: str
    predicate: str
    status: str
    output_digest: str
    artifact_id: str
    source: str = "affordance-executor"
    reliability: SourceReliability = SourceReliability.AUTHORITATIVE

    def __post_init__(self) -> None:
        for name in (
            "subject",
            "predicate",
            "status",
            "output_digest",
            "artifact_id",
            "source",
        ):
            _require_text(name, getattr(self, name))


def claim_value_key(value: Scalar) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _state_digest(state: OperationalStateEstimate) -> str:
    payload = {
        "repository_id": state.repository_id,
        "as_of_sequence": state.as_of_sequence,
        "as_of_time": state.as_of_time.isoformat(),
        "claims": [claim.claim_id for claim in state.claims],
        "superseded": [claim.claim_id for claim in state.superseded_claims],
        "corrections": list(state.applied_corrections),
        "schema_version": state.schema_version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"state:{hashlib.sha256(encoded).hexdigest()}"


def _require_text(name: str, value: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} must be non-empty")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
