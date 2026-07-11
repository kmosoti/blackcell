from __future__ import annotations

from datetime import datetime

from blackcell.features.ingest_observation.command import IngestObservation
from blackcell.kernel import EventEnvelope

OBSERVATION_RECORDED = "observation.recorded"
OBSERVATION_SCHEMA_VERSION = "observation/v2"


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
                "claims": [
                    {
                        "claim_id": claim.claim_id,
                        "subject": claim.subject,
                        "predicate": claim.predicate,
                        "value": claim.value,
                        "confidence": claim.confidence,
                    }
                    for claim in observation.claims
                ],
                "evidence": [
                    {
                        "locator": pointer.locator,
                        "artifact_id": pointer.artifact_id,
                        "digest": pointer.digest,
                    }
                    for pointer in observation.evidence
                ],
            },
            recorded_at=recorded_at,
            effective_at=observation.effective_at,
            correlation_id=command.correlation_id,
            causation_id=command.causation_id,
            idempotency_key=observation.idempotency_key or observation.observation_id,
        )
        for offset, observation in enumerate(command.observations, start=1)
    )
