from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TypeGuard, cast

from blackcell.features.request_decision.command import (
    DECISION_REQUEST_SCHEMA_VERSION,
    RequestDecision,
    decision_request_payload,
)
from blackcell.features.request_decision.errors import DecisionOutputViolation
from blackcell.features.request_decision.models import (
    DecisionAffordance,
    DecisionArgument,
    DecisionArgumentSpec,
    DecisionAttempt,
    DecisionBudget,
    DecisionCapability,
    DecisionClassification,
    DecisionDiagnosticCode,
    DecisionFailure,
    DecisionFailureDiagnostic,
    DecisionFailureKind,
    DecisionLocality,
    DecisionProposal,
    DecisionRequirements,
    DecisionResponse,
    DecisionRoute,
    DecisionUsage,
    _attempt_payload,
    _failure_payload,
    _response_payload,
    _route_payload,
    _usage_payload,
)
from blackcell.kernel import JsonScalar, JsonValue
from blackcell.kernel._json import canonical_json_bytes, freeze_json

DECISION_REQUEST_MEDIA_TYPE = "application/vnd.blackcell.decision-request+json"
DECISION_ROUTE_MEDIA_TYPE = "application/vnd.blackcell.decision-route+json"
DECISION_ATTEMPT_MEDIA_TYPE = "application/vnd.blackcell.decision-attempt+json"
DECISION_RESPONSE_MEDIA_TYPE = "application/vnd.blackcell.decision-response+json"
DECISION_FAILURE_MEDIA_TYPE = "application/vnd.blackcell.decision-failure+json"
DECISION_USAGE_MEDIA_TYPE = "application/vnd.blackcell.decision-usage+json"

DECISION_ROUTE_SCHEMA_VERSION = "decision-route/v1"
DECISION_ATTEMPT_SCHEMA_VERSION = "decision-attempt/v1"
DECISION_RESPONSE_SCHEMA_VERSION = "decision-response/v1"
DECISION_FAILURE_SCHEMA_VERSION = "decision-failure/v1"
DECISION_FAILURE_SCHEMA_VERSION_V2 = "decision-failure/v2"
DECISION_USAGE_SCHEMA_VERSION = "decision-usage/v1"
DECISION_PROPOSAL_SCHEMA_VERSION = "decision-proposal/v1"

_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "request_id",
        "run_id",
        "node_id",
        "correlation_id",
        "causation_id",
        "context_frame_id",
        "objective",
        "context_payload",
        "evidence_event_ids",
        "affordances",
        "capability",
        "classification",
        "locality",
        "budget",
        "estimated_input_tokens",
        "deterministic_required",
        "requested_at",
        "tools_allowed",
        "model_input",
        "output_schema",
    }
)
_BUDGET_KEYS = frozenset(
    {
        "max_input_tokens",
        "max_output_tokens",
        "max_latency_ms",
        "max_cost_microusd",
    }
)
_AFFORDANCE_KEYS = frozenset({"name", "arguments"})
_ARGUMENT_SPEC_KEYS = frozenset({"name", "required"})
_ROUTE_KEYS = frozenset(
    {
        "schema_version",
        "profile_id",
        "adapter_id",
        "model_id",
        "capability",
        "local",
        "deterministic",
        "selected_at",
    }
)
_ATTEMPT_KEYS = frozenset(
    {
        "schema_version",
        "request_id",
        "request_digest",
        "route_id",
        "attempt_number",
        "started_at",
    }
)
_PROPOSAL_KEYS = frozenset(
    {
        "schema_version",
        "proposal_id",
        "context_frame_id",
        "affordance",
        "arguments",
        "rationale",
        "evidence_event_ids",
    }
)
_MODEL_OUTPUT_KEYS = _PROPOSAL_KEYS - {"schema_version"}
_ARGUMENT_KEYS = frozenset({"name", "value"})
_RESPONSE_KEYS = frozenset(
    {
        "schema_version",
        "request_id",
        "request_digest",
        "route_id",
        "attempt_id",
        "proposal",
        "completed_at",
    }
)
_FAILURE_V1_KEYS = frozenset(
    {
        "schema_version",
        "request_id",
        "request_digest",
        "kind",
        "code",
        "retryable",
        "failed_at",
        "route_id",
        "attempt_id",
        "exception_type",
    }
)
_FAILURE_V2_KEYS = _FAILURE_V1_KEYS | {"diagnostic"}
_DIAGNOSTIC_KEYS = frozenset({"code", "path", "rejected_output_digest"})
_USAGE_KEYS = frozenset(
    {
        "schema_version",
        "request_id",
        "attempt_id",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "cost_microusd",
        "deterministic",
    }
)


def encode_decision_request(request: RequestDecision) -> bytes:
    return canonical_json_bytes(decision_request_payload(request))


def decode_decision_request(
    value: str | bytes,
    *,
    expected_request_digest: str | None = None,
) -> RequestDecision:
    payload = _object(_decode(value), _REQUEST_KEYS, "decision request")
    _schema(payload, DECISION_REQUEST_SCHEMA_VERSION, "decision request")
    if payload["tools_allowed"] is not False:
        raise ValueError("decision request cannot grant model tool authority")
    budget = _object(payload["budget"], _BUDGET_KEYS, "decision request budget")
    affordances = _decode_affordances(payload["affordances"])
    request = RequestDecision(
        requirements=DecisionRequirements(
            request_id=_text(payload, "request_id"),
            node_id=_text(payload, "node_id"),
            capability=_enum(DecisionCapability, payload, "capability"),
            classification=_enum(DecisionClassification, payload, "classification"),
            locality=_enum(DecisionLocality, payload, "locality"),
            budget=DecisionBudget(
                _integer(budget, "max_input_tokens"),
                _integer(budget, "max_output_tokens"),
                _integer(budget, "max_latency_ms"),
                _integer(budget, "max_cost_microusd"),
            ),
            estimated_input_tokens=_integer(payload, "estimated_input_tokens"),
            deterministic_required=_boolean(payload, "deterministic_required"),
            requested_at=_datetime(payload, "requested_at"),
        ),
        run_id=_text(payload, "run_id"),
        correlation_id=_text(payload, "correlation_id"),
        causation_id=_text(payload, "causation_id"),
        context_frame_id=_text(payload, "context_frame_id"),
        objective=_text(payload, "objective"),
        context_payload=_string(payload, "context_payload"),
        evidence_event_ids=_string_tuple(payload, "evidence_event_ids"),
        affordances=affordances,
        schema_version=_text(payload, "schema_version"),
    )
    if freeze_json(payload["model_input"]) != request.model_input:
        raise ValueError("decision request model input does not match its typed fields")
    if freeze_json(payload["output_schema"]) != request.output_schema:
        raise ValueError("decision request output schema does not match its typed fields")
    _expected_digest(request.request_digest, expected_request_digest, "decision request")
    return request


def encode_decision_route(route: DecisionRoute) -> bytes:
    return canonical_json_bytes(_route_payload(route))


def decode_decision_route(
    value: str | bytes,
    *,
    expected_route_id: str | None = None,
) -> DecisionRoute:
    payload = _object(_decode(value), _ROUTE_KEYS, "decision route")
    _schema(payload, DECISION_ROUTE_SCHEMA_VERSION, "decision route")
    route = DecisionRoute(
        profile_id=_text(payload, "profile_id"),
        adapter_id=_text(payload, "adapter_id"),
        model_id=_text(payload, "model_id"),
        capability=_enum(DecisionCapability, payload, "capability"),
        local=_boolean(payload, "local"),
        deterministic=_boolean(payload, "deterministic"),
        selected_at=_datetime(payload, "selected_at"),
        schema_version=_text(payload, "schema_version"),
    )
    _expected_digest(route.route_id, expected_route_id, "decision route")
    return route


def encode_decision_attempt(attempt: DecisionAttempt) -> bytes:
    return canonical_json_bytes(_attempt_payload(attempt))


def decode_decision_attempt(
    value: str | bytes,
    *,
    expected_attempt_id: str | None = None,
) -> DecisionAttempt:
    payload = _object(_decode(value), _ATTEMPT_KEYS, "decision attempt")
    _schema(payload, DECISION_ATTEMPT_SCHEMA_VERSION, "decision attempt")
    attempt = DecisionAttempt(
        request_id=_text(payload, "request_id"),
        request_digest=_text(payload, "request_digest"),
        route_id=_text(payload, "route_id"),
        attempt_number=_integer(payload, "attempt_number"),
        started_at=_datetime(payload, "started_at"),
        schema_version=_text(payload, "schema_version"),
    )
    _expected_digest(attempt.attempt_id, expected_attempt_id, "decision attempt")
    return attempt


def decode_decision_output(
    output: Mapping[str, JsonValue],
    request: RequestDecision,
) -> DecisionProposal:
    payload = _object(output, _MODEL_OUTPUT_KEYS, "decision output")
    proposal = _decode_proposal_payload(
        {"schema_version": DECISION_PROPOSAL_SCHEMA_VERSION, **payload}
    )
    if proposal.context_frame_id != request.context_frame_id:
        raise DecisionOutputViolation(
            DecisionDiagnosticCode.CONTEXT_FRAME_MISMATCH,
            "$.context_frame_id",
        )
    affordances = {affordance.name: affordance for affordance in request.affordances}
    affordance = affordances.get(proposal.affordance)
    if affordance is None:
        raise DecisionOutputViolation(
            DecisionDiagnosticCode.UNDECLARED_AFFORDANCE,
            "$.affordance",
        ) from None
    declared = {argument.name: argument for argument in affordance.arguments}
    provided = {argument.name for argument in proposal.arguments}
    unexpected_index = next(
        (
            index
            for index, argument in enumerate(proposal.arguments)
            if argument.name not in declared
        ),
        None,
    )
    if unexpected_index is not None:
        raise DecisionOutputViolation(
            DecisionDiagnosticCode.UNDECLARED_ARGUMENT,
            f"$.arguments[{unexpected_index}].name",
        )
    if any(
        argument.required and argument.name not in provided for argument in affordance.arguments
    ):
        raise DecisionOutputViolation(
            DecisionDiagnosticCode.MISSING_REQUIRED_ARGUMENT,
            "$.arguments",
        )
    outside_index = next(
        (
            index
            for index, event_id in enumerate(proposal.evidence_event_ids)
            if event_id not in request.evidence_event_ids
        ),
        None,
    )
    if outside_index is not None:
        raise DecisionOutputViolation(
            DecisionDiagnosticCode.EVIDENCE_OUTSIDE_CONTEXT,
            f"$.evidence_event_ids[{outside_index}]",
        )
    return proposal


def encode_decision_response(response: DecisionResponse) -> bytes:
    return canonical_json_bytes(_response_payload(response))


def decode_decision_response(
    value: str | bytes,
    *,
    expected_response_id: str | None = None,
    request: RequestDecision | None = None,
) -> DecisionResponse:
    payload = _object(_decode(value), _RESPONSE_KEYS, "decision response")
    _schema(payload, DECISION_RESPONSE_SCHEMA_VERSION, "decision response")
    proposal_payload = _object(payload["proposal"], _PROPOSAL_KEYS, "decision proposal")
    proposal = _decode_proposal_payload(proposal_payload)
    if request is not None:
        proposal = decode_decision_output(
            cast(
                "Mapping[str, JsonValue]",
                {key: value for key, value in proposal_payload.items() if key != "schema_version"},
            ),
            request,
        )
    response = DecisionResponse(
        request_id=_text(payload, "request_id"),
        request_digest=_text(payload, "request_digest"),
        route_id=_text(payload, "route_id"),
        attempt_id=_text(payload, "attempt_id"),
        proposal=proposal,
        completed_at=_datetime(payload, "completed_at"),
        schema_version=_text(payload, "schema_version"),
    )
    if request is not None and (
        response.request_id != request.request_id
        or response.request_digest != request.request_digest
    ):
        raise ValueError("decision response belongs to a different request")
    _expected_digest(response.response_id, expected_response_id, "decision response")
    return response


def encode_decision_failure(failure: DecisionFailure) -> bytes:
    return canonical_json_bytes(_failure_payload(failure))


def decode_decision_failure(
    value: str | bytes,
    *,
    expected_failure_id: str | None = None,
) -> DecisionFailure:
    raw = _decode(value)
    if not isinstance(raw, Mapping):
        raise ValueError("decision failure has unexpected fields")
    schema_version = raw.get("schema_version")
    if schema_version == DECISION_FAILURE_SCHEMA_VERSION:
        payload = _object(raw, _FAILURE_V1_KEYS, "decision failure")
        diagnostic = None
    elif schema_version == DECISION_FAILURE_SCHEMA_VERSION_V2:
        payload = _object(raw, _FAILURE_V2_KEYS, "decision failure")
        diagnostic_payload = _object(
            payload["diagnostic"],
            _DIAGNOSTIC_KEYS,
            "decision failure diagnostic",
        )
        diagnostic = DecisionFailureDiagnostic(
            code=_enum(DecisionDiagnosticCode, diagnostic_payload, "code"),
            path=_text(diagnostic_payload, "path"),
            rejected_output_digest=_text(
                diagnostic_payload,
                "rejected_output_digest",
            ),
        )
    else:
        raise ValueError(f"unsupported decision failure schema {schema_version!r}")
    failure = DecisionFailure(
        request_id=_text(payload, "request_id"),
        request_digest=_text(payload, "request_digest"),
        kind=_enum(DecisionFailureKind, payload, "kind"),
        code=_text(payload, "code"),
        retryable=_boolean(payload, "retryable"),
        failed_at=_datetime(payload, "failed_at"),
        route_id=_optional_text(payload, "route_id"),
        attempt_id=_optional_text(payload, "attempt_id"),
        exception_type=_optional_text(payload, "exception_type"),
        diagnostic=diagnostic,
        schema_version=_text(payload, "schema_version"),
    )
    _expected_digest(failure.failure_id, expected_failure_id, "decision failure")
    return failure


def encode_decision_usage(usage: DecisionUsage) -> bytes:
    return canonical_json_bytes(_usage_payload(usage))


def decode_decision_usage(
    value: str | bytes,
    *,
    expected_usage_id: str | None = None,
) -> DecisionUsage:
    payload = _object(_decode(value), _USAGE_KEYS, "decision usage")
    _schema(payload, DECISION_USAGE_SCHEMA_VERSION, "decision usage")
    usage = DecisionUsage(
        request_id=_text(payload, "request_id"),
        attempt_id=_text(payload, "attempt_id"),
        input_tokens=_integer(payload, "input_tokens"),
        output_tokens=_integer(payload, "output_tokens"),
        latency_ms=_integer(payload, "latency_ms"),
        cost_microusd=_integer(payload, "cost_microusd"),
        deterministic=_boolean(payload, "deterministic"),
        schema_version=_text(payload, "schema_version"),
    )
    _expected_digest(usage.usage_id, expected_usage_id, "decision usage")
    return usage


def _decode_proposal_payload(payload: Mapping[str, object]) -> DecisionProposal:
    _schema(payload, DECISION_PROPOSAL_SCHEMA_VERSION, "decision proposal")
    arguments_value = payload["arguments"]
    if not _array(arguments_value):
        raise ValueError("decision proposal arguments must be an array")
    arguments = tuple(
        DecisionArgument(
            _text(argument, "name"),
            _json_scalar(argument, "value"),
        )
        for item in arguments_value
        for argument in (_object(item, _ARGUMENT_KEYS, "decision argument"),)
    )
    return DecisionProposal(
        proposal_id=_text(payload, "proposal_id"),
        context_frame_id=_text(payload, "context_frame_id"),
        affordance=_text(payload, "affordance"),
        arguments=arguments,
        rationale=_text(payload, "rationale"),
        evidence_event_ids=_string_tuple(payload, "evidence_event_ids"),
        schema_version=_text(payload, "schema_version"),
    )


def _decode_affordances(value: object) -> tuple[DecisionAffordance, ...]:
    if not _array(value):
        raise ValueError("decision request affordances must be an array")
    affordances: list[DecisionAffordance] = []
    for item in value:
        payload = _object(item, _AFFORDANCE_KEYS, "decision affordance")
        arguments_value = payload["arguments"]
        if not _array(arguments_value):
            raise ValueError("decision affordance arguments must be an array")
        arguments = tuple(
            DecisionArgumentSpec(
                _text(argument, "name"),
                _boolean(argument, "required"),
            )
            for argument_item in arguments_value
            for argument in (_object(argument_item, _ARGUMENT_SPEC_KEYS, "decision argument spec"),)
        )
        affordances.append(DecisionAffordance(_text(payload, "name"), arguments))
    return tuple(affordances)


def _decode(value: str | bytes) -> object:
    try:
        return json.loads(value)
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise ValueError("decision artifact must be valid JSON") from error


def _object(value: object, keys: frozenset[str], label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{label} has unexpected fields")
    if any(not isinstance(key, str) for key in value):  # pragma: no cover - JSON decoder
        raise ValueError(f"{label} keys must be strings")
    return {str(key): item for key, item in value.items()}


def _array(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)


def _text(payload: Mapping[str, object], field: str) -> str:
    value = payload[field]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _string(payload: Mapping[str, object], field: str) -> str:
    value = payload[field]
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _optional_text(payload: Mapping[str, object], field: str) -> str | None:
    value = payload[field]
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string or null")
    return value


def _integer(payload: Mapping[str, object], field: str) -> int:
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _boolean(payload: Mapping[str, object], field: str) -> bool:
    value = payload[field]
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _datetime(payload: Mapping[str, object], field: str) -> datetime:
    value = _text(payload, field)
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return timestamp


def _string_tuple(payload: Mapping[str, object], field: str) -> tuple[str, ...]:
    value = payload[field]
    if not _array(value) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a string array")
    return tuple(cast("str", item) for item in value)


def _json_scalar(payload: Mapping[str, object], field: str) -> JsonScalar:
    value = payload[field]
    if value is not None and not isinstance(value, bool | int | float | str):
        raise ValueError(f"{field} must be a JSON scalar")
    frozen = freeze_json(value, path=f"$.{field}")
    if isinstance(frozen, Mapping | tuple):  # pragma: no cover - guarded above
        raise ValueError(f"{field} must be a JSON scalar")
    return cast("JsonScalar", frozen)


def _enum(enum_type, payload: Mapping[str, object], field: str):
    value = _text(payload, field)
    try:
        return enum_type(value)
    except ValueError as error:
        raise ValueError(f"{field} is not recognized") from error


def _schema(payload: Mapping[str, object], expected: str, label: str) -> None:
    actual = _text(payload, "schema_version")
    if actual != expected:
        raise ValueError(f"unsupported {label} schema {actual!r}")


def _expected_digest(actual: str, expected: str | None, label: str) -> None:
    if expected is not None and actual != expected:
        raise ValueError(f"{label} identity does not match its canonical content")


__all__ = [
    "DECISION_ATTEMPT_MEDIA_TYPE",
    "DECISION_ATTEMPT_SCHEMA_VERSION",
    "DECISION_FAILURE_MEDIA_TYPE",
    "DECISION_FAILURE_SCHEMA_VERSION",
    "DECISION_FAILURE_SCHEMA_VERSION_V2",
    "DECISION_PROPOSAL_SCHEMA_VERSION",
    "DECISION_REQUEST_MEDIA_TYPE",
    "DECISION_RESPONSE_MEDIA_TYPE",
    "DECISION_RESPONSE_SCHEMA_VERSION",
    "DECISION_ROUTE_MEDIA_TYPE",
    "DECISION_ROUTE_SCHEMA_VERSION",
    "DECISION_USAGE_MEDIA_TYPE",
    "DECISION_USAGE_SCHEMA_VERSION",
    "decode_decision_attempt",
    "decode_decision_failure",
    "decode_decision_output",
    "decode_decision_request",
    "decode_decision_response",
    "decode_decision_route",
    "decode_decision_usage",
    "encode_decision_attempt",
    "encode_decision_failure",
    "encode_decision_request",
    "encode_decision_response",
    "encode_decision_route",
    "encode_decision_usage",
]
