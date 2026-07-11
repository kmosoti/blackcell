from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from blackcell.kernel import JsonScalar


@dataclass(frozen=True, slots=True)
class OperationalStateScope:
    """The bounded evidence partition represented by an operational state.

    ``stream_id`` is optional only for an empty, compatibility projection where
    there is no observation stream to infer.  A state containing evidence is
    always bound to both a domain and one source stream.
    """

    domain: str
    stream_id: str | None

    def __post_init__(self) -> None:
        if not self.domain.strip():
            raise ValueError("domain must not be empty")
        if self.stream_id is not None and not self.stream_id.strip():
            raise ValueError("stream_id must not be blank")

    @property
    def bound(self) -> bool:
        return self.stream_id is not None


@dataclass(frozen=True, slots=True)
class BeliefClaim:
    claim_id: str
    subject: str
    predicate: str
    value: JsonScalar
    confidence: float
    effective_at: datetime
    recorded_at: datetime
    source_event_id: str
    source: str
    actor: str
    correlation_id: str
    domain: str
    stream_id: str
    stream_sequence: int
    global_position: int
    correction_id: str | None = None
    supersedes_claim_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "claim_id",
            "subject",
            "predicate",
            "source_event_id",
            "source",
            "actor",
            "correlation_id",
            "domain",
            "stream_id",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        _require_aware(self.effective_at, "effective_at")
        _require_aware(self.recorded_at, "recorded_at")
        if self.stream_sequence < 1 or self.global_position < 1:
            raise ValueError("claim ledger positions must be positive")
        if self.correction_id is None:
            if self.supersedes_claim_ids:
                raise ValueError("ordinary claims cannot supersede other claims")
        else:
            if not self.correction_id.strip():
                raise ValueError("correction_id must not be blank")
            if not self.supersedes_claim_ids:
                raise ValueError("correction replacements must identify superseded claims")
        if len(self.supersedes_claim_ids) != len(set(self.supersedes_claim_ids)):
            raise ValueError("superseded claim ids must be unique")

    @property
    def key(self) -> tuple[str, str]:
        return (self.subject, self.predicate)


@dataclass(frozen=True, slots=True)
class BeliefConflict:
    subject: str
    predicate: str
    source_event_ids: tuple[str, ...]
    claim_ids: tuple[str, ...]
    values: tuple[JsonScalar, ...]

    @property
    def key(self) -> tuple[str, str]:
        return (self.subject, self.predicate)


@dataclass(frozen=True, slots=True)
class BeliefCorrection:
    correction_id: str
    supersedes_claim_ids: tuple[str, ...]
    replacement_claim_id: str
    reason: str
    effective_at: datetime
    recorded_at: datetime
    source_event_id: str
    source: str
    actor: str
    correlation_id: str
    domain: str
    stream_id: str
    stream_sequence: int
    global_position: int

    def __post_init__(self) -> None:
        for name in (
            "correction_id",
            "replacement_claim_id",
            "reason",
            "source_event_id",
            "source",
            "actor",
            "correlation_id",
            "domain",
            "stream_id",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if not self.supersedes_claim_ids:
            raise ValueError("a correction must supersede at least one claim")
        if any(not claim_id.strip() for claim_id in self.supersedes_claim_ids):
            raise ValueError("superseded claim ids must not be blank")
        if len(self.supersedes_claim_ids) != len(set(self.supersedes_claim_ids)):
            raise ValueError("superseded claim ids must be unique")
        _require_aware(self.effective_at, "effective_at")
        _require_aware(self.recorded_at, "recorded_at")
        if self.stream_sequence < 1 or self.global_position < 1:
            raise ValueError("correction ledger positions must be positive")


@dataclass(frozen=True, slots=True)
class OperationalBeliefState:
    scope: OperationalStateScope
    claims: tuple[BeliefClaim, ...]
    conflicts: tuple[BeliefConflict, ...]
    cutoff_global_position: int
    last_source_stream_sequence: int
    superseded_claims: tuple[BeliefClaim, ...] = ()
    applied_corrections: tuple[BeliefCorrection, ...] = ()
    effective_time_cutoff: datetime | None = None

    def __post_init__(self) -> None:
        if self.cutoff_global_position < 0:
            raise ValueError("cutoff_global_position must be non-negative")
        if self.last_source_stream_sequence < 0:
            raise ValueError("last_source_stream_sequence must be non-negative")
        if self.effective_time_cutoff is not None:
            _require_aware(self.effective_time_cutoff, "effective_time_cutoff")
        if not self.scope.bound and (
            self.claims
            or self.conflicts
            or self.superseded_claims
            or self.applied_corrections
            or self.last_source_stream_sequence
        ):
            raise ValueError("an unbound operational state must be empty")
        if self.scope.stream_id is not None:
            outside_scope = tuple(
                claim
                for claim in (*self.claims, *self.superseded_claims)
                if claim.domain != self.scope.domain or claim.stream_id != self.scope.stream_id
            )
            outside_corrections = tuple(
                correction
                for correction in self.applied_corrections
                if correction.domain != self.scope.domain
                or correction.stream_id != self.scope.stream_id
            )
            if outside_scope or outside_corrections:
                raise ValueError("operational state claims must belong to its declared scope")
        all_claims = (*self.claims, *self.superseded_claims)
        if any(claim.global_position > self.cutoff_global_position for claim in all_claims):
            raise ValueError("operational state claims cannot exceed its ledger cutoff")
        if any(
            correction.global_position > self.cutoff_global_position
            for correction in self.applied_corrections
        ):
            raise ValueError("operational state corrections cannot exceed its ledger cutoff")
        if any(claim.stream_sequence > self.last_source_stream_sequence for claim in all_claims):
            raise ValueError("operational state claims cannot exceed its source stream position")
        if any(
            correction.stream_sequence > self.last_source_stream_sequence
            for correction in self.applied_corrections
        ):
            raise ValueError("operational state corrections cannot exceed its stream position")
        claim_ids = tuple(claim.claim_id for claim in all_claims)
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("current and superseded claim ids must be disjoint")
        correction_ids = tuple(item.correction_id for item in self.applied_corrections)
        if len(correction_ids) != len(set(correction_ids)):
            raise ValueError("applied correction ids must be unique")

    @property
    def last_global_position(self) -> int:
        """Compatibility name for the complete ledger cutoff represented by this state."""

        return self.cutoff_global_position

    def claims_for(self, subject: str, predicate: str) -> tuple[BeliefClaim, ...]:
        return tuple(claim for claim in self.claims if claim.key == (subject, predicate))


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
