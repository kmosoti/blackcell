from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from blackcell.kernel import JsonScalar
from blackcell.kernel._json import json_digest


@dataclass(frozen=True, slots=True)
class ContextEvidence:
    subject: str
    predicate: str
    value: JsonScalar
    confidence: float
    effective_at: datetime
    freshness_seconds: int
    stale: bool
    source_event_id: str
    relevance_score: int
    selection_reasons: tuple[str, ...]
    conflicted: bool


class ContextOmissionStage(StrEnum):
    RETRIEVAL = "retrieval"
    CONTEXT_PROJECTION = "context-projection"


class ContextOmissionReason(StrEnum):
    IRRELEVANT = "irrelevant"
    RESULT_LIMIT = "retrieval-result-cap"
    CHARACTER_BUDGET = "context-character-budget"


@dataclass(frozen=True, slots=True)
class ContextOmission:
    subject: str
    predicate: str
    value: JsonScalar
    confidence: float
    effective_at: datetime
    freshness_seconds: int
    stale: bool
    source_event_id: str
    relevance_score: int
    selection_reasons: tuple[str, ...]
    conflicted: bool
    stage: ContextOmissionStage
    reason: ContextOmissionReason
    serialized_characters: int | None = None
    source_omission_id: str | None = None
    schema_version: str = "context-omission/v1"
    omission_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.stage is ContextOmissionStage.RETRIEVAL:
            if self.reason is ContextOmissionReason.CHARACTER_BUDGET:
                raise ValueError("retrieval omissions require a retrieval reason")
            if not self.source_omission_id:
                raise ValueError("retrieval omissions require source_omission_id")
            if self.serialized_characters is not None:
                raise ValueError("retrieval omissions cannot declare a character size")
        else:
            if self.reason is not ContextOmissionReason.CHARACTER_BUDGET:
                raise ValueError("context projection omissions require a character-budget reason")
            if self.serialized_characters is None or self.serialized_characters < 1:
                raise ValueError("context projection omissions require a positive character size")
            if self.source_omission_id is not None:
                raise ValueError("context projection omissions cannot reference a source omission")
        object.__setattr__(self, "omission_id", json_digest(_omission_payload(self)))


@dataclass(frozen=True, slots=True)
class ContextFrame:
    task_id: str
    objective: str
    generated_at: datetime
    state_position: int
    source_packet_id: str
    source_selection_id: str
    evidence: tuple[ContextEvidence, ...]
    provenance_event_ids: tuple[str, ...]
    omissions: tuple[ContextOmission, ...]
    serialized_characters: int
    schema_version: str = "context-frame/v2"
    frame_id: str = field(init=False)

    @property
    def omitted_evidence_count(self) -> int:
        """Compatibility count derived from inspectable omission records."""

        return len(self.omissions)

    def __post_init__(self) -> None:
        if self.serialized_characters < 0:
            raise ValueError("serialized_characters must not be negative")
        expected_provenance = tuple(dict.fromkeys(item.source_event_id for item in self.evidence))
        if self.provenance_event_ids != expected_provenance:
            raise ValueError("provenance_event_ids must match ordered evidence sources")
        object.__setattr__(
            self,
            "frame_id",
            json_digest(
                {
                    "schema_version": self.schema_version,
                    "task_id": self.task_id,
                    "objective": self.objective,
                    "generated_at": self.generated_at.isoformat(),
                    "state_position": self.state_position,
                    "source_packet_id": self.source_packet_id,
                    "source_selection_id": self.source_selection_id,
                    "evidence": [_evidence_payload(item) for item in self.evidence],
                    "provenance_event_ids": list(self.provenance_event_ids),
                    "omissions": [_omission_payload(item) for item in self.omissions],
                    "serialized_characters": self.serialized_characters,
                }
            ),
        )


def _evidence_payload(evidence: ContextEvidence) -> dict[str, object]:
    return {
        "subject": evidence.subject,
        "predicate": evidence.predicate,
        "value": evidence.value,
        "confidence": evidence.confidence,
        "effective_at": evidence.effective_at.isoformat(),
        "freshness_seconds": evidence.freshness_seconds,
        "stale": evidence.stale,
        "source_event_id": evidence.source_event_id,
        "relevance_score": evidence.relevance_score,
        "selection_reasons": list(evidence.selection_reasons),
        "conflicted": evidence.conflicted,
    }


def _omission_payload(omission: ContextOmission) -> dict[str, object]:
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
        "relevance_score": omission.relevance_score,
        "selection_reasons": list(omission.selection_reasons),
        "conflicted": omission.conflicted,
        "stage": omission.stage,
        "reason": omission.reason,
        "serialized_characters": omission.serialized_characters,
        "source_omission_id": omission.source_omission_id,
    }
