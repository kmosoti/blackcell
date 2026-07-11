from __future__ import annotations

from datetime import datetime

from blackcell.features.ingest_observation.command import (
    EvidencePointer,
    IngestCorrection,
    IngestObservation,
    ObservedClaim,
)
from blackcell.kernel import EventEnvelope, JsonInput

OBSERVATION_RECORDED = "observation.recorded"
OBSERVATION_SCHEMA_VERSION = "observation/v2"
OBSERVATION_CORRECTED = "observation.corrected"
CORRECTION_SCHEMA_VERSION = "observation-correction/v1"


def observation_events(
    command: IngestObservation,
    *,
    recorded_at: datetime,
) -> tuple[EventEnvelope, ...]:
    return tuple(
        EventEnvelope.create(
            stream_id=command.stream_id,
            stream_sequence=command.expected_sequence + offset,
            event_type=OBSERVATION_RECORDED,
            actor=command.actor,
            source=command.source,
            payload={
                "domain": command.domain,
                "observation_id": observation.observation_id,
                "observation_schema_version": OBSERVATION_SCHEMA_VERSION,
                "claims": [_claim_payload(claim) for claim in observation.claims],
                "evidence": [_evidence_payload(pointer) for pointer in observation.evidence],
            },
            recorded_at=recorded_at,
            effective_at=observation.effective_at,
            correlation_id=command.correlation_id,
            causation_id=command.causation_id,
            idempotency_key=observation.idempotency_key or observation.observation_id,
        )
        for offset, observation in enumerate(command.observations, start=1)
    )


def correction_events(
    command: IngestCorrection,
    *,
    recorded_at: datetime,
) -> tuple[EventEnvelope, ...]:
    return tuple(
        EventEnvelope.create(
            stream_id=command.stream_id,
            stream_sequence=command.expected_sequence + offset,
            event_type=OBSERVATION_CORRECTED,
            actor=command.actor,
            source=command.source,
            payload={
                "domain": command.domain,
                "correction_id": correction.correction_id,
                "correction_schema_version": CORRECTION_SCHEMA_VERSION,
                "supersedes_claim_ids": list(correction.supersedes_claim_ids),
                "replacement": _claim_payload(correction.replacement),
                "reason": correction.reason,
                "evidence": [_evidence_payload(pointer) for pointer in correction.evidence],
            },
            recorded_at=recorded_at,
            effective_at=correction.effective_at,
            correlation_id=command.correlation_id,
            causation_id=command.causation_id,
            idempotency_key=correction.idempotency_key or correction.correction_id,
        )
        for offset, correction in enumerate(command.corrections, start=1)
    )


def _claim_payload(claim: ObservedClaim) -> dict[str, JsonInput]:
    return {
        "claim_id": claim.claim_id,
        "subject": claim.subject,
        "predicate": claim.predicate,
        "value": claim.value,
        "confidence": claim.confidence,
    }


def _evidence_payload(pointer: EvidencePointer) -> dict[str, JsonInput]:
    return {
        "locator": pointer.locator,
        "artifact_id": pointer.artifact_id,
        "digest": pointer.digest,
    }
