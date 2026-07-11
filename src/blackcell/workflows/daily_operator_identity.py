from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from blackcell.kernel import JsonInput
from blackcell.kernel._json import canonical_json, json_digest
from blackcell.workflows.run_protocol import RUN_WORKFLOW_VERSION

if TYPE_CHECKING:
    from blackcell.workflows.daily_operator import DailyOperatorRequest

_REQUEST_SCHEMA_VERSION = "daily-operator-request/v1"


def daily_operator_request_digest(request: DailyOperatorRequest) -> str:
    return json_digest(daily_operator_request_payload(request))


def daily_operator_request_payload(request: DailyOperatorRequest) -> dict[str, JsonInput]:
    """Return the complete semantic identity of one Daily Operator delivery."""

    return {
        "schema_version": _REQUEST_SCHEMA_VERSION,
        "workflow_version": RUN_WORKFLOW_VERSION,
        "run_id": request.run_id,
        "ingestion": {
            "stream_id": request.ingestion.stream_id,
            "expected_sequence": request.ingestion.expected_sequence,
            "actor": request.ingestion.actor,
            "source": request.ingestion.source,
            "correlation_id": request.ingestion.correlation_id,
            "causation_id": request.ingestion.causation_id,
            "domain": request.ingestion.domain,
            "observations": [
                {
                    "observation_id": observation.observation_id,
                    "effective_at": _timestamp(observation.effective_at),
                    "idempotency_key": observation.idempotency_key,
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
                }
                for observation in request.ingestion.observations
            ],
        },
        "signal": {
            "purpose": request.signal.purpose,
            "generated_at": _timestamp(request.signal.generated_at),
            "stale_after_seconds": request.signal.stale_after_seconds,
        },
        "retrieval": {
            "objective": request.retrieval.objective,
            "required_keys": [
                {"subject": key.subject, "predicate": key.predicate}
                for key in request.retrieval.required_keys
            ],
            "max_results": request.retrieval.max_results,
        },
        "context": {
            "task_id": request.context.task_id,
            "objective": request.context.objective,
            "generated_at": _timestamp(request.context.generated_at),
            "max_characters": request.context.max_characters,
        },
        "constraints": {
            "evaluated_at": _timestamp(request.constraints.evaluated_at),
            "definitions": [
                {
                    "schema_version": definition.schema_version,
                    "constraint_id": definition.constraint_id,
                    "description": definition.description,
                    "subject": definition.subject,
                    "predicate": definition.predicate,
                    "operator": definition.operator.value,
                    "expected_values": _set_semantic_values(definition.expected_values),
                    "minimum_confidence": definition.minimum_confidence,
                    "max_age_seconds": definition.max_age_seconds,
                }
                for definition in request.constraints.constraints
            ],
        },
        "authorization_affordance": {
            "name": request.authorization_affordance.name,
            "read_only": request.authorization_affordance.read_only,
            "external": request.authorization_affordance.external,
            "mutates_state": request.authorization_affordance.mutates_state,
            "evidence_action": request.authorization_affordance.evidence_action,
            "allowed_arguments": sorted(request.authorization_affordance.allowed_arguments),
        },
        "execution_affordance": {
            "name": request.execution_affordance.name,
            "adapter_id": request.execution_affordance.adapter_id,
            "side_effect_class": request.execution_affordance.side_effect_class.value,
            "timeout_seconds": request.execution_affordance.timeout_seconds,
            "arguments": [
                {"name": argument.name, "required": argument.required}
                for argument in sorted(
                    request.execution_affordance.arguments,
                    key=lambda item: item.name,
                )
            ],
        },
        "invocation_id": request.invocation_id,
        "idempotency_key": request.idempotency_key,
        "approval_granted": request.approval_granted,
    }


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _set_semantic_values(values: tuple[object, ...]) -> list[JsonInput]:
    encoded = sorted({canonical_json({"value": value}) for value in values})
    return [json.loads(item)["value"] for item in encoded]
