from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from blackcell.kernel import JsonScalar
from blackcell.kernel._json import canonical_json, json_digest

_SUPPORTED_SOURCE_OMISSION_SCHEMAS = frozenset({"evidence-omission/v2", "evidence-omission/v3"})


class ContextEpistemicStatus(StrEnum):
    OBSERVED = "observed"
    UNKNOWN = "unknown"


class ContextUnknownReason(StrEnum):
    EXPIRED = "expired"


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
    epistemic_status: ContextEpistemicStatus = ContextEpistemicStatus.OBSERVED
    unknown_reason: ContextUnknownReason | None = None
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
        if self.epistemic_status is not ContextEpistemicStatus.OBSERVED:
            raise ValueError("unknown claims cannot be asserted as ContextFrame evidence")


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
    UNKNOWN = "expired-unknown"


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
    epistemic_status: ContextEpistemicStatus = ContextEpistemicStatus.OBSERVED
    unknown_reason: ContextUnknownReason | None = None
    expires_at: datetime | None = None
    omission_id: str = field(init=False)

    @property
    def serialized_characters(self) -> int | None:
        """Compatibility name for the model-payload contribution."""

        return self.model_payload_characters

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
        if self.stage is ContextOmissionStage.RETRIEVAL:
            if self.reason is ContextOmissionReason.CHARACTER_BUDGET:
                raise ValueError("retrieval omissions require a retrieval reason")
            if not self.source_omission_id:
                raise ValueError("retrieval omissions require source_omission_id")
            if not self.source_omission_schema_version:
                raise ValueError("retrieval omissions require source_omission_schema_version")
            if self.source_omission_schema_version not in _SUPPORTED_SOURCE_OMISSION_SCHEMAS:
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
        if self.reason is ContextOmissionReason.UNKNOWN:
            if (
                self.stage is not ContextOmissionStage.RETRIEVAL
                or self.selection_reasons
                or self.epistemic_status is not ContextEpistemicStatus.UNKNOWN
            ):
                raise ValueError("expired-unknown omissions require unknown retrieval semantics")
        elif self.epistemic_status is not ContextEpistemicStatus.OBSERVED:
            raise ValueError("unknown claims require the expired-unknown omission reason")
        if self.schema_version not in {"context-omission/v2", "context-omission/v3"}:
            raise ValueError("context omission schema is unsupported")
        if self.schema_version == "context-omission/v2" and _has_epistemic_extensions(self):
            raise ValueError("context-omission/v2 cannot contain epistemic extensions")
        if self.schema_version == "context-omission/v3" and not _has_epistemic_extensions(self):
            raise ValueError("context-omission/v3 requires epistemic extensions")
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
    state_effective_time: datetime | None = None
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

        return "\n".join(
            serialize_context_evidence(item, schema_version=self.schema_version)
            for item in self.evidence
        )

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
        if self.state_effective_time is not None:
            _require_aware(self.state_effective_time, "state_effective_time")
        if self.schema_version not in {"context-frame/v3", "context-frame/v4"}:
            raise ValueError("ContextFrame schema is unsupported")
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
        has_epistemic_extensions = self.state_effective_time is not None or any(
            _has_epistemic_extensions(item) for item in dispositions
        )
        if self.schema_version == "context-frame/v3" and has_epistemic_extensions:
            raise ValueError("context-frame/v3 cannot contain epistemic extensions")
        if self.schema_version == "context-frame/v4" and not has_epistemic_extensions:
            raise ValueError("context-frame/v4 requires epistemic extensions")
        expected_provenance = tuple(dict.fromkeys(item.source_event_id for item in self.evidence))
        if self.provenance_event_ids != expected_provenance:
            raise ValueError("provenance_event_ids must match ordered evidence sources")
        if self.model_payload_characters != len(self.model_payload):
            raise ValueError("model_payload_characters must match the exact model payload")
        object.__setattr__(self, "frame_id", json_digest(_frame_payload(self)))


def serialize_context_evidence(
    evidence: ContextEvidence,
    *,
    schema_version: str | None = None,
) -> str:
    selected_schema = schema_version or (
        "context-frame/v4" if _has_epistemic_extensions(evidence) else "context-frame/v3"
    )
    if selected_schema not in {"context-frame/v3", "context-frame/v4"}:
        raise ValueError("ContextFrame evidence schema is unsupported")
    if selected_schema == "context-frame/v3" and _has_epistemic_extensions(evidence):
        raise ValueError("context-frame/v3 evidence cannot contain epistemic extensions")
    return canonical_json(_evidence_payload(evidence, selected_schema))


def serialize_context_frame(frame: ContextFrame) -> str:
    """Return the canonical identity payload, excluding the derived frame ID."""

    return canonical_json(_frame_payload(frame))


def _frame_payload(frame: ContextFrame) -> dict[str, object]:
    payload: dict[str, object] = {
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
        "evidence": [_evidence_payload(item, frame.schema_version) for item in frame.evidence],
        "provenance_event_ids": list(frame.provenance_event_ids),
        "omissions": [_omission_payload(item) for item in frame.omissions],
        "model_payload_characters": frame.model_payload_characters,
    }
    if frame.schema_version == "context-frame/v4":
        payload["state_effective_time"] = (
            frame.state_effective_time.isoformat()
            if frame.state_effective_time is not None
            else None
        )
    return payload


def _evidence_payload(
    evidence: ContextEvidence,
    frame_schema_version: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
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
    if frame_schema_version == "context-frame/v4":
        payload.update(_epistemic_payload(evidence))
    return payload


def _omission_payload(omission: ContextOmission) -> dict[str, object]:
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
        "relevance_score": omission.relevance_score,
        "selection_reasons": list(omission.selection_reasons),
        "conflicted": omission.conflicted,
        "stage": omission.stage,
        "reason": omission.reason,
        "model_payload_characters": omission.model_payload_characters,
        "source_omission_id": omission.source_omission_id,
        "source_omission_schema_version": omission.source_omission_schema_version,
    }
    if omission.schema_version == "context-omission/v3":
        payload.update(_epistemic_payload(omission))
    return payload


def _source_omission_digest(omission: ContextOmission) -> str:
    payload: dict[str, object] = {
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
    if omission.source_omission_schema_version == "evidence-omission/v3":
        payload.update(_epistemic_payload(omission))
    return json_digest(payload)


def _epistemic_payload(
    item: ContextEvidence | ContextOmission,
) -> dict[str, object]:
    return {
        "epistemic_status": item.epistemic_status,
        "unknown_reason": item.unknown_reason,
        "expires_at": item.expires_at.isoformat() if item.expires_at is not None else None,
    }


def _has_epistemic_extensions(item: ContextEvidence | ContextOmission) -> bool:
    return (
        item.epistemic_status is not ContextEpistemicStatus.OBSERVED
        or item.unknown_reason is not None
        or item.expires_at is not None
    )


def _validate_claim_semantics(
    *,
    value: JsonScalar,
    confidence: float,
    effective_at: datetime,
    stale: bool,
    epistemic_status: ContextEpistemicStatus,
    unknown_reason: ContextUnknownReason | None,
    expires_at: datetime | None,
) -> None:
    if not isinstance(epistemic_status, ContextEpistemicStatus):
        raise TypeError("epistemic_status must be recognized")
    if unknown_reason is not None and not isinstance(unknown_reason, ContextUnknownReason):
        raise TypeError("unknown_reason must be recognized")
    if expires_at is not None:
        _require_aware(expires_at, "expires_at")
        if expires_at < effective_at:
            raise ValueError("expires_at cannot precede effective_at")
    if epistemic_status is ContextEpistemicStatus.OBSERVED:
        if unknown_reason is not None:
            raise ValueError("observed context dispositions cannot have an unknown reason")
    elif (
        value is not None
        or confidence != 0.0
        or unknown_reason is not ContextUnknownReason.EXPIRED
        or expires_at is None
        or not stale
    ):
        raise ValueError("unknown context dispositions require explicit expired semantics")


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
