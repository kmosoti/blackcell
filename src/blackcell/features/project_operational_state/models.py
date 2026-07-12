from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import cast

from blackcell.kernel import JsonScalar


class EpistemicStatus(StrEnum):
    OBSERVED = "observed"
    UNKNOWN = "unknown"


class UnknownReason(StrEnum):
    EXPIRED = "expired"


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
    expires_at: datetime | None = None
    epistemic_status: EpistemicStatus = EpistemicStatus.OBSERVED
    unknown_reason: UnknownReason | None = None

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
        if isinstance(self.confidence, bool) or not math.isfinite(self.confidence):
            raise ValueError("confidence must be a finite number")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between zero and one")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (self.stream_sequence, self.global_position)
        ):
            raise ValueError("claim ledger positions must be positive")
        if self.expires_at is not None:
            _require_aware(self.expires_at, "expires_at")
            if self.expires_at < self.effective_at:
                raise ValueError("expires_at cannot precede effective_at")
        if not isinstance(self.epistemic_status, EpistemicStatus):
            raise TypeError("epistemic_status must be recognized")
        if self.unknown_reason is not None and not isinstance(self.unknown_reason, UnknownReason):
            raise TypeError("unknown_reason must be recognized")
        if self.epistemic_status is EpistemicStatus.OBSERVED:
            if self.unknown_reason is not None:
                raise ValueError("observed claims cannot have an unknown reason")
        else:
            if self.value is not None or self.confidence != 0.0:
                raise ValueError("unknown claims require a null value and zero confidence")
            if self.unknown_reason is not UnknownReason.EXPIRED:
                raise ValueError("unknown claims require a supported reason")
            if self.expires_at is None:
                raise ValueError("expired unknown claims require expires_at")
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
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (self.stream_sequence, self.global_position)
        ):
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
    expired_claims: tuple[BeliefClaim, ...] = ()
    correction_replacement_claims: tuple[BeliefClaim, ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.cutoff_global_position, bool)
            or not isinstance(self.cutoff_global_position, int)
            or self.cutoff_global_position < 0
        ):
            raise ValueError("cutoff_global_position must be non-negative")
        if (
            isinstance(self.last_source_stream_sequence, bool)
            or not isinstance(self.last_source_stream_sequence, int)
            or self.last_source_stream_sequence < 0
        ):
            raise ValueError("last_source_stream_sequence must be non-negative")
        if self.last_source_stream_sequence > self.cutoff_global_position:
            raise ValueError("source stream position cannot exceed the ledger cutoff")
        if self.effective_time_cutoff is not None:
            _require_aware(self.effective_time_cutoff, "effective_time_cutoff")
        if not self.scope.bound and (
            self.claims
            or self.conflicts
            or self.superseded_claims
            or self.applied_corrections
            or self.expired_claims
            or self.correction_replacement_claims
            or self.last_source_stream_sequence
        ):
            raise ValueError("an unbound operational state must be empty")
        if self.scope.stream_id is not None:
            outside_scope = tuple(
                claim
                for claim in (
                    *self.claims,
                    *self.superseded_claims,
                    *self.expired_claims,
                    *self.correction_replacement_claims,
                )
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
        scoped_claims = (
            *self.claims,
            *self.superseded_claims,
            *self.expired_claims,
            *self.correction_replacement_claims,
        )
        if any(claim.global_position > self.cutoff_global_position for claim in scoped_claims):
            raise ValueError("operational state claims cannot exceed its ledger cutoff")
        if any(
            correction.global_position > self.cutoff_global_position
            for correction in self.applied_corrections
        ):
            raise ValueError("operational state corrections cannot exceed its ledger cutoff")
        if any(claim.stream_sequence > self.last_source_stream_sequence for claim in scoped_claims):
            raise ValueError("operational state claims cannot exceed its source stream position")
        if any(
            correction.stream_sequence > self.last_source_stream_sequence
            for correction in self.applied_corrections
        ):
            raise ValueError("operational state corrections cannot exceed its stream position")
        claim_ids = tuple(claim.claim_id for claim in (*self.claims, *self.superseded_claims))
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("current and superseded claim ids must be disjoint")
        correction_ids = tuple(item.correction_id for item in self.applied_corrections)
        if len(correction_ids) != len(set(correction_ids)):
            raise ValueError("applied correction ids must be unique")
        unknown_by_id = {claim.claim_id: claim for claim in self.unknowns}
        expired_ids = tuple(claim.claim_id for claim in self.expired_claims)
        if len(expired_ids) != len(set(expired_ids)):
            raise ValueError("expired claim ids must be unique")
        if set(expired_ids) != set(unknown_by_id):
            raise ValueError("expired claims must exactly back current expired unknowns")
        expected_unknowns = {
            expired.claim_id: replace(
                expired,
                value=None,
                confidence=0.0,
                epistemic_status=EpistemicStatus.UNKNOWN,
                unknown_reason=UnknownReason.EXPIRED,
            )
            for expired in self.expired_claims
        }
        if unknown_by_id != expected_unknowns:
            raise ValueError("expired unknowns must preserve exact claim provenance")
        _validate_expiry_semantics(self)
        _validate_correction_lineage(self)
        _validate_event_fingerprints(self)

    @property
    def last_global_position(self) -> int:
        """Compatibility name for the complete ledger cutoff represented by this state."""

        return self.cutoff_global_position

    def claims_for(self, subject: str, predicate: str) -> tuple[BeliefClaim, ...]:
        return tuple(claim for claim in self.claims if claim.key == (subject, predicate))

    @property
    def unknowns(self) -> tuple[BeliefClaim, ...]:
        return tuple(
            claim for claim in self.claims if claim.epistemic_status is EpistemicStatus.UNKNOWN
        )

    @property
    def observed_claims(self) -> tuple[BeliefClaim, ...]:
        return tuple(
            claim for claim in self.claims if claim.epistemic_status is EpistemicStatus.OBSERVED
        )


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _validate_expiry_semantics(state: OperationalBeliefState) -> None:
    cutoff = state.effective_time_cutoff
    if any(
        claim.epistemic_status is not EpistemicStatus.OBSERVED or claim.unknown_reason is not None
        for claim in (*state.superseded_claims, *state.expired_claims)
    ):
        raise ValueError("superseded and expired origins must retain observed evidence")
    if {claim.claim_id for claim in state.superseded_claims} & {
        claim.claim_id for claim in state.expired_claims
    }:
        raise ValueError("superseded and expired claim identities must be disjoint")
    if state.expired_claims and cutoff is None:
        raise ValueError("expired unknowns require an explicit effective-time cutoff")
    if cutoff is None:
        return
    if any(
        claim.expires_at is not None and claim.expires_at <= cutoff
        for claim in state.observed_claims
    ):
        raise ValueError("current observed claims cannot be expired at the effective-time cutoff")
    if any(claim.expires_at is None or claim.expires_at > cutoff for claim in state.expired_claims):
        raise ValueError("expired claim origins must expire by the effective-time cutoff")
    evidence = (
        *state.observed_claims,
        *state.superseded_claims,
        *state.expired_claims,
        *state.correction_replacement_claims,
    )
    if any(claim.effective_at > cutoff for claim in evidence) or any(
        correction.effective_at > cutoff for correction in state.applied_corrections
    ):
        raise ValueError("operational-state evidence cannot exceed its effective-time cutoff")


def _validate_correction_lineage(state: OperationalBeliefState) -> None:
    corrections = state.applied_corrections
    correction_ids = tuple(correction.correction_id for correction in corrections)
    replacement_ids = tuple(correction.replacement_claim_id for correction in corrections)
    replacements = state.correction_replacement_claims
    if tuple(claim.claim_id for claim in replacements) != replacement_ids:
        raise ValueError(
            "correction replacement claims must exactly follow applied correction order"
        )
    if len(replacement_ids) != len(set(replacement_ids)):
        raise ValueError("correction replacement claim ids must be unique")
    if len(correction_ids) != len(set(correction_ids)):
        raise ValueError("applied correction ids must be unique")
    if any(
        claim.epistemic_status is not EpistemicStatus.OBSERVED or claim.unknown_reason is not None
        for claim in replacements
    ):
        raise ValueError("correction replacements must retain observed evidence")
    correction_event_identities = (
        tuple(correction.source_event_id for correction in corrections),
        tuple(correction.global_position for correction in corrections),
        tuple((correction.stream_id, correction.stream_sequence) for correction in corrections),
    )
    if any(len(values) != len(set(values)) for values in correction_event_identities):
        raise ValueError("each applied correction must belong to one distinct event")

    superseded_by_id = {claim.claim_id: claim for claim in state.superseded_claims}
    target_sequence = tuple(
        claim_id for correction in corrections for claim_id in correction.supersedes_claim_ids
    )
    if len(target_sequence) != len(set(target_sequence)):
        raise ValueError("applied corrections cannot supersede one claim more than once")
    if set(target_sequence) != set(superseded_by_id):
        raise ValueError("superseded claims must exactly match applied correction targets")

    replacements_by_id = {claim.claim_id: claim for claim in replacements}
    replacements_by_event = {
        _event_fingerprint(correction): replacement
        for correction, replacement in zip(corrections, replacements, strict=True)
    }
    represented_occurrences = (
        *state.observed_claims,
        *state.superseded_claims,
        *state.expired_claims,
        *state.correction_replacement_claims,
    )
    for claim in represented_occurrences:
        event_replacement = replacements_by_event.get(_event_fingerprint(claim))
        if event_replacement is not None and claim != event_replacement:
            raise ValueError("a correction event fingerprint cannot identify an ordinary claim")

    for correction, replacement in zip(corrections, replacements, strict=True):
        if (
            replacement.correction_id != correction.correction_id
            or replacement.supersedes_claim_ids != correction.supersedes_claim_ids
            or _event_fingerprint(replacement) != _event_fingerprint(correction)
        ):
            raise ValueError("correction replacement provenance does not match its correction")
        targets = tuple(superseded_by_id[claim_id] for claim_id in correction.supersedes_claim_ids)
        if any(
            target.key != replacement.key
            or target.global_position >= correction.global_position
            or target.effective_at > correction.effective_at
            for target in targets
        ):
            raise ValueError("correction targets do not match replacement lineage")

    represented_originals = (
        *state.observed_claims,
        *state.superseded_claims,
        *state.expired_claims,
    )
    for claim in represented_originals:
        replacement = replacements_by_id.get(claim.claim_id)
        if replacement is not None:
            if replacement != claim:
                raise ValueError("represented correction claims must match retained replacements")
            continue
        if claim.correction_id is not None:
            raise ValueError("represented correction claims must match retained replacements")


def _validate_event_fingerprints(state: OperationalBeliefState) -> None:
    evidence: tuple[BeliefClaim | BeliefCorrection, ...] = (
        *state.claims,
        *state.correction_replacement_claims,
        *state.superseded_claims,
        *state.expired_claims,
        *state.applied_corrections,
    )
    by_event_id: dict[str, tuple[object, ...]] = {}
    by_global_position: dict[int, tuple[object, ...]] = {}
    by_stream_position: dict[tuple[str, int], tuple[object, ...]] = {}
    for item in evidence:
        if item.stream_sequence > item.global_position:
            raise ValueError("source stream positions cannot exceed global ledger positions")
        fingerprint = _event_fingerprint(item)
        _bind_fingerprint(
            by_event_id,
            item.source_event_id,
            fingerprint,
            "source_event_id",
        )
        _bind_fingerprint(
            by_global_position,
            item.global_position,
            fingerprint,
            "global_position",
        )
        _bind_fingerprint(
            by_stream_position,
            (item.stream_id, item.stream_sequence),
            fingerprint,
            "source stream position",
        )

    previous_by_stream: dict[str, int] = {}
    for global_position in sorted(by_global_position):
        fingerprint = by_global_position[global_position]
        stream_id = cast(str, fingerprint[2])
        stream_sequence = cast(int, fingerprint[3])
        previous = previous_by_stream.get(stream_id, 0)
        if stream_sequence <= previous:
            raise ValueError("source stream and global ledger positions must be monotonic")
        previous_by_stream[stream_id] = stream_sequence


def _event_fingerprint(
    item: BeliefClaim | BeliefCorrection,
) -> tuple[object, ...]:
    return (
        item.source_event_id,
        item.global_position,
        item.stream_id,
        item.stream_sequence,
        item.effective_at,
        item.recorded_at,
        item.source,
        item.actor,
        item.correlation_id,
        item.domain,
    )


def _bind_fingerprint[KeyT](
    bindings: dict[KeyT, tuple[object, ...]],
    key: KeyT,
    fingerprint: tuple[object, ...],
    label: str,
) -> None:
    existing = bindings.setdefault(key, fingerprint)
    if existing != fingerprint:
        raise ValueError(f"{label} identifies inconsistent event metadata")
