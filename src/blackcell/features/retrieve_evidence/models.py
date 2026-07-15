from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from blackcell.kernel import JsonScalar
from blackcell.kernel._json import json_digest


class EvidenceEpistemicStatus(StrEnum):
    OBSERVED = "observed"
    UNKNOWN = "unknown"


class EvidenceUnknownReason(StrEnum):
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class EvidenceKey:
    subject: str
    predicate: str

    def __post_init__(self) -> None:
        if not self.subject.strip() or not self.predicate.strip():
            raise ValueError("evidence keys require subject and predicate")


@dataclass(frozen=True, slots=True, order=True)
class EvidenceClaimIdentity:
    source_event_id: str
    claim_id: str

    def __post_init__(self) -> None:
        if not self.source_event_id.strip() or not self.claim_id.strip():
            raise ValueError("evidence claim identities must not be empty")


@dataclass(frozen=True, slots=True)
class EvidenceObjectiveMatch:
    """One objective-relevance result returned by a retrieval adapter."""

    identity: EvidenceClaimIdentity
    score: int
    reason: str

    def __post_init__(self) -> None:
        if self.score < 1:
            raise ValueError("objective-match scores must be positive")
        if not self.reason.strip():
            raise ValueError("objective-match reasons must not be empty")
        if self.reason in {"required", "conflict", "state-fallback"}:
            raise ValueError("objective matchers cannot claim feature-owned selection reasons")


@dataclass(frozen=True, slots=True, order=True)
class UnknownEvidenceSupport:
    source_event_id: str
    claim_id: str
    expires_at: datetime
    unknown_reason: EvidenceUnknownReason = EvidenceUnknownReason.EXPIRED

    def __post_init__(self) -> None:
        if not self.source_event_id.strip() or not self.claim_id.strip():
            raise ValueError("unknown evidence support identities must not be empty")
        _require_aware(self.expires_at, "unknown evidence support expires_at")
        if not isinstance(self.unknown_reason, EvidenceUnknownReason):
            raise TypeError("unknown evidence support reason must be recognized")


class RequiredEvidenceGapReason(StrEnum):
    ABSENT = "absent-required-key"
    UNKNOWN = "unknown-required-key"


@dataclass(frozen=True, slots=True)
class RequiredEvidenceGap:
    key: EvidenceKey
    source_packet_id: str
    state_domain: str
    state_stream_id: str | None
    state_global_position: int
    state_stream_position: int
    reason: RequiredEvidenceGapReason = RequiredEvidenceGapReason.ABSENT
    schema_version: str = "required-evidence-gap/v2"
    state_effective_time: datetime | None = None
    unknown_supports: tuple[UnknownEvidenceSupport, ...] = ()
    gap_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.source_packet_id.strip() or not self.state_domain.strip():
            raise ValueError("gap packet and state identities must not be empty")
        if self.state_stream_id is not None and not self.state_stream_id.strip():
            raise ValueError("state_stream_id must not be blank")
        if self.state_global_position < 0 or self.state_stream_position < 0:
            raise ValueError("state positions must be non-negative")
        if self.state_stream_id is None and self.state_stream_position:
            raise ValueError("an unbound gap scope cannot have a stream position")
        if self.state_effective_time is not None:
            _require_aware(self.state_effective_time, "state_effective_time")
        if tuple(sorted(set(self.unknown_supports))) != self.unknown_supports:
            raise ValueError("unknown evidence supports must be sorted and unique")
        if self.reason is RequiredEvidenceGapReason.ABSENT and self.unknown_supports:
            raise ValueError("absent required-evidence gaps cannot have unknown support")
        if self.reason is RequiredEvidenceGapReason.UNKNOWN and not self.unknown_supports:
            raise ValueError("unknown required-evidence gaps require supporting claims")
        if self.schema_version not in {
            "required-evidence-gap/v2",
            "required-evidence-gap/v3",
        }:
            raise ValueError("required-evidence gap schema is unsupported")
        if self.schema_version == "required-evidence-gap/v2" and (
            self.state_effective_time is not None
            or self.unknown_supports
            or self.reason is not RequiredEvidenceGapReason.ABSENT
        ):
            raise ValueError("required-evidence-gap/v2 cannot contain epistemic extensions")
        if self.schema_version == "required-evidence-gap/v3" and (
            self.state_effective_time is None
            and not self.unknown_supports
            and self.reason is RequiredEvidenceGapReason.ABSENT
        ):
            raise ValueError("required-evidence-gap/v3 requires epistemic extensions")
        object.__setattr__(self, "gap_id", json_digest(_gap_payload(self)))


class MissingRequiredEvidenceError(ValueError):
    def __init__(self, gaps: tuple[RequiredEvidenceGap, ...]) -> None:
        if not gaps:
            raise ValueError("missing required evidence errors require at least one gap")
        self.gaps = gaps
        self.missing_keys = tuple(gap.key for gap in gaps)
        rendered = ", ".join(
            f"{gap.key.subject}/{gap.key.predicate} ({gap.reason})" for gap in gaps
        )
        super().__init__(f"required evidence is missing for: {rendered}")


@dataclass(frozen=True, slots=True)
class EvidenceCandidate:
    claim_id: str
    subject: str
    predicate: str
    value: JsonScalar
    confidence: float
    effective_at: datetime
    freshness_seconds: int
    stale: bool
    source_event_id: str
    domain: str
    stream_id: str
    stream_sequence: int
    global_position: int
    score: int
    reasons: tuple[str, ...]
    conflicted: bool
    epistemic_status: EvidenceEpistemicStatus = EvidenceEpistemicStatus.OBSERVED
    unknown_reason: EvidenceUnknownReason | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_claim_semantics(
            value=self.value,
            confidence=self.confidence,
            effective_at=self.effective_at,
            stale=self.stale,
            epistemic_status=self.epistemic_status,
            unknown_reason=self.unknown_reason,
            expires_at=self.expires_at,
        )
        if self.epistemic_status is not EvidenceEpistemicStatus.OBSERVED:
            raise ValueError("unknown claims cannot be positive evidence candidates")


class EvidenceOmissionReason(StrEnum):
    IRRELEVANT = "irrelevant"
    RESULT_LIMIT = "retrieval-result-cap"
    UNKNOWN = "expired-unknown"


@dataclass(frozen=True, slots=True)
class EvidenceOmission:
    claim_id: str
    subject: str
    predicate: str
    value: JsonScalar
    confidence: float
    effective_at: datetime
    freshness_seconds: int
    stale: bool
    source_event_id: str
    domain: str
    stream_id: str
    stream_sequence: int
    global_position: int
    score: int
    reasons: tuple[str, ...]
    conflicted: bool
    reason: EvidenceOmissionReason
    schema_version: str = "evidence-omission/v2"
    epistemic_status: EvidenceEpistemicStatus = EvidenceEpistemicStatus.OBSERVED
    unknown_reason: EvidenceUnknownReason | None = None
    expires_at: datetime | None = None
    omission_id: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_claim_semantics(
            value=self.value,
            confidence=self.confidence,
            effective_at=self.effective_at,
            stale=self.stale,
            epistemic_status=self.epistemic_status,
            unknown_reason=self.unknown_reason,
            expires_at=self.expires_at,
        )
        if "required" in self.reasons:
            raise ValueError("required evidence cannot be recorded as omitted")
        if self.reason is EvidenceOmissionReason.IRRELEVANT and self.reasons:
            raise ValueError("irrelevant omissions cannot have selection reasons")
        if self.reason is EvidenceOmissionReason.RESULT_LIMIT and not self.reasons:
            raise ValueError("result-limit omissions require selection reasons")
        if self.reason is EvidenceOmissionReason.UNKNOWN:
            if self.reasons or self.epistemic_status is not EvidenceEpistemicStatus.UNKNOWN:
                raise ValueError("expired-unknown omissions require unknown claim semantics")
        elif self.epistemic_status is not EvidenceEpistemicStatus.OBSERVED:
            raise ValueError("unknown claims require the expired-unknown omission reason")
        if self.schema_version not in {"evidence-omission/v2", "evidence-omission/v3"}:
            raise ValueError("evidence omission schema is unsupported")
        if self.schema_version == "evidence-omission/v2" and _has_epistemic_extensions(self):
            raise ValueError("evidence-omission/v2 cannot contain epistemic extensions")
        if self.schema_version == "evidence-omission/v3" and not _has_epistemic_extensions(self):
            raise ValueError("evidence-omission/v3 requires epistemic extensions")
        object.__setattr__(self, "omission_id", json_digest(_omission_payload(self)))


@dataclass(frozen=True, slots=True)
class EvidenceSelection:
    objective: str
    source_packet_id: str
    source_packet_purpose: str
    state_domain: str
    state_stream_id: str | None
    state_global_position: int
    state_stream_position: int
    source_claim_identities: tuple[EvidenceClaimIdentity, ...]
    candidates: tuple[EvidenceCandidate, ...]
    omissions: tuple[EvidenceOmission, ...]
    required_keys: tuple[EvidenceKey, ...] = ()
    required_match_count: int = 0
    schema_version: str = "evidence-selection/v4"
    state_effective_time: datetime | None = None
    selection_id: str = field(init=False)

    @property
    def omitted_count(self) -> int:
        """Compatibility count derived from inspectable omission records."""

        return len(self.omissions)

    @property
    def state_position(self) -> int:
        """Compatibility name for the complete ledger cutoff."""

        return self.state_global_position

    def __post_init__(self) -> None:
        if not self.source_packet_purpose.strip() or not self.state_domain.strip():
            raise ValueError("packet purpose and state domain must not be empty")
        if self.state_effective_time is not None:
            _require_aware(self.state_effective_time, "state_effective_time")
        if self.schema_version not in {"evidence-selection/v4", "evidence-selection/v5"}:
            raise ValueError("evidence selection schema is unsupported")
        if self.state_stream_id is not None and not self.state_stream_id.strip():
            raise ValueError("state_stream_id must not be blank")
        if self.state_global_position < 0 or self.state_stream_position < 0:
            raise ValueError("state positions must be non-negative")
        dispositions = (*self.candidates, *self.omissions)
        if self.state_stream_id is None and (dispositions or self.state_stream_position):
            raise ValueError("an unbound evidence selection must not contain evidence")
        if any(
            item.domain != self.state_domain or item.stream_id != self.state_stream_id
            for item in dispositions
        ):
            raise ValueError("evidence dispositions must belong to the selection state scope")
        if any(item.global_position > self.state_global_position for item in dispositions):
            raise ValueError("evidence dispositions cannot exceed the selection ledger cutoff")
        if any(item.stream_sequence > self.state_stream_position for item in dispositions):
            raise ValueError("evidence dispositions cannot exceed the selection stream position")
        if tuple(sorted(set(self.source_claim_identities))) != self.source_claim_identities:
            raise ValueError("source claim identities must be sorted and unique")
        candidate_identities = {
            EvidenceClaimIdentity(item.source_event_id, item.claim_id) for item in self.candidates
        }
        omission_identities = {
            EvidenceClaimIdentity(item.source_event_id, item.claim_id) for item in self.omissions
        }
        if len(candidate_identities) != len(self.candidates):
            raise ValueError("selected evidence identities must be unique")
        if len(omission_identities) != len(self.omissions):
            raise ValueError("omitted evidence identities must be unique")
        if candidate_identities & omission_identities:
            raise ValueError("selected and omitted evidence identities must be disjoint")
        if candidate_identities | omission_identities != set(self.source_claim_identities):
            raise ValueError("evidence dispositions must exactly cover source packet claims")
        has_epistemic_extensions = self.state_effective_time is not None or any(
            _has_epistemic_extensions(item) for item in dispositions
        )
        if self.schema_version == "evidence-selection/v4" and has_epistemic_extensions:
            raise ValueError("evidence-selection/v4 cannot contain epistemic extensions")
        if self.schema_version == "evidence-selection/v5" and not has_epistemic_extensions:
            raise ValueError("evidence-selection/v5 requires epistemic extensions")
        required = {(key.subject, key.predicate) for key in self.required_keys}
        matching_observed_dispositions = tuple(
            item
            for item in dispositions
            if (item.subject, item.predicate) in required
            and item.epistemic_status is EvidenceEpistemicStatus.OBSERVED
        )
        unselected_required = tuple(
            item
            for item in self.candidates
            if (item.subject, item.predicate) in required and "required" not in item.reasons
        ) + tuple(
            item
            for item in self.omissions
            if (item.subject, item.predicate) in required
            and item.epistemic_status is EvidenceEpistemicStatus.OBSERVED
        )
        if unselected_required:
            raise ValueError("every required matching disposition must be selected as required")
        if self.required_match_count != len(matching_observed_dispositions):
            raise ValueError(
                "required_match_count must equal all matching observed dispositions: "
                f"expected {len(matching_observed_dispositions)}, "
                f"got {self.required_match_count}"
            )
        unexpected_required = tuple(
            item
            for item in self.candidates
            if "required" in item.reasons and (item.subject, item.predicate) not in required
        )
        if unexpected_required:
            raise ValueError("evidence candidate is marked required for an undeclared key")
        required_candidates = tuple(
            item
            for item in self.candidates
            if (item.subject, item.predicate) in required and "required" in item.reasons
        )
        missing_keys = tuple(
            key
            for key in self.required_keys
            if (key.subject, key.predicate)
            not in {(item.subject, item.predicate) for item in required_candidates}
        )
        if missing_keys:
            raise ValueError("evidence selection does not contain every required evidence key")
        if len(required_candidates) != self.required_match_count:
            raise ValueError(
                "evidence selection does not contain every required match: "
                f"expected {self.required_match_count}, selected {len(required_candidates)}"
            )
        object.__setattr__(self, "selection_id", json_digest(_selection_payload(self)))


def _gap_payload(gap: RequiredEvidenceGap) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": gap.schema_version,
        "subject": gap.key.subject,
        "predicate": gap.key.predicate,
        "source_packet_id": gap.source_packet_id,
        "state_domain": gap.state_domain,
        "state_stream_id": gap.state_stream_id,
        "state_global_position": gap.state_global_position,
        "state_stream_position": gap.state_stream_position,
        "reason": gap.reason,
    }
    if gap.schema_version == "required-evidence-gap/v3":
        payload.update(
            {
                "state_effective_time": (
                    gap.state_effective_time.isoformat()
                    if gap.state_effective_time is not None
                    else None
                ),
                "unknown_supports": [
                    {
                        "source_event_id": support.source_event_id,
                        "claim_id": support.claim_id,
                        "expires_at": support.expires_at.isoformat(),
                        "unknown_reason": support.unknown_reason,
                    }
                    for support in gap.unknown_supports
                ],
            }
        )
    return payload


def _selection_payload(selection: EvidenceSelection) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": selection.schema_version,
        "objective": selection.objective,
        "source_packet_id": selection.source_packet_id,
        "source_packet_purpose": selection.source_packet_purpose,
        "state_domain": selection.state_domain,
        "state_stream_id": selection.state_stream_id,
        "state_global_position": selection.state_global_position,
        "state_stream_position": selection.state_stream_position,
        "source_claim_identities": [
            {
                "source_event_id": identity.source_event_id,
                "claim_id": identity.claim_id,
            }
            for identity in selection.source_claim_identities
        ],
        "required_keys": [
            {"subject": key.subject, "predicate": key.predicate} for key in selection.required_keys
        ],
        "required_match_count": selection.required_match_count,
        "candidates": [
            _candidate_payload(item, selection.schema_version) for item in selection.candidates
        ],
        "omissions": [_omission_payload(item) for item in selection.omissions],
    }
    if selection.schema_version == "evidence-selection/v5":
        payload["state_effective_time"] = (
            selection.state_effective_time.isoformat()
            if selection.state_effective_time is not None
            else None
        )
    return payload


def _candidate_payload(
    candidate: EvidenceCandidate,
    selection_schema_version: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "claim_id": candidate.claim_id,
        "subject": candidate.subject,
        "predicate": candidate.predicate,
        "value": candidate.value,
        "confidence": candidate.confidence,
        "effective_at": candidate.effective_at.isoformat(),
        "freshness_seconds": candidate.freshness_seconds,
        "stale": candidate.stale,
        "source_event_id": candidate.source_event_id,
        "domain": candidate.domain,
        "stream_id": candidate.stream_id,
        "stream_sequence": candidate.stream_sequence,
        "global_position": candidate.global_position,
        "score": candidate.score,
        "reasons": list(candidate.reasons),
        "conflicted": candidate.conflicted,
    }
    if selection_schema_version == "evidence-selection/v5":
        payload.update(_epistemic_payload(candidate))
    return payload


def _omission_payload(omission: EvidenceOmission) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": omission.schema_version,
        "claim_id": omission.claim_id,
        "subject": omission.subject,
        "predicate": omission.predicate,
        "value": omission.value,
        "confidence": omission.confidence,
        "effective_at": omission.effective_at.isoformat(),
        "freshness_seconds": omission.freshness_seconds,
        "stale": omission.stale,
        "source_event_id": omission.source_event_id,
        "domain": omission.domain,
        "stream_id": omission.stream_id,
        "stream_sequence": omission.stream_sequence,
        "global_position": omission.global_position,
        "score": omission.score,
        "reasons": list(omission.reasons),
        "conflicted": omission.conflicted,
        "reason": omission.reason,
    }
    if omission.schema_version == "evidence-omission/v3":
        payload.update(_epistemic_payload(omission))
    return payload


def _epistemic_payload(
    item: EvidenceCandidate | EvidenceOmission,
) -> dict[str, object]:
    return {
        "epistemic_status": item.epistemic_status,
        "unknown_reason": item.unknown_reason,
        "expires_at": item.expires_at.isoformat() if item.expires_at is not None else None,
    }


def _has_epistemic_extensions(item: EvidenceCandidate | EvidenceOmission) -> bool:
    return (
        item.epistemic_status is not EvidenceEpistemicStatus.OBSERVED
        or item.unknown_reason is not None
        or item.expires_at is not None
    )


def _validate_claim_semantics(
    *,
    value: JsonScalar,
    confidence: float,
    effective_at: datetime,
    stale: bool,
    epistemic_status: EvidenceEpistemicStatus,
    unknown_reason: EvidenceUnknownReason | None,
    expires_at: datetime | None,
) -> None:
    if not isinstance(epistemic_status, EvidenceEpistemicStatus):
        raise TypeError("epistemic_status must be recognized")
    if unknown_reason is not None and not isinstance(unknown_reason, EvidenceUnknownReason):
        raise TypeError("unknown_reason must be recognized")
    if expires_at is not None:
        _require_aware(expires_at, "expires_at")
        if expires_at < effective_at:
            raise ValueError("expires_at cannot precede effective_at")
    if epistemic_status is EvidenceEpistemicStatus.OBSERVED:
        if unknown_reason is not None:
            raise ValueError("observed evidence cannot have an unknown reason")
    elif (
        value is not None
        or confidence != 0.0
        or unknown_reason is not EvidenceUnknownReason.EXPIRED
        or expires_at is None
        or not stale
    ):
        raise ValueError("unknown evidence requires explicit expired semantics")


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
