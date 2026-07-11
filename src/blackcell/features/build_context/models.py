from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from blackcell.kernel import JsonScalar
from blackcell.kernel._json import canonical_json, json_digest

_SUPPORTED_SOURCE_OMISSION_SCHEMA = "evidence-omission/v2"


@dataclass(frozen=True, slots=True)
class ContextEvidence:
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
    relevance_score: int
    selection_reasons: tuple[str, ...]
    conflicted: bool


@dataclass(frozen=True, slots=True, order=True)
class ContextClaimIdentity:
    source_event_id: str
    claim_id: str

    def __post_init__(self) -> None:
        if not self.source_event_id.strip() or not self.claim_id.strip():
            raise ValueError("context claim identities must not be empty")


class ContextOmissionStage(StrEnum):
    RETRIEVAL = "retrieval"
    CONTEXT_PROJECTION = "context-projection"


class ContextOmissionReason(StrEnum):
    IRRELEVANT = "irrelevant"
    RESULT_LIMIT = "retrieval-result-cap"
    CHARACTER_BUDGET = "context-character-budget"


@dataclass(frozen=True, slots=True)
class ContextOmission:
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
    relevance_score: int
    selection_reasons: tuple[str, ...]
    conflicted: bool
    stage: ContextOmissionStage
    reason: ContextOmissionReason
    model_payload_characters: int | None = None
    source_omission_id: str | None = None
    source_omission_schema_version: str | None = None
    schema_version: str = "context-omission/v2"
    omission_id: str = field(init=False)

    @property
    def serialized_characters(self) -> int | None:
        """Compatibility name for the model-payload contribution."""

        return self.model_payload_characters

    def __post_init__(self) -> None:
        if self.stage is ContextOmissionStage.RETRIEVAL:
            if self.reason is ContextOmissionReason.CHARACTER_BUDGET:
                raise ValueError("retrieval omissions require a retrieval reason")
            if not self.source_omission_id:
                raise ValueError("retrieval omissions require source_omission_id")
            if not self.source_omission_schema_version:
                raise ValueError("retrieval omissions require source_omission_schema_version")
            if self.source_omission_schema_version != _SUPPORTED_SOURCE_OMISSION_SCHEMA:
                raise ValueError("retrieval omission source schema is unsupported")
            if self.model_payload_characters is not None:
                raise ValueError("retrieval omissions cannot declare a model-payload size")
            if self.source_omission_id != _source_omission_digest(self):
                raise ValueError("retrieval omission content does not match source_omission_id")
        else:
            if self.reason is not ContextOmissionReason.CHARACTER_BUDGET:
                raise ValueError("context projection omissions require a character-budget reason")
            if self.model_payload_characters is None or self.model_payload_characters < 1:
                raise ValueError(
                    "context projection omissions require a positive model-payload size"
                )
            if self.source_omission_id is not None:
                raise ValueError("context projection omissions cannot reference a source omission")
            if self.source_omission_schema_version is not None:
                raise ValueError("context projection omissions cannot declare a source schema")
        object.__setattr__(self, "omission_id", json_digest(_omission_payload(self)))


@dataclass(frozen=True, slots=True)
class ContextFrame:
    task_id: str
    objective: str
    generated_at: datetime
    source_packet_id: str
    source_packet_purpose: str
    source_selection_id: str
    state_domain: str
    state_stream_id: str | None
    state_global_position: int
    state_stream_position: int
    source_claim_identities: tuple[ContextClaimIdentity, ...]
    evidence: tuple[ContextEvidence, ...]
    provenance_event_ids: tuple[str, ...]
    omissions: tuple[ContextOmission, ...]
    model_payload_characters: int
    schema_version: str = "context-frame/v3"
    frame_id: str = field(init=False)

    @property
    def state_position(self) -> int:
        """Compatibility name for the complete ledger cutoff."""

        return self.state_global_position

    @property
    def omitted_evidence_count(self) -> int:
        """Compatibility count derived from inspectable omission records."""

        return len(self.omissions)

    @property
    def model_payload(self) -> str:
        """Exact JSONL evidence view intended for model consumption.

        Audit-only omission bodies are intentionally excluded.
        """

        return "\n".join(serialize_context_evidence(item) for item in self.evidence)

    @property
    def serialized_characters(self) -> int:
        """Compatibility name for the exact model-facing evidence payload size."""

        return self.model_payload_characters

    def __post_init__(self) -> None:
        required_text = (
            self.task_id,
            self.objective,
            self.source_packet_id,
            self.source_packet_purpose,
            self.source_selection_id,
            self.state_domain,
        )
        if any(not item.strip() for item in required_text):
            raise ValueError("ContextFrame identities, purpose, and state domain must not be empty")
        if self.state_stream_id is not None and not self.state_stream_id.strip():
            raise ValueError("state_stream_id must not be blank")
        if self.state_global_position < 0 or self.state_stream_position < 0:
            raise ValueError("state positions must be non-negative")
        dispositions = (*self.evidence, *self.omissions)
        if self.state_stream_id is None and (dispositions or self.state_stream_position):
            raise ValueError("an unbound ContextFrame must not contain evidence")
        if any(
            item.domain != self.state_domain or item.stream_id != self.state_stream_id
            for item in dispositions
        ):
            raise ValueError("ContextFrame evidence must belong to its declared state scope")
        if any(item.global_position > self.state_global_position for item in dispositions):
            raise ValueError("ContextFrame evidence cannot exceed its ledger cutoff")
        if any(item.stream_sequence > self.state_stream_position for item in dispositions):
            raise ValueError("ContextFrame evidence cannot exceed its stream position")
        if tuple(sorted(set(self.source_claim_identities))) != self.source_claim_identities:
            raise ValueError("source claim identities must be sorted and unique")
        evidence_identities = {
            ContextClaimIdentity(item.source_event_id, item.claim_id) for item in self.evidence
        }
        omission_identities = {
            ContextClaimIdentity(item.source_event_id, item.claim_id) for item in self.omissions
        }
        if len(evidence_identities) != len(self.evidence):
            raise ValueError("ContextFrame evidence identities must be unique")
        if len(omission_identities) != len(self.omissions):
            raise ValueError("ContextFrame omission identities must be unique")
        if evidence_identities & omission_identities:
            raise ValueError("ContextFrame evidence and omission identities must be disjoint")
        if evidence_identities | omission_identities != set(self.source_claim_identities):
            raise ValueError("ContextFrame dispositions must exactly cover source packet claims")
        expected_provenance = tuple(dict.fromkeys(item.source_event_id for item in self.evidence))
        if self.provenance_event_ids != expected_provenance:
            raise ValueError("provenance_event_ids must match ordered evidence sources")
        if self.model_payload_characters != len(self.model_payload):
            raise ValueError("model_payload_characters must match the exact model payload")
        object.__setattr__(self, "frame_id", json_digest(_frame_payload(self)))


def serialize_context_evidence(evidence: ContextEvidence) -> str:
    return canonical_json(_evidence_payload(evidence))


def serialize_context_frame(frame: ContextFrame) -> str:
    """Return the canonical v3 identity payload, excluding the derived frame ID."""

    return canonical_json(_frame_payload(frame))


def _frame_payload(frame: ContextFrame) -> dict[str, object]:
    return {
        "schema_version": frame.schema_version,
        "task_id": frame.task_id,
        "objective": frame.objective,
        "generated_at": frame.generated_at.isoformat(),
        "source_packet_id": frame.source_packet_id,
        "source_packet_purpose": frame.source_packet_purpose,
        "source_selection_id": frame.source_selection_id,
        "state_domain": frame.state_domain,
        "state_stream_id": frame.state_stream_id,
        "state_global_position": frame.state_global_position,
        "state_stream_position": frame.state_stream_position,
        "source_claim_identities": [
            {
                "source_event_id": identity.source_event_id,
                "claim_id": identity.claim_id,
            }
            for identity in frame.source_claim_identities
        ],
        "evidence": [_evidence_payload(item) for item in frame.evidence],
        "provenance_event_ids": list(frame.provenance_event_ids),
        "omissions": [_omission_payload(item) for item in frame.omissions],
        "model_payload_characters": frame.model_payload_characters,
    }


def _evidence_payload(evidence: ContextEvidence) -> dict[str, object]:
    return {
        "claim_id": evidence.claim_id,
        "subject": evidence.subject,
        "predicate": evidence.predicate,
        "value": evidence.value,
        "confidence": evidence.confidence,
        "effective_at": evidence.effective_at.isoformat(),
        "freshness_seconds": evidence.freshness_seconds,
        "stale": evidence.stale,
        "source_event_id": evidence.source_event_id,
        "domain": evidence.domain,
        "stream_id": evidence.stream_id,
        "stream_sequence": evidence.stream_sequence,
        "global_position": evidence.global_position,
        "relevance_score": evidence.relevance_score,
        "selection_reasons": list(evidence.selection_reasons),
        "conflicted": evidence.conflicted,
    }


def _omission_payload(omission: ContextOmission) -> dict[str, object]:
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
        "relevance_score": omission.relevance_score,
        "selection_reasons": list(omission.selection_reasons),
        "conflicted": omission.conflicted,
        "stage": omission.stage,
        "reason": omission.reason,
        "model_payload_characters": omission.model_payload_characters,
        "source_omission_id": omission.source_omission_id,
        "source_omission_schema_version": omission.source_omission_schema_version,
    }


def _source_omission_digest(omission: ContextOmission) -> str:
    return json_digest(
        {
            "schema_version": omission.source_omission_schema_version,
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
            "score": omission.relevance_score,
            "reasons": list(omission.selection_reasons),
            "conflicted": omission.conflicted,
            "reason": omission.reason,
        }
    )
