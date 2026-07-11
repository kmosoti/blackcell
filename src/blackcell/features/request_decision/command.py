from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

from blackcell.features.request_decision.models import (
    DecisionAffordance,
    DecisionBudget,
    DecisionCapability,
    DecisionClassification,
    DecisionLocality,
    DecisionRequirements,
)
from blackcell.kernel import JsonInput, JsonValue
from blackcell.kernel._json import freeze_json, json_digest

DECISION_REQUEST_SCHEMA_VERSION = "decision-request/v1"


@dataclass(frozen=True, slots=True)
class RequestDecision:
    requirements: DecisionRequirements
    run_id: str
    correlation_id: str
    causation_id: str
    context_frame_id: str
    objective: str
    context_payload: str
    evidence_event_ids: tuple[str, ...]
    affordances: tuple[DecisionAffordance, ...]
    schema_version: str = DECISION_REQUEST_SCHEMA_VERSION
    request_digest: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "correlation_id",
            "causation_id",
            "context_frame_id",
            "objective",
            "schema_version",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.schema_version != DECISION_REQUEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported decision request schema {self.schema_version!r}")
        if any(not event_id.strip() for event_id in self.evidence_event_ids):
            raise ValueError("decision request evidence ids must not be blank")
        if len(self.evidence_event_ids) != len(set(self.evidence_event_ids)):
            raise ValueError("decision request evidence ids must be unique")
        ordered_affordances = tuple(sorted(self.affordances))
        if not ordered_affordances:
            raise ValueError("decision request requires at least one affordance")
        names = tuple(affordance.name for affordance in ordered_affordances)
        if len(names) != len(set(names)):
            raise ValueError("decision request affordance names must be unique")
        object.__setattr__(self, "affordances", ordered_affordances)
        object.__setattr__(self, "request_digest", json_digest(decision_request_payload(self)))

    @property
    def request_id(self) -> str:
        return self.requirements.request_id

    @property
    def node_id(self) -> str:
        return self.requirements.node_id

    @property
    def capability(self) -> DecisionCapability:
        return self.requirements.capability

    @property
    def classification(self) -> DecisionClassification:
        return self.requirements.classification

    @property
    def locality(self) -> DecisionLocality:
        return self.requirements.locality

    @property
    def budget(self) -> DecisionBudget:
        return self.requirements.budget

    @property
    def estimated_input_tokens(self) -> int:
        return self.requirements.estimated_input_tokens

    @property
    def deterministic_required(self) -> bool:
        return self.requirements.deterministic_required

    @property
    def requested_at(self) -> datetime:
        return self.requirements.requested_at

    @property
    def model_input(self) -> Mapping[str, JsonValue]:
        value = freeze_json(
            {
                "context_frame_id": self.context_frame_id,
                "objective": self.objective,
                "context_payload": self.context_payload,
                "evidence_event_ids": self.evidence_event_ids,
                "affordances": tuple(
                    {
                        "name": affordance.name,
                        "arguments": tuple(
                            {
                                "name": argument.name,
                                "required": argument.required,
                            }
                            for argument in affordance.arguments
                        ),
                    }
                    for affordance in self.affordances
                ),
            },
            path="$.model_input",
        )
        if not isinstance(value, Mapping):  # pragma: no cover - constructed object invariant
            raise TypeError("decision model input must be an object")
        return cast("Mapping[str, JsonValue]", value)

    @property
    def output_schema(self) -> Mapping[str, JsonValue]:
        affordance_schema: dict[str, JsonInput]
        if len(self.affordances) == 1:
            affordance_schema = {
                "type": "string",
                "const": self.affordances[0].name,
            }
        else:
            affordance_schema = {"type": "string", "minLength": 1}
        value = freeze_json(
            {
                "type": "object",
                "additionalProperties": False,
                "required": (
                    "proposal_id",
                    "context_frame_id",
                    "affordance",
                    "arguments",
                    "rationale",
                    "evidence_event_ids",
                ),
                "properties": {
                    "proposal_id": {"type": "string", "minLength": 1},
                    "context_frame_id": {
                        "type": "string",
                        "const": self.context_frame_id,
                    },
                    "affordance": affordance_schema,
                    "arguments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ("name", "value"),
                            "properties": {
                                "name": {"type": "string", "minLength": 1},
                                "value": {
                                    "type": (
                                        "null",
                                        "boolean",
                                        "integer",
                                        "number",
                                        "string",
                                    )
                                },
                            },
                        },
                    },
                    "rationale": {"type": "string", "minLength": 1},
                    "evidence_event_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
            path="$.output_schema",
        )
        if not isinstance(value, Mapping):  # pragma: no cover - constructed object invariant
            raise TypeError("decision output schema must be an object")
        return cast("Mapping[str, JsonValue]", value)


def decision_request_payload(request: RequestDecision) -> dict[str, JsonInput]:
    return {
        "schema_version": request.schema_version,
        "request_id": request.request_id,
        "run_id": request.run_id,
        "node_id": request.node_id,
        "correlation_id": request.correlation_id,
        "causation_id": request.causation_id,
        "context_frame_id": request.context_frame_id,
        "objective": request.objective,
        "context_payload": request.context_payload,
        "evidence_event_ids": list(request.evidence_event_ids),
        "affordances": [
            {
                "name": affordance.name,
                "arguments": [
                    {"name": argument.name, "required": argument.required}
                    for argument in affordance.arguments
                ],
            }
            for affordance in request.affordances
        ],
        "capability": request.capability.value,
        "classification": request.classification.value,
        "locality": request.locality.value,
        "budget": {
            "max_input_tokens": request.budget.max_input_tokens,
            "max_output_tokens": request.budget.max_output_tokens,
            "max_latency_ms": request.budget.max_latency_ms,
            "max_cost_microusd": request.budget.max_cost_microusd,
        },
        "estimated_input_tokens": request.estimated_input_tokens,
        "deterministic_required": request.deterministic_required,
        "requested_at": request.requested_at.isoformat(),
        "tools_allowed": False,
        "model_input": request.model_input,
        "output_schema": request.output_schema,
    }


__all__ = [
    "DECISION_REQUEST_SCHEMA_VERSION",
    "RequestDecision",
    "decision_request_payload",
]
