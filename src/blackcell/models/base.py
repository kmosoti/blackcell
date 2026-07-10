from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, runtime_checkable

from blackcell.control import (
    ActionArgument,
    ActionProposal,
    ClaimRequirement,
    ExpectedEffect,
    ProposedAssertion,
)

JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject = dict[str, JsonValue]

ACTION_PROPOSAL_SCHEMA: JsonObject = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "proposal_id",
        "context_frame_id",
        "affordance",
        "arguments",
        "expected_effects",
        "rationale",
        "required_evidence",
        "evidence_ids",
        "assertions",
    ],
    "properties": {
        "proposal_id": {"type": "string", "minLength": 1},
        "context_frame_id": {"type": "string", "minLength": 1},
        "affordance": {"type": "string", "minLength": 1},
        "arguments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "value"],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "value": {"type": ["string", "number", "integer", "boolean", "null"]},
                },
            },
        },
        "expected_effects": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["subject", "predicate", "value"],
                "properties": {
                    "subject": {"type": "string", "minLength": 1},
                    "predicate": {"type": "string", "minLength": 1},
                    "value": {"type": ["string", "number", "integer", "boolean", "null"]},
                },
            },
        },
        "rationale": {"type": "string", "minLength": 1},
        "required_evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["subject", "predicate", "max_age_seconds", "allow_unknown"],
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "max_age_seconds": {"type": ["integer", "null"], "minimum": 0},
                    "allow_unknown": {"type": "boolean"},
                },
            },
        },
        "evidence_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "assertions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "evidence_ids"],
                "properties": {
                    "text": {"type": "string", "minLength": 1},
                    "evidence_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
        "schema_version": {"type": "string", "const": "action-proposal/v1"},
    },
}


def action_proposal_from_mapping(value: Mapping[str, Any]) -> ActionProposal:
    """Strictly decode provider JSON into the canonical control proposal."""

    proposal_id = _required_string(value, "proposal_id")
    context_frame_id = _required_string(value, "context_frame_id")
    affordance = _required_string(value, "affordance")
    rationale = _required_string(value, "rationale")
    schema_version = value.get("schema_version", "action-proposal/v1")
    if schema_version != "action-proposal/v1":
        raise ProposalParseError("unsupported action proposal schema_version")

    arguments = tuple(
        ActionArgument(_required_string(item, "name"), _scalar(item.get("value"), "value"))
        for item in _object_array(value.get("arguments"), "arguments")
    )
    expected_effects = tuple(
        ExpectedEffect(
            _required_string(item, "subject"),
            _required_string(item, "predicate"),
            _scalar(item.get("value"), "value"),
        )
        for item in _object_array(value.get("expected_effects"), "expected_effects")
    )
    required_evidence = tuple(
        _claim_requirement(item)
        for item in _object_array(value.get("required_evidence"), "required_evidence")
    )
    evidence_ids = _string_tuple(value.get("evidence_ids"), "evidence_ids")
    assertions = tuple(
        ProposedAssertion(
            _required_string(item, "text"),
            _string_tuple(item.get("evidence_ids"), "assertions.evidence_ids"),
        )
        for item in _object_array(value.get("assertions"), "assertions")
    )
    try:
        return ActionProposal(
            proposal_id=proposal_id,
            context_frame_id=context_frame_id,
            affordance=affordance,
            arguments=arguments,
            expected_effects=expected_effects,
            rationale=rationale,
            required_evidence=required_evidence,
            evidence_ids=evidence_ids,
            assertions=assertions,
            schema_version=schema_version,
        )
    except ValueError as error:
        raise ProposalParseError(str(error)) from error


def action_proposal_to_mapping(proposal: ActionProposal) -> JsonObject:
    return {
        "proposal_id": proposal.proposal_id,
        "context_frame_id": proposal.context_frame_id,
        "affordance": proposal.affordance,
        "arguments": [
            {"name": argument.name, "value": argument.value} for argument in proposal.arguments
        ],
        "expected_effects": [
            {"subject": effect.subject, "predicate": effect.predicate, "value": effect.value}
            for effect in proposal.expected_effects
        ],
        "rationale": proposal.rationale,
        "required_evidence": [
            {
                "subject": requirement.subject,
                "predicate": requirement.predicate,
                "max_age_seconds": requirement.max_age_seconds,
                "allow_unknown": requirement.allow_unknown,
            }
            for requirement in proposal.required_evidence
        ],
        "evidence_ids": list(proposal.evidence_ids),
        "assertions": [
            {"text": assertion.text, "evidence_ids": list(assertion.evidence_ids)}
            for assertion in proposal.assertions
        ],
        "schema_version": proposal.schema_version,
    }


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ModelInvocation:
    provider: str
    model: str | None
    invocation_id: str
    replayed: bool
    duration_ms: float
    configuration: Mapping[str, JsonValue] = field(default_factory=dict)
    response_metadata: Mapping[str, JsonValue] = field(default_factory=dict)
    usage: ModelUsage = field(default_factory=ModelUsage)


ProposalT = TypeVar("ProposalT", covariant=True)


@dataclass(frozen=True, slots=True)
class DecisionResult[ProposalT]:
    proposal: ProposalT
    invocation: ModelInvocation


class ModelError(RuntimeError):
    """Base error for a decision-model boundary."""


class UnknownRecordingError(ModelError):
    pass


class ModelExecutionError(ModelError):
    pass


class ModelTimeoutError(ModelExecutionError):
    pass


class ProposalParseError(ModelError, ValueError):
    pass


ProposalParser = Callable[[Mapping[str, Any]], ProposalT]


@runtime_checkable
class DecisionModel[ProposalT](Protocol):
    """A model that proposes inert actions from a serialized context frame."""

    @property
    def name(self) -> str: ...

    def decide(
        self,
        context_frame: Mapping[str, Any],
        *,
        output_schema: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> DecisionResult[ProposalT]: ...


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
        raise ProposalParseError(f"{field_name} must be an array of strings")
    if not all(isinstance(item, str) and item for item in value):
        raise ProposalParseError(f"{field_name} must contain non-empty strings")
    return tuple(value)


def _object_array(value: Any, field_name: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
        raise ProposalParseError(f"{field_name} must be an array of objects")
    if not all(isinstance(item, Mapping) for item in value):
        raise ProposalParseError(f"{field_name} must contain only objects")
    return tuple(value)


def _required_string(value: Mapping[str, Any], field_name: str) -> str:
    item = value.get(field_name)
    if not isinstance(item, str) or not item.strip():
        raise ProposalParseError(f"{field_name} must be a non-empty string")
    return item.strip()


def _scalar(value: Any, field_name: str) -> JsonScalar:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ProposalParseError(f"{field_name} must be a JSON scalar")


def _claim_requirement(value: Mapping[str, Any]) -> ClaimRequirement:
    max_age_seconds = value.get("max_age_seconds")
    if max_age_seconds is not None and (
        not isinstance(max_age_seconds, int)
        or isinstance(max_age_seconds, bool)
        or max_age_seconds < 0
    ):
        raise ProposalParseError("max_age_seconds must be a non-negative integer or null")
    allow_unknown = value.get("allow_unknown")
    if not isinstance(allow_unknown, bool):
        raise ProposalParseError("allow_unknown must be a boolean")
    return ClaimRequirement(
        _required_string(value, "subject"),
        _required_string(value, "predicate"),
        max_age_seconds,
        allow_unknown,
    )


def _json_object(value: Mapping[str, Any], *, field_name: str) -> JsonObject:
    result: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ProposalParseError(f"{field_name} keys must be strings")
        result[key] = _json_value(item, field_name=f"{field_name}.{key}")
    return result


def _json_value(value: Any, *, field_name: str) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return _json_object(value, field_name=field_name)
    if isinstance(value, (list, tuple)):
        return [_json_value(item, field_name=field_name) for item in value]
    raise ProposalParseError(f"{field_name} is not JSON serializable")
