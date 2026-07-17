from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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


class GatewayFailureCode(StrEnum):
    REQUEST_INPUT_BUDGET_EXCEEDED = "request_input_budget_exceeded"
    NO_PROFILE = "no_profile"
    PREPARED_CALL_INVALID = "prepared_call_invalid"
    ADAPTER_INPUT_BUDGET_EXCEEDED = "adapter_input_budget_exceeded"
    PROFILE_INPUT_LIMIT_EXCEEDED = "profile_input_limit_exceeded"
    ADAPTER_OUTPUT_BUDGET_EXCEEDED = "adapter_output_budget_exceeded"
    PROFILE_OUTPUT_LIMIT_EXCEEDED = "profile_output_limit_exceeded"
    ADAPTER_LATENCY_BUDGET_EXCEEDED = "adapter_latency_budget_exceeded"
    ADAPTER_COST_BUDGET_EXCEEDED = "adapter_cost_budget_exceeded"
    PROFILE_COST_LIMIT_EXCEEDED = "profile_cost_limit_exceeded"
    PROFILE_DETERMINISM_VIOLATED = "profile_determinism_violated"
    REQUEST_DETERMINISM_VIOLATED = "request_determinism_violated"


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
class GatewayCompletion:
    """Content-free evidence that an admitted adapter call completed."""

    output_digest: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_microusd: int
    deterministic: bool
    completed_at: datetime

    def __post_init__(self) -> None:
        hexadecimal = self.output_digest.removeprefix("sha256:")
        if not self.output_digest.startswith("sha256:") or len(hexadecimal) != 64:
            raise ValueError("gateway completion output_digest must be a SHA-256 digest")
        try:
            int(hexadecimal, 16)
        except ValueError as error:
            raise ValueError("gateway completion output_digest must be a SHA-256 digest") from error
        values = (
            self.input_tokens,
            self.output_tokens,
            self.latency_ms,
            self.cost_microusd,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise TypeError("gateway completion usage values must be integers")
        if min(values) < 0:
            raise ValueError("gateway completion usage values must be non-negative")
        if not isinstance(self.deterministic, bool):
            raise TypeError("gateway completion determinism marker must be a boolean")
        if self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None:
            raise ValueError("gateway completion timestamp must be timezone-aware")
        object.__setattr__(self, "completed_at", self.completed_at.astimezone(UTC))


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


@dataclass(frozen=True, slots=True)
class PreparedGatewayCall:
    """Exact, policy-admitted route prepared without invoking a model adapter."""

    request: ModelRequest
    decision: RoutingDecision
    effective_budget: GatewayBudget

    def __post_init__(self) -> None:
        if self.decision.capability is not self.request.capability:
            raise ValueError("prepared route capability does not match its request")
        if self.request.locality is LocalityPolicy.LOCAL_ONLY and not self.decision.local:
            raise ValueError("a local-only request cannot prepare a remote route")
        if self.request.deterministic_required and not self.decision.deterministic:
            raise ValueError("a deterministic request cannot prepare a non-deterministic route")
        if self.effective_budget.max_input_tokens > self.request.budget.max_input_tokens:
            raise ValueError("prepared input budget exceeds the request budget")
        if self.effective_budget.max_output_tokens > self.request.budget.max_output_tokens:
            raise ValueError("prepared output budget exceeds the request budget")
        if self.effective_budget.max_latency_ms > self.request.budget.max_latency_ms:
            raise ValueError("prepared latency budget exceeds the request budget")
        if self.effective_budget.max_cost_microusd > self.request.budget.max_cost_microusd:
            raise ValueError("prepared cost budget exceeds the request budget")
