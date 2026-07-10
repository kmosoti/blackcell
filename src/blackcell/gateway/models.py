from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import cast

from blackcell.kernel import JsonValue
from blackcell.kernel._json import freeze_json


class ModelCapability(StrEnum):
    REASON = "reason"
    CODE = "code"
    REVIEW = "review"
    VERIFY = "verify"
    EMBED = "embed"


class DataClassification(IntEnum):
    PUBLIC = 0
    INTERNAL = 1
    PRIVATE = 2
    SECRET = 3


class LocalityPolicy(StrEnum):
    LOCAL_ONLY = "local-only"
    REMOTE_ALLOWED = "remote-allowed"


@dataclass(frozen=True, slots=True)
class GatewayBudget:
    max_input_tokens: int
    max_output_tokens: int
    max_latency_ms: int
    max_cost_microusd: int

    def __post_init__(self) -> None:
        if (
            min(
                self.max_input_tokens,
                self.max_output_tokens,
                self.max_latency_ms,
                self.max_cost_microusd,
            )
            < 0
        ):
            raise ValueError("gateway budgets must be non-negative")


@dataclass(frozen=True, slots=True)
class ModelRequest:
    request_id: str
    capability: ModelCapability
    input: Mapping[str, JsonValue]
    output_schema: Mapping[str, JsonValue]
    classification: DataClassification
    locality: LocalityPolicy
    budget: GatewayBudget
    estimated_input_tokens: int
    correlation_id: str
    run_id: str
    node_id: str
    deterministic_required: bool = False
    causation_id: str | None = None
    tools_allowed: bool = False

    def __post_init__(self) -> None:
        for name in ("request_id", "correlation_id", "run_id", "node_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.causation_id is not None and not self.causation_id.strip():
            raise ValueError("causation_id must not be blank")
        if self.estimated_input_tokens < 0:
            raise ValueError("estimated_input_tokens must be non-negative")
        if self.tools_allowed:
            raise ValueError("gateway models cannot receive direct tool authority")
        frozen_input = freeze_json(self.input, path="$.input")
        frozen_schema = freeze_json(self.output_schema, path="$.output_schema")
        object.__setattr__(self, "input", cast("Mapping[str, JsonValue]", frozen_input))
        object.__setattr__(self, "output_schema", cast("Mapping[str, JsonValue]", frozen_schema))


@dataclass(frozen=True, slots=True)
class AdapterResult:
    output: Mapping[str, JsonValue]
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_microusd: int
    deterministic: bool

    def __post_init__(self) -> None:
        if (
            min(
                self.input_tokens,
                self.output_tokens,
                self.latency_ms,
                self.cost_microusd,
            )
            < 0
        ):
            raise ValueError("adapter usage values must be non-negative")
        frozen = freeze_json(self.output, path="$.output")
        object.__setattr__(self, "output", cast("Mapping[str, JsonValue]", frozen))


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    profile_id: str
    adapter_id: str
    model_id: str
    capability: ModelCapability
    local: bool
    deterministic: bool


@dataclass(frozen=True, slots=True)
class ModelResponse:
    request_id: str
    output: Mapping[str, JsonValue]
    profile_id: str
    adapter_id: str
    model_id: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_microusd: int
    deterministic: bool
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class GatewayAuditRecord:
    request_id: str
    capability: ModelCapability
    classification: DataClassification
    profile_id: str
    adapter_id: str
    model_id: str
    correlation_id: str
    run_id: str
    node_id: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_microusd: int
    deterministic: bool


@dataclass(frozen=True, slots=True)
class GatewayResult:
    decision: RoutingDecision
    response: ModelResponse
