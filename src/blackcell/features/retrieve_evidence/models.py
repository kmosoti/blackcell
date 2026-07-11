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


class RequiredEvidenceGapReason(StrEnum):
    ABSENT = "absent-required-key"


@dataclass(frozen=True, slots=True)
class RequiredEvidenceGap:
    key: EvidenceKey
    reason: RequiredEvidenceGapReason = RequiredEvidenceGapReason.ABSENT
    schema_version: str = "required-evidence-gap/v1"
    gap_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "gap_id",
            json_digest(
                {
                    "schema_version": self.schema_version,
                    "subject": self.key.subject,
                    "predicate": self.key.predicate,
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
    subject: str
    predicate: str
    value: JsonScalar
    confidence: float
    effective_at: datetime
    freshness_seconds: int
    stale: bool
    source_event_id: str
    score: int
    reasons: tuple[str, ...]
    conflicted: bool


class EvidenceOmissionReason(StrEnum):
    IRRELEVANT = "irrelevant"
    RESULT_LIMIT = "retrieval-result-cap"


@dataclass(frozen=True, slots=True)
class EvidenceOmission:
    subject: str
    predicate: str
    value: JsonScalar
    confidence: float
    effective_at: datetime
    freshness_seconds: int
    stale: bool
    source_event_id: str
    score: int
    reasons: tuple[str, ...]
    conflicted: bool
    reason: EvidenceOmissionReason
    schema_version: str = "evidence-omission/v1"
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
    state_position: int
    candidates: tuple[EvidenceCandidate, ...]
    omissions: tuple[EvidenceOmission, ...]
    required_keys: tuple[EvidenceKey, ...] = ()
    required_match_count: int = 0
    schema_version: str = "evidence-selection/v3"
    selection_id: str = field(init=False)

    @property
    def omitted_count(self) -> int:
        """Compatibility count derived from inspectable omission records."""

        return len(self.omissions)

    def __post_init__(self) -> None:
        required = {(key.subject, key.predicate) for key in self.required_keys}
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
                    "state_position": self.state_position,
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
        "subject": candidate.subject,
        "predicate": candidate.predicate,
        "value": candidate.value,
        "confidence": candidate.confidence,
        "effective_at": candidate.effective_at.isoformat(),
        "freshness_seconds": candidate.freshness_seconds,
        "stale": candidate.stale,
        "source_event_id": candidate.source_event_id,
        "score": candidate.score,
        "reasons": list(candidate.reasons),
        "conflicted": candidate.conflicted,
    }


def _omission_payload(omission: EvidenceOmission) -> dict[str, object]:
    return {
        "schema_version": omission.schema_version,
        "subject": omission.subject,
        "predicate": omission.predicate,
        "value": omission.value,
        "confidence": omission.confidence,
        "effective_at": omission.effective_at.isoformat(),
        "freshness_seconds": omission.freshness_seconds,
        "stale": omission.stale,
        "source_event_id": omission.source_event_id,
        "score": omission.score,
        "reasons": list(omission.reasons),
        "conflicted": omission.conflicted,
        "reason": omission.reason,
    }
