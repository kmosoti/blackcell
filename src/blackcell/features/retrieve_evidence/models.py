from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from blackcell.kernel import JsonScalar
from blackcell.kernel._json import json_digest


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


class RequiredEvidenceGapReason(StrEnum):
    ABSENT = "absent-required-key"


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
        object.__setattr__(
            self,
            "gap_id",
            json_digest(
                {
                    "schema_version": self.schema_version,
                    "subject": self.key.subject,
                    "predicate": self.key.predicate,
                    "source_packet_id": self.source_packet_id,
                    "state_domain": self.state_domain,
                    "state_stream_id": self.state_stream_id,
                    "state_global_position": self.state_global_position,
                    "state_stream_position": self.state_stream_position,
                    "reason": self.reason,
                }
            ),
        )


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


class EvidenceOmissionReason(StrEnum):
    IRRELEVANT = "irrelevant"
    RESULT_LIMIT = "retrieval-result-cap"


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
    omission_id: str = field(init=False)

    def __post_init__(self) -> None:
        if "required" in self.reasons:
            raise ValueError("required evidence cannot be recorded as omitted")
        if self.reason is EvidenceOmissionReason.IRRELEVANT and self.reasons:
            raise ValueError("irrelevant omissions cannot have selection reasons")
        if self.reason is EvidenceOmissionReason.RESULT_LIMIT and not self.reasons:
            raise ValueError("result-limit omissions require selection reasons")
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
        required = {(key.subject, key.predicate) for key in self.required_keys}
        matching_dispositions = tuple(
            item for item in dispositions if (item.subject, item.predicate) in required
        )
        unselected_required = tuple(
            item
            for item in self.candidates
            if (item.subject, item.predicate) in required and "required" not in item.reasons
        ) + tuple(item for item in self.omissions if (item.subject, item.predicate) in required)
        if unselected_required:
            raise ValueError("every required matching disposition must be selected as required")
        if self.required_match_count != len(matching_dispositions):
            raise ValueError(
                "required_match_count must equal all matching source dispositions: "
                f"expected {len(matching_dispositions)}, got {self.required_match_count}"
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
        object.__setattr__(
            self,
            "selection_id",
            json_digest(
                {
                    "schema_version": self.schema_version,
                    "objective": self.objective,
                    "source_packet_id": self.source_packet_id,
                    "source_packet_purpose": self.source_packet_purpose,
                    "state_domain": self.state_domain,
                    "state_stream_id": self.state_stream_id,
                    "state_global_position": self.state_global_position,
                    "state_stream_position": self.state_stream_position,
                    "source_claim_identities": [
                        {
                            "source_event_id": identity.source_event_id,
                            "claim_id": identity.claim_id,
                        }
                        for identity in self.source_claim_identities
                    ],
                    "required_keys": [
                        {"subject": key.subject, "predicate": key.predicate}
                        for key in self.required_keys
                    ],
                    "required_match_count": self.required_match_count,
                    "candidates": [_candidate_payload(item) for item in self.candidates],
                    "omissions": [_omission_payload(item) for item in self.omissions],
                }
            ),
        )


def _candidate_payload(candidate: EvidenceCandidate) -> dict[str, object]:
    return {
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


def _omission_payload(omission: EvidenceOmission) -> dict[str, object]:
    return {
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
