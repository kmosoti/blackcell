from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast

from blackcell.features.authorize_action import AffordancePolicy
from blackcell.features.build_context import BuildContext
from blackcell.features.derive_signal_packet import DeriveSignalPacket
from blackcell.features.evaluate_outcome import (
    EvaluationArtifactCodecError,
    EvaluationSpec,
    decode_evaluation_spec,
    evaluation_spec_payload,
)
from blackcell.features.execute_affordance import (
    AffordanceArgumentSpec,
    AffordanceDefinition,
    SideEffectClass,
)
from blackcell.features.ingest_observation import (
    EvidencePointer,
    IngestObservation,
    ObservationInput,
    ObservedClaim,
)
from blackcell.features.request_decision import (
    DecisionBudget,
    DecisionCapability,
    DecisionClassification,
    DecisionLocality,
    DecisionRequirements,
)
from blackcell.features.retrieve_evidence import EvidenceKey, RetrieveEvidence
from blackcell.features.solve_constraints import (
    ConstraintDefinition,
    ConstraintOperator,
    SolveConstraints,
)
from blackcell.kernel import JsonScalar
from blackcell.kernel._json import canonical_json_bytes, json_digest
from blackcell.workflows.daily_operator_v2_request import DailyOperatorV2Request
from blackcell.workflows.run_protocol import RUN_WORKFLOW_VERSION_V2

DAILY_OPERATOR_REQUEST_V2_SCHEMA_VERSION = "daily-operator-request/v2"
DAILY_OPERATOR_REQUEST_V2_MEDIA_TYPE = "application/vnd.blackcell.daily-operator-request+json"

_AFFORDANCE_POLICY_SCHEMA_VERSION = "affordance-policy/v1"
_AFFORDANCE_DEFINITION_SCHEMA_VERSION = "affordance-definition/v1"
_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "workflow_version",
        "request_digest",
        "run_id",
        "ingestion",
        "initial_effective_time_cutoff",
        "signal",
        "retrieval",
        "context",
        "constraints",
        "evaluation_spec",
        "gateway_requirements",
        "authorization_affordance",
        "execution_affordance",
        "invocation_id",
        "idempotency_key",
        "expected_observer_id",
        "expected_observer_contract_version",
        "approval_granted",
    }
)
_INGESTION_KEYS = frozenset(
    {
        "stream_id",
        "expected_sequence",
        "actor",
        "source",
        "correlation_id",
        "causation_id",
        "domain",
        "observations",
    }
)
_OBSERVATION_KEYS = frozenset(
    {"observation_id", "effective_at", "idempotency_key", "claims", "evidence"}
)
_CLAIM_KEYS = frozenset({"claim_id", "subject", "predicate", "value", "confidence", "expires_at"})
_EVIDENCE_KEYS = frozenset({"locator", "artifact_id", "digest"})
_SIGNAL_KEYS = frozenset({"purpose", "generated_at", "stale_after_seconds"})
_RETRIEVAL_KEYS = frozenset({"objective", "required_keys", "max_results"})
_EVIDENCE_KEY_KEYS = frozenset({"subject", "predicate"})
_CONTEXT_KEYS = frozenset({"task_id", "objective", "generated_at", "max_characters"})
_CONSTRAINTS_KEYS = frozenset({"evaluated_at", "definitions"})
_CONSTRAINT_KEYS = frozenset(
    {
        "schema_version",
        "constraint_id",
        "description",
        "subject",
        "predicate",
        "operator",
        "expected_values",
        "minimum_confidence",
        "max_age_seconds",
    }
)
_GATEWAY_KEYS = frozenset(
    {
        "request_id",
        "node_id",
        "capability",
        "classification",
        "locality",
        "budget",
        "estimated_input_tokens",
        "deterministic_required",
        "requested_at",
    }
)
_BUDGET_KEYS = frozenset(
    {"max_input_tokens", "max_output_tokens", "max_latency_ms", "max_cost_microusd"}
)
_AUTHORIZATION_KEYS = frozenset(
    {
        "schema_version",
        "name",
        "read_only",
        "external",
        "mutates_state",
        "evidence_action",
        "allowed_arguments",
    }
)
_EXECUTION_KEYS = frozenset(
    {
        "schema_version",
        "name",
        "adapter_id",
        "side_effect_class",
        "timeout_seconds",
        "arguments",
    }
)
_ARGUMENT_SPEC_KEYS = frozenset({"name", "required"})


class DailyOperatorV2RequestCodecError(ValueError):
    """A v2 request is malformed, non-canonical, or has a false identity."""


def daily_operator_v2_request_identity_payload(
    request: DailyOperatorV2Request,
) -> dict[str, object]:
    """Return every caller-controlled input that determines one v2 run."""

    return {
        "schema_version": DAILY_OPERATOR_REQUEST_V2_SCHEMA_VERSION,
        "workflow_version": RUN_WORKFLOW_VERSION_V2,
        "run_id": request.run_id,
        "ingestion": _ingestion_payload(request.ingestion),
        "initial_effective_time_cutoff": _timestamp(request.initial_effective_time_cutoff),
        "signal": {
            "purpose": request.signal.purpose,
            "generated_at": _timestamp(request.signal.generated_at),
            "stale_after_seconds": request.signal.stale_after_seconds,
        },
        "retrieval": {
            "objective": request.retrieval.objective,
            "required_keys": [
                {"subject": item.subject, "predicate": item.predicate}
                for item in request.retrieval.required_keys
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
            "definitions": [_constraint_payload(item) for item in request.constraints.constraints],
        },
        "evaluation_spec": evaluation_spec_payload(request.evaluation_spec),
        "gateway_requirements": _gateway_payload(request.gateway_requirements),
        "authorization_affordance": {
            "schema_version": _AFFORDANCE_POLICY_SCHEMA_VERSION,
            "name": request.authorization_affordance.name,
            "read_only": request.authorization_affordance.read_only,
            "external": request.authorization_affordance.external,
            "mutates_state": request.authorization_affordance.mutates_state,
            "evidence_action": request.authorization_affordance.evidence_action,
            "allowed_arguments": list(request.authorization_affordance.allowed_arguments),
        },
        "execution_affordance": {
            "schema_version": _AFFORDANCE_DEFINITION_SCHEMA_VERSION,
            "name": request.execution_affordance.name,
            "adapter_id": request.execution_affordance.adapter_id,
            "side_effect_class": request.execution_affordance.side_effect_class.value,
            "timeout_seconds": request.execution_affordance.timeout_seconds,
            "arguments": [
                {"name": item.name, "required": item.required}
                for item in request.execution_affordance.arguments
            ],
        },
        "invocation_id": request.invocation_id,
        "idempotency_key": request.idempotency_key,
        "expected_observer_id": request.expected_observer_id,
        "expected_observer_contract_version": request.expected_observer_contract_version,
        "approval_granted": request.approval_granted,
    }


def daily_operator_v2_request_digest(request: DailyOperatorV2Request) -> str:
    return json_digest(daily_operator_v2_request_identity_payload(request))


def daily_operator_v2_request_payload(request: DailyOperatorV2Request) -> dict[str, object]:
    return {
        **daily_operator_v2_request_identity_payload(request),
        "request_digest": daily_operator_v2_request_digest(request),
    }


def encode_daily_operator_v2_request(request: DailyOperatorV2Request) -> bytes:
    return canonical_json_bytes(daily_operator_v2_request_payload(request))


def decode_daily_operator_v2_request(data: bytes) -> DailyOperatorV2Request:
    payload = _decode_object(data)
    _keys(payload, _ROOT_KEYS, "DailyOperatorV2Request")
    _schema(
        payload,
        "schema_version",
        DAILY_OPERATOR_REQUEST_V2_SCHEMA_VERSION,
        "DailyOperatorV2Request",
    )
    _schema(
        payload,
        "workflow_version",
        RUN_WORKFLOW_VERSION_V2,
        "DailyOperatorV2Request",
    )
    try:
        request = DailyOperatorV2Request(
            run_id=_text(payload["run_id"], "run_id"),
            ingestion=_decode_ingestion(payload["ingestion"]),
            initial_effective_time_cutoff=_datetime(
                payload["initial_effective_time_cutoff"],
                "initial_effective_time_cutoff",
            ),
            signal=_decode_signal(payload["signal"]),
            retrieval=_decode_retrieval(payload["retrieval"]),
            context=_decode_context(payload["context"]),
            constraints=_decode_constraints(payload["constraints"]),
            evaluation_spec=_decode_evaluation_spec(payload["evaluation_spec"]),
            gateway_requirements=_decode_gateway(payload["gateway_requirements"]),
            authorization_affordance=_decode_authorization(payload["authorization_affordance"]),
            execution_affordance=_decode_execution(payload["execution_affordance"]),
            invocation_id=_text(payload["invocation_id"], "invocation_id"),
            idempotency_key=_text(payload["idempotency_key"], "idempotency_key"),
            expected_observer_id=_text(payload["expected_observer_id"], "expected_observer_id"),
            expected_observer_contract_version=_text(
                payload["expected_observer_contract_version"],
                "expected_observer_contract_version",
            ),
            approval_granted=_boolean(payload["approval_granted"], "approval_granted"),
        )
    except DailyOperatorV2RequestCodecError:
        raise
    except (TypeError, ValueError) as error:
        raise DailyOperatorV2RequestCodecError(
            "DailyOperatorV2Request violates its contract"
        ) from error

    expected_digest = daily_operator_v2_request_digest(request)
    if _text(payload["request_digest"], "request_digest") != expected_digest:
        raise DailyOperatorV2RequestCodecError(
            "request_digest does not match DailyOperatorV2Request content"
        )
    if encode_daily_operator_v2_request(request) != data:
        raise DailyOperatorV2RequestCodecError(
            "DailyOperatorV2Request collections and values must use canonical domain ordering"
        )
    return request


def _ingestion_payload(command: IngestObservation) -> dict[str, object]:
    return {
        "stream_id": command.stream_id,
        "expected_sequence": command.expected_sequence,
        "actor": command.actor,
        "source": command.source,
        "correlation_id": command.correlation_id,
        "causation_id": command.causation_id,
        "domain": command.domain,
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
                        "expires_at": (
                            None if claim.expires_at is None else _timestamp(claim.expires_at)
                        ),
                    }
                    for claim in observation.claims
                ],
                "evidence": [
                    {
                        "locator": item.locator,
                        "artifact_id": item.artifact_id,
                        "digest": item.digest,
                    }
                    for item in observation.evidence
                ],
            }
            for observation in command.observations
        ],
    }


def _constraint_payload(definition: ConstraintDefinition) -> dict[str, object]:
    return {
        "schema_version": definition.schema_version,
        "constraint_id": definition.constraint_id,
        "description": definition.description,
        "subject": definition.subject,
        "predicate": definition.predicate,
        "operator": definition.operator.value,
        "expected_values": list(definition.expected_values),
        "minimum_confidence": definition.minimum_confidence,
        "max_age_seconds": definition.max_age_seconds,
    }


def _gateway_payload(requirements: DecisionRequirements) -> dict[str, object]:
    return {
        "request_id": requirements.request_id,
        "node_id": requirements.node_id,
        "capability": requirements.capability.value,
        "classification": requirements.classification.value,
        "locality": requirements.locality.value,
        "budget": {
            "max_input_tokens": requirements.budget.max_input_tokens,
            "max_output_tokens": requirements.budget.max_output_tokens,
            "max_latency_ms": requirements.budget.max_latency_ms,
            "max_cost_microusd": requirements.budget.max_cost_microusd,
        },
        "estimated_input_tokens": requirements.estimated_input_tokens,
        "deterministic_required": requirements.deterministic_required,
        "requested_at": _timestamp(requirements.requested_at),
    }


def _decode_ingestion(value: object) -> IngestObservation:
    payload = _object(value, _INGESTION_KEYS, "ingestion")
    observations = tuple(
        _decode_observation(item, index)
        for index, item in enumerate(_array(payload["observations"], "observations"))
    )
    return IngestObservation(
        stream_id=_text(payload["stream_id"], "ingestion.stream_id"),
        expected_sequence=_integer(payload["expected_sequence"], "ingestion.expected_sequence"),
        actor=_text(payload["actor"], "ingestion.actor"),
        source=_text(payload["source"], "ingestion.source"),
        correlation_id=_text(payload["correlation_id"], "ingestion.correlation_id"),
        observations=observations,
        causation_id=_optional_text(payload["causation_id"], "ingestion.causation_id"),
        domain=_text(payload["domain"], "ingestion.domain"),
    )


def _decode_observation(value: object, index: int) -> ObservationInput:
    label = f"ingestion.observations[{index}]"
    payload = _object(value, _OBSERVATION_KEYS, label)
    claims = tuple(
        _decode_claim(item, label, claim_index)
        for claim_index, item in enumerate(_array(payload["claims"], f"{label}.claims"))
    )
    evidence = tuple(
        _decode_evidence(item, label, evidence_index)
        for evidence_index, item in enumerate(_array(payload["evidence"], f"{label}.evidence"))
    )
    return ObservationInput(
        observation_id=_text(payload["observation_id"], f"{label}.observation_id"),
        effective_at=_datetime(payload["effective_at"], f"{label}.effective_at"),
        claims=claims,
        evidence=evidence,
        idempotency_key=_optional_text(payload["idempotency_key"], f"{label}.idempotency_key"),
    )


def _decode_claim(value: object, observation_label: str, index: int) -> ObservedClaim:
    label = f"{observation_label}.claims[{index}]"
    payload = _object(value, _CLAIM_KEYS, label)
    expires_at = payload["expires_at"]
    return ObservedClaim(
        claim_id=_text(payload["claim_id"], f"{label}.claim_id"),
        subject=_text(payload["subject"], f"{label}.subject"),
        predicate=_text(payload["predicate"], f"{label}.predicate"),
        value=_scalar(payload["value"], f"{label}.value"),
        confidence=_number(payload["confidence"], f"{label}.confidence"),
        expires_at=(None if expires_at is None else _datetime(expires_at, f"{label}.expires_at")),
    )


def _decode_evidence(value: object, observation_label: str, index: int) -> EvidencePointer:
    label = f"{observation_label}.evidence[{index}]"
    payload = _object(value, _EVIDENCE_KEYS, label)
    return EvidencePointer(
        locator=_optional_text(payload["locator"], f"{label}.locator"),
        artifact_id=_optional_text(payload["artifact_id"], f"{label}.artifact_id"),
        digest=_optional_text(payload["digest"], f"{label}.digest"),
    )


def _decode_signal(value: object) -> DeriveSignalPacket:
    payload = _object(value, _SIGNAL_KEYS, "signal")
    return DeriveSignalPacket(
        purpose=_text(payload["purpose"], "signal.purpose"),
        generated_at=_datetime(payload["generated_at"], "signal.generated_at"),
        stale_after_seconds=_integer(payload["stale_after_seconds"], "signal.stale_after_seconds"),
    )


def _decode_retrieval(value: object) -> RetrieveEvidence:
    payload = _object(value, _RETRIEVAL_KEYS, "retrieval")
    required_keys = tuple(
        _decode_evidence_key(item, index)
        for index, item in enumerate(_array(payload["required_keys"], "retrieval.required_keys"))
    )
    return RetrieveEvidence(
        objective=_text(payload["objective"], "retrieval.objective"),
        required_keys=required_keys,
        max_results=_integer(payload["max_results"], "retrieval.max_results"),
    )


def _decode_evidence_key(value: object, index: int) -> EvidenceKey:
    label = f"retrieval.required_keys[{index}]"
    payload = _object(value, _EVIDENCE_KEY_KEYS, label)
    return EvidenceKey(
        _text(payload["subject"], f"{label}.subject"),
        _text(payload["predicate"], f"{label}.predicate"),
    )


def _decode_context(value: object) -> BuildContext:
    payload = _object(value, _CONTEXT_KEYS, "context")
    return BuildContext(
        task_id=_text(payload["task_id"], "context.task_id"),
        objective=_text(payload["objective"], "context.objective"),
        generated_at=_datetime(payload["generated_at"], "context.generated_at"),
        max_characters=_integer(payload["max_characters"], "context.max_characters"),
    )


def _decode_constraints(value: object) -> SolveConstraints:
    payload = _object(value, _CONSTRAINTS_KEYS, "constraints")
    definitions = tuple(
        _decode_constraint(item, index)
        for index, item in enumerate(_array(payload["definitions"], "constraints.definitions"))
    )
    return SolveConstraints(
        evaluated_at=_datetime(payload["evaluated_at"], "constraints.evaluated_at"),
        constraints=definitions,
    )


def _decode_constraint(value: object, index: int) -> ConstraintDefinition:
    label = f"constraints.definitions[{index}]"
    payload = _object(value, _CONSTRAINT_KEYS, label)
    max_age = payload["max_age_seconds"]
    return ConstraintDefinition(
        constraint_id=_text(payload["constraint_id"], f"{label}.constraint_id"),
        description=_text(payload["description"], f"{label}.description"),
        subject=_text(payload["subject"], f"{label}.subject"),
        predicate=_text(payload["predicate"], f"{label}.predicate"),
        operator=_enum(ConstraintOperator, payload["operator"], f"{label}.operator"),
        expected_values=tuple(
            _scalar(item, f"{label}.expected_values[{value_index}]")
            for value_index, item in enumerate(
                _array(payload["expected_values"], f"{label}.expected_values")
            )
        ),
        minimum_confidence=_number(payload["minimum_confidence"], f"{label}.minimum_confidence"),
        max_age_seconds=(
            None if max_age is None else _integer(max_age, f"{label}.max_age_seconds")
        ),
        schema_version=_text(payload["schema_version"], f"{label}.schema_version"),
    )


def _decode_evaluation_spec(value: object) -> EvaluationSpec:
    payload = _mapping(value, "evaluation_spec")
    try:
        return decode_evaluation_spec(canonical_json_bytes(payload))
    except EvaluationArtifactCodecError as error:
        raise DailyOperatorV2RequestCodecError(
            "evaluation_spec violates its owner artifact contract"
        ) from error


def _decode_gateway(value: object) -> DecisionRequirements:
    payload = _object(value, _GATEWAY_KEYS, "gateway_requirements")
    budget = _object(payload["budget"], _BUDGET_KEYS, "gateway_requirements.budget")
    return DecisionRequirements(
        request_id=_text(payload["request_id"], "gateway_requirements.request_id"),
        node_id=_text(payload["node_id"], "gateway_requirements.node_id"),
        capability=_enum(
            DecisionCapability,
            payload["capability"],
            "gateway_requirements.capability",
        ),
        classification=_enum(
            DecisionClassification,
            payload["classification"],
            "gateway_requirements.classification",
        ),
        locality=_enum(
            DecisionLocality,
            payload["locality"],
            "gateway_requirements.locality",
        ),
        budget=DecisionBudget(
            _integer(budget["max_input_tokens"], "gateway_requirements.budget.max_input_tokens"),
            _integer(
                budget["max_output_tokens"],
                "gateway_requirements.budget.max_output_tokens",
            ),
            _integer(budget["max_latency_ms"], "gateway_requirements.budget.max_latency_ms"),
            _integer(
                budget["max_cost_microusd"],
                "gateway_requirements.budget.max_cost_microusd",
            ),
        ),
        estimated_input_tokens=_integer(
            payload["estimated_input_tokens"],
            "gateway_requirements.estimated_input_tokens",
        ),
        deterministic_required=_boolean(
            payload["deterministic_required"],
            "gateway_requirements.deterministic_required",
        ),
        requested_at=_datetime(payload["requested_at"], "gateway_requirements.requested_at"),
    )


def _decode_authorization(value: object) -> AffordancePolicy:
    payload = _object(value, _AUTHORIZATION_KEYS, "authorization_affordance")
    _schema(
        payload,
        "schema_version",
        _AFFORDANCE_POLICY_SCHEMA_VERSION,
        "authorization_affordance",
    )
    return AffordancePolicy(
        name=_text(payload["name"], "authorization_affordance.name"),
        read_only=_boolean(payload["read_only"], "authorization_affordance.read_only"),
        external=_boolean(payload["external"], "authorization_affordance.external"),
        mutates_state=_boolean(payload["mutates_state"], "authorization_affordance.mutates_state"),
        evidence_action=_boolean(
            payload["evidence_action"], "authorization_affordance.evidence_action"
        ),
        allowed_arguments=_string_tuple(
            payload["allowed_arguments"], "authorization_affordance.allowed_arguments"
        ),
    )


def _decode_execution(value: object) -> AffordanceDefinition:
    payload = _object(value, _EXECUTION_KEYS, "execution_affordance")
    _schema(
        payload,
        "schema_version",
        _AFFORDANCE_DEFINITION_SCHEMA_VERSION,
        "execution_affordance",
    )
    arguments = tuple(
        _decode_argument_spec(item, index)
        for index, item in enumerate(_array(payload["arguments"], "execution_affordance.arguments"))
    )
    return AffordanceDefinition(
        name=_text(payload["name"], "execution_affordance.name"),
        adapter_id=_text(payload["adapter_id"], "execution_affordance.adapter_id"),
        side_effect_class=_enum(
            SideEffectClass,
            payload["side_effect_class"],
            "execution_affordance.side_effect_class",
        ),
        timeout_seconds=_number(payload["timeout_seconds"], "execution_affordance.timeout_seconds"),
        arguments=arguments,
    )


def _decode_argument_spec(value: object, index: int) -> AffordanceArgumentSpec:
    label = f"execution_affordance.arguments[{index}]"
    payload = _object(value, _ARGUMENT_SPEC_KEYS, label)
    return AffordanceArgumentSpec(
        name=_text(payload["name"], f"{label}.name"),
        required=_boolean(payload["required"], f"{label}.required"),
    )


def _decode_object(data: bytes) -> dict[str, object]:
    if not isinstance(data, bytes):
        raise TypeError("DailyOperatorV2Request artifact data must be bytes")
    try:
        decoded = json.loads(data.decode("utf-8"))
        encoded = canonical_json_bytes(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise DailyOperatorV2RequestCodecError(
            "DailyOperatorV2Request must be UTF-8 canonical JSON"
        ) from error
    if encoded != data:
        raise DailyOperatorV2RequestCodecError(
            "DailyOperatorV2Request must use canonical JSON encoding"
        )
    return _mapping(decoded, "DailyOperatorV2Request")


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise DailyOperatorV2RequestCodecError(f"{label} must be a JSON object")
    return cast("dict[str, object]", value)


def _object(value: object, expected: frozenset[str], label: str) -> dict[str, object]:
    payload = _mapping(value, label)
    _keys(payload, expected, label)
    return payload


def _keys(payload: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    actual = frozenset(payload)
    if actual != expected:
        raise DailyOperatorV2RequestCodecError(
            f"{label} fields differ; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise DailyOperatorV2RequestCodecError(f"{label} must be a JSON array")
    return cast("list[object]", value)


def _schema(
    payload: Mapping[str, object],
    key: str,
    expected: str,
    label: str,
) -> None:
    actual = _text(payload[key], f"{label}.{key}")
    if actual != expected:
        raise DailyOperatorV2RequestCodecError(
            f"unsupported {label} {key} {actual!r}; expected {expected!r}"
        )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DailyOperatorV2RequestCodecError(f"{label} must be a non-empty string")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise DailyOperatorV2RequestCodecError(f"{label} must be a boolean")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DailyOperatorV2RequestCodecError(f"{label} must be an integer")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DailyOperatorV2RequestCodecError(f"{label} must be numeric")
    return float(value)


def _scalar(value: object, label: str) -> JsonScalar:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    raise DailyOperatorV2RequestCodecError(f"{label} must be a JSON scalar")


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    return tuple(
        _text(item, f"{label}[{index}]") for index, item in enumerate(_array(value, label))
    )


def _datetime(value: object, label: str) -> datetime:
    try:
        result = datetime.fromisoformat(_text(value, label))
    except ValueError as error:
        raise DailyOperatorV2RequestCodecError(f"{label} must be an ISO timestamp") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise DailyOperatorV2RequestCodecError(f"{label} must be timezone-aware")
    return result.astimezone(UTC)


def _enum[EnumT: StrEnum](enum_type: type[EnumT], value: object, label: str) -> EnumT:
    try:
        return enum_type(_text(value, label))
    except ValueError as error:
        raise DailyOperatorV2RequestCodecError(f"{label} is not recognized") from error


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


__all__ = [
    "DAILY_OPERATOR_REQUEST_V2_MEDIA_TYPE",
    "DAILY_OPERATOR_REQUEST_V2_SCHEMA_VERSION",
    "DailyOperatorV2RequestCodecError",
    "daily_operator_v2_request_digest",
    "daily_operator_v2_request_identity_payload",
    "daily_operator_v2_request_payload",
    "decode_daily_operator_v2_request",
    "encode_daily_operator_v2_request",
]
