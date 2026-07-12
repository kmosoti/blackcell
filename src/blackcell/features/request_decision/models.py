from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from blackcell.kernel import JsonScalar, JsonValue
from blackcell.kernel._json import freeze_json, json_digest

if TYPE_CHECKING:
    from blackcell.features.request_decision.command import RequestDecision

_SHA256_PREFIX = "sha256:"


class DecisionCapability(StrEnum):
    REASON = "reason"
    CODE = "code"
    REVIEW = "review"
    VERIFY = "verify"
    EMBED = "embed"


class DecisionClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    PRIVATE = "private"
    SECRET = "secret"


class DecisionLocality(StrEnum):
    LOCAL_ONLY = "local-only"
    REMOTE_ALLOWED = "remote-allowed"


class DecisionFailureKind(StrEnum):
    ADMISSION = "admission"
    ADAPTER = "adapter"
    TIMEOUT = "timeout"
    BUDGET = "budget"
    SCHEMA = "schema"
    INTEGRITY = "integrity"


@dataclass(frozen=True, slots=True)
class DecisionBudget:
    max_input_tokens: int
    max_output_tokens: int
    max_latency_ms: int
    max_cost_microusd: int

    def __post_init__(self) -> None:
        values = (
            self.max_input_tokens,
            self.max_output_tokens,
            self.max_latency_ms,
            self.max_cost_microusd,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise TypeError("decision budgets must be integers")
        if min(values) < 0:
            raise ValueError("decision budgets must be non-negative")


@dataclass(frozen=True, slots=True, order=True)
class DecisionArgumentSpec:
    name: str
    required: bool = True

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("decision argument name must not be empty")
        if not isinstance(self.required, bool):
            raise TypeError("decision argument required marker must be a boolean")


@dataclass(frozen=True, slots=True, order=True)
class DecisionAffordance:
    name: str
    arguments: tuple[DecisionArgumentSpec, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("decision affordance name must not be empty")
        ordered = tuple(sorted(self.arguments))
        names = tuple(argument.name for argument in ordered)
        if len(names) != len(set(names)):
            raise ValueError("decision affordance argument names must be unique")
        object.__setattr__(self, "arguments", ordered)


@dataclass(frozen=True, slots=True)
class DecisionRequirements:
    request_id: str
    node_id: str
    capability: DecisionCapability
    classification: DecisionClassification
    locality: DecisionLocality
    budget: DecisionBudget
    estimated_input_tokens: int
    deterministic_required: bool
    requested_at: datetime

    def __post_init__(self) -> None:
        for name in ("request_id", "node_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.capability, DecisionCapability):
            raise TypeError("capability must be a DecisionCapability")
        if not isinstance(self.classification, DecisionClassification):
            raise TypeError("classification must be a DecisionClassification")
        if not isinstance(self.locality, DecisionLocality):
            raise TypeError("locality must be a DecisionLocality")
        if isinstance(self.estimated_input_tokens, bool) or not isinstance(
            self.estimated_input_tokens, int
        ):
            raise TypeError("estimated_input_tokens must be an integer")
        if self.estimated_input_tokens < 0:
            raise ValueError("estimated_input_tokens must be non-negative")
        if not isinstance(self.deterministic_required, bool):
            raise TypeError("deterministic_required must be a boolean")
        object.__setattr__(
            self,
            "requested_at",
            _timestamp(self.requested_at, "requested_at"),
        )


@dataclass(frozen=True, slots=True)
class DecisionArgument:
    name: str
    value: JsonScalar

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("decision argument name must not be empty")
        freeze_json(self.value, path=f"$.arguments.{self.name}")


@dataclass(frozen=True, slots=True)
class DecisionProposal:
    proposal_id: str
    context_frame_id: str
    affordance: str
    arguments: tuple[DecisionArgument, ...]
    rationale: str
    evidence_event_ids: tuple[str, ...]
    schema_version: str = "decision-proposal/v1"
    proposal_digest: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "proposal_id",
            "context_frame_id",
            "affordance",
            "rationale",
            "schema_version",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        ordered_arguments = tuple(sorted(self.arguments, key=lambda item: item.name))
        names = tuple(argument.name for argument in ordered_arguments)
        if len(names) != len(set(names)):
            raise ValueError("decision proposal argument names must be unique")
        if any(not event_id.strip() for event_id in self.evidence_event_ids):
            raise ValueError("decision proposal evidence ids must not be blank")
        if len(self.evidence_event_ids) != len(set(self.evidence_event_ids)):
            raise ValueError("decision proposal evidence ids must be unique")
        object.__setattr__(self, "arguments", ordered_arguments)
        object.__setattr__(self, "proposal_digest", json_digest(_proposal_payload(self)))


@dataclass(frozen=True, slots=True)
class DecisionRoute:
    profile_id: str
    adapter_id: str
    model_id: str
    capability: DecisionCapability
    local: bool
    deterministic: bool
    selected_at: datetime
    schema_version: str = "decision-route/v1"
    route_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("profile_id", "adapter_id", "model_id", "schema_version"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.capability, DecisionCapability):
            raise TypeError("route capability must be a DecisionCapability")
        if not isinstance(self.local, bool) or not isinstance(self.deterministic, bool):
            raise TypeError("route locality and determinism markers must be booleans")
        object.__setattr__(self, "selected_at", _timestamp(self.selected_at, "selected_at"))
        object.__setattr__(self, "route_id", json_digest(_route_payload(self)))


@dataclass(frozen=True, slots=True)
class DecisionRequestRecord:
    request: RequestDecision
    request_artifact_digest: str
    registered_at: datetime

    def __post_init__(self) -> None:
        _validate_digest(self.request_artifact_digest, "request_artifact_digest")
        if self.request_artifact_digest != self.request.request_digest:
            raise ValueError("request artifact digest does not match the decision request")
        object.__setattr__(
            self,
            "registered_at",
            _timestamp(self.registered_at, "registered_at"),
        )


@dataclass(frozen=True, slots=True)
class DecisionPreparation:
    request_record: DecisionRequestRecord
    route: DecisionRoute
    route_artifact_digest: str
    prepared_at: datetime

    def __post_init__(self) -> None:
        request = self.request_record.request
        if self.route.capability != request.capability:
            raise ValueError("decision route capability does not match its request")
        if request.locality is DecisionLocality.LOCAL_ONLY and not self.route.local:
            raise ValueError("a local-only decision request cannot use a remote route")
        if request.deterministic_required and not self.route.deterministic:
            raise ValueError("a deterministic decision request requires a deterministic route")
        _validate_digest(self.route_artifact_digest, "route_artifact_digest")
        if self.route_artifact_digest != self.route.route_id:
            raise ValueError("route artifact digest does not match the selected route")
        prepared_at = _timestamp(self.prepared_at, "prepared_at")
        if prepared_at < self.request_record.registered_at or prepared_at < self.route.selected_at:
            raise ValueError("decision preparation cannot precede request registration or routing")
        object.__setattr__(self, "prepared_at", prepared_at)


@dataclass(frozen=True, slots=True)
class DecisionAttempt:
    request_id: str
    request_digest: str
    route_id: str
    attempt_number: int
    started_at: datetime
    schema_version: str = "decision-attempt/v1"
    attempt_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("request_id", "schema_version"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        _validate_digest(self.request_digest, "request_digest")
        _validate_digest(self.route_id, "route_id")
        if isinstance(self.attempt_number, bool) or not isinstance(self.attempt_number, int):
            raise TypeError("attempt_number must be an integer")
        if self.attempt_number < 1:
            raise ValueError("attempt_number must be positive")
        object.__setattr__(self, "started_at", _timestamp(self.started_at, "started_at"))
        object.__setattr__(self, "attempt_id", json_digest(_attempt_payload(self)))


@dataclass(frozen=True, slots=True)
class DecisionAttemptRecord:
    attempt: DecisionAttempt
    attempt_artifact_digest: str

    def __post_init__(self) -> None:
        _validate_digest(self.attempt_artifact_digest, "attempt_artifact_digest")
        if self.attempt_artifact_digest != self.attempt.attempt_id:
            raise ValueError("attempt artifact digest does not match the attempt")


@dataclass(frozen=True, slots=True)
class DecisionAttemptClaim:
    attempt_record: DecisionAttemptRecord
    fencing_revision: int
    claim_token: str
    invoked_at: datetime | None = None

    def __post_init__(self) -> None:
        if isinstance(self.fencing_revision, bool) or not isinstance(self.fencing_revision, int):
            raise TypeError("fencing_revision must be an integer")
        if self.fencing_revision < 1:
            raise ValueError("fencing_revision must be positive")
        if not self.claim_token.strip():
            raise ValueError("claim_token must not be empty")
        if self.invoked_at is not None:
            invoked_at = _timestamp(self.invoked_at, "invoked_at")
            if invoked_at < self.attempt_record.attempt.started_at:
                raise ValueError("invoked_at cannot precede attempt acquisition")
            object.__setattr__(self, "invoked_at", invoked_at)


@dataclass(frozen=True, slots=True)
class DecisionAdapterResult:
    output: Mapping[str, JsonValue]
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_microusd: int
    deterministic: bool
    completed_at: datetime

    def __post_init__(self) -> None:
        values = (
            self.input_tokens,
            self.output_tokens,
            self.latency_ms,
            self.cost_microusd,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise TypeError("decision adapter usage values must be integers")
        if min(values) < 0:
            raise ValueError("decision adapter usage values must be non-negative")
        if not isinstance(self.deterministic, bool):
            raise TypeError("decision adapter determinism marker must be a boolean")
        frozen = freeze_json(self.output, path="$.output")
        if not isinstance(frozen, Mapping):
            raise TypeError("decision adapter output must be an object")
        object.__setattr__(self, "output", cast("Mapping[str, JsonValue]", frozen))
        object.__setattr__(
            self,
            "completed_at",
            _timestamp(self.completed_at, "completed_at"),
        )


@dataclass(frozen=True, slots=True)
class DecisionUsage:
    request_id: str
    attempt_id: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_microusd: int
    deterministic: bool
    schema_version: str = "decision-usage/v1"
    usage_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("request_id", "schema_version"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        _validate_digest(self.attempt_id, "attempt_id")
        values = (
            self.input_tokens,
            self.output_tokens,
            self.latency_ms,
            self.cost_microusd,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise TypeError("decision usage values must be integers")
        if min(values) < 0:
            raise ValueError("decision usage values must be non-negative")
        if not isinstance(self.deterministic, bool):
            raise TypeError("decision usage determinism marker must be a boolean")
        object.__setattr__(self, "usage_id", json_digest(_usage_payload(self)))


@dataclass(frozen=True, slots=True)
class DecisionResponse:
    request_id: str
    request_digest: str
    route_id: str
    attempt_id: str
    proposal: DecisionProposal
    completed_at: datetime
    schema_version: str = "decision-response/v1"
    response_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("request_id", "schema_version"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        for name in ("request_digest", "route_id", "attempt_id"):
            _validate_digest(getattr(self, name), name)
        object.__setattr__(self, "completed_at", _timestamp(self.completed_at, "completed_at"))
        object.__setattr__(self, "response_id", json_digest(_response_payload(self)))


@dataclass(frozen=True, slots=True)
class DecisionFailure:
    request_id: str
    request_digest: str
    kind: DecisionFailureKind
    code: str
    retryable: bool
    failed_at: datetime
    route_id: str | None = None
    attempt_id: str | None = None
    exception_type: str | None = None
    schema_version: str = "decision-failure/v1"
    failure_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("request_id", "code", "schema_version"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        _validate_digest(self.request_digest, "request_digest")
        if not isinstance(self.kind, DecisionFailureKind):
            raise TypeError("failure kind must be a DecisionFailureKind")
        if not isinstance(self.retryable, bool):
            raise TypeError("failure retryable marker must be a boolean")
        if self.route_id is not None:
            _validate_digest(self.route_id, "route_id")
        if self.attempt_id is not None:
            _validate_digest(self.attempt_id, "attempt_id")
            if self.route_id is None:
                raise ValueError("an attempted decision failure requires a route")
        if self.exception_type is not None and not self.exception_type.strip():
            raise ValueError("exception_type must not be blank")
        object.__setattr__(self, "failed_at", _timestamp(self.failed_at, "failed_at"))
        object.__setattr__(self, "failure_id", json_digest(_failure_payload(self)))


@dataclass(frozen=True, slots=True)
class DecisionSuccessRecord:
    preparation: DecisionPreparation
    attempt_record: DecisionAttemptRecord
    response: DecisionResponse
    response_artifact_digest: str
    usage: DecisionUsage
    usage_artifact_digest: str

    def __post_init__(self) -> None:
        _validate_digest(self.response_artifact_digest, "response_artifact_digest")
        _validate_digest(self.usage_artifact_digest, "usage_artifact_digest")
        if self.response_artifact_digest != self.response.response_id:
            raise ValueError("response artifact digest does not match the response")
        if self.usage_artifact_digest != self.usage.usage_id:
            raise ValueError("usage artifact digest does not match the usage")
        _validate_terminal_binding(
            self.preparation,
            self.attempt_record,
            request_id=self.response.request_id,
            request_digest=self.response.request_digest,
            route_id=self.response.route_id,
            attempt_id=self.response.attempt_id,
        )
        if self.usage.request_id != self.response.request_id:
            raise ValueError("decision usage belongs to a different request")
        if self.usage.attempt_id != self.response.attempt_id:
            raise ValueError("decision usage belongs to a different attempt")


@dataclass(frozen=True, slots=True)
class DecisionFailureRecord:
    request_record: DecisionRequestRecord
    failure: DecisionFailure
    failure_artifact_digest: str
    preparation: DecisionPreparation | None = None
    attempt_record: DecisionAttemptRecord | None = None
    usage: DecisionUsage | None = None
    usage_artifact_digest: str | None = None

    def __post_init__(self) -> None:
        _validate_digest(self.failure_artifact_digest, "failure_artifact_digest")
        if self.failure_artifact_digest != self.failure.failure_id:
            raise ValueError("failure artifact digest does not match the failure")
        request = self.request_record.request
        if (
            self.failure.request_id != request.request_id
            or self.failure.request_digest != request.request_digest
        ):
            raise ValueError("decision failure belongs to a different request")
        if (self.preparation is None) != (self.failure.route_id is None):
            raise ValueError("decision failure route binding is incomplete")
        if (self.attempt_record is None) != (self.failure.attempt_id is None):
            raise ValueError("decision failure attempt binding is incomplete")
        if self.preparation is not None:
            if self.preparation.request_record != self.request_record:
                raise ValueError("decision failure preparation belongs to another request")
            if self.failure.route_id != self.preparation.route.route_id:
                raise ValueError("decision failure belongs to a different route")
        if self.attempt_record is not None and self.preparation is not None:
            _validate_terminal_binding(
                self.preparation,
                self.attempt_record,
                request_id=self.failure.request_id,
                request_digest=self.failure.request_digest,
                route_id=cast("str", self.failure.route_id),
                attempt_id=cast("str", self.failure.attempt_id),
            )
        if (self.usage is None) != (self.usage_artifact_digest is None):
            raise ValueError("decision failure usage artifact binding is incomplete")
        if self.usage is not None and self.usage_artifact_digest is not None:
            _validate_digest(self.usage_artifact_digest, "usage_artifact_digest")
            if self.usage_artifact_digest != self.usage.usage_id:
                raise ValueError("usage artifact digest does not match the usage")
            if self.usage.request_id != self.failure.request_id:
                raise ValueError("decision failure usage belongs to a different request")
            if self.usage.attempt_id != self.failure.attempt_id:
                raise ValueError("decision failure usage belongs to a different attempt")


type DecisionTerminalRecord = DecisionSuccessRecord | DecisionFailureRecord


def _timestamp(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _validate_digest(value: str, field_name: str) -> None:
    hexadecimal = value.removeprefix(_SHA256_PREFIX)
    if not value.startswith(_SHA256_PREFIX) or len(hexadecimal) != 64:
        raise ValueError(f"{field_name} must be a SHA-256 digest")
    try:
        int(hexadecimal, 16)
    except ValueError as error:
        raise ValueError(f"{field_name} must be a SHA-256 digest") from error


def _proposal_payload(proposal: DecisionProposal) -> dict[str, object]:
    return {
        "schema_version": proposal.schema_version,
        "proposal_id": proposal.proposal_id,
        "context_frame_id": proposal.context_frame_id,
        "affordance": proposal.affordance,
        "arguments": [
            {"name": argument.name, "value": argument.value} for argument in proposal.arguments
        ],
        "rationale": proposal.rationale,
        "evidence_event_ids": list(proposal.evidence_event_ids),
    }


def _route_payload(route: DecisionRoute) -> dict[str, object]:
    return {
        "schema_version": route.schema_version,
        "profile_id": route.profile_id,
        "adapter_id": route.adapter_id,
        "model_id": route.model_id,
        "capability": route.capability.value,
        "local": route.local,
        "deterministic": route.deterministic,
        "selected_at": route.selected_at.isoformat(),
    }


def _attempt_payload(attempt: DecisionAttempt) -> dict[str, object]:
    return {
        "schema_version": attempt.schema_version,
        "request_id": attempt.request_id,
        "request_digest": attempt.request_digest,
        "route_id": attempt.route_id,
        "attempt_number": attempt.attempt_number,
        "started_at": attempt.started_at.isoformat(),
    }


def _usage_payload(usage: DecisionUsage) -> dict[str, object]:
    return {
        "schema_version": usage.schema_version,
        "request_id": usage.request_id,
        "attempt_id": usage.attempt_id,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "latency_ms": usage.latency_ms,
        "cost_microusd": usage.cost_microusd,
        "deterministic": usage.deterministic,
    }


def _response_payload(response: DecisionResponse) -> dict[str, object]:
    return {
        "schema_version": response.schema_version,
        "request_id": response.request_id,
        "request_digest": response.request_digest,
        "route_id": response.route_id,
        "attempt_id": response.attempt_id,
        "proposal": _proposal_payload(response.proposal),
        "completed_at": response.completed_at.isoformat(),
    }


def _failure_payload(failure: DecisionFailure) -> dict[str, object]:
    return {
        "schema_version": failure.schema_version,
        "request_id": failure.request_id,
        "request_digest": failure.request_digest,
        "kind": failure.kind.value,
        "code": failure.code,
        "retryable": failure.retryable,
        "failed_at": failure.failed_at.isoformat(),
        "route_id": failure.route_id,
        "attempt_id": failure.attempt_id,
        "exception_type": failure.exception_type,
    }


def _validate_terminal_binding(
    preparation: DecisionPreparation,
    attempt_record: DecisionAttemptRecord,
    *,
    request_id: str,
    request_digest: str,
    route_id: str,
    attempt_id: str,
) -> None:
    attempt = attempt_record.attempt
    request = preparation.request_record.request
    expected_request_id = request.request_id
    expected_request_digest = request.request_digest
    if request_id != expected_request_id or request_digest != expected_request_digest:
        raise ValueError("decision terminal record belongs to a different request")
    if route_id != preparation.route.route_id or attempt.route_id != route_id:
        raise ValueError("decision terminal record belongs to a different route")
    if attempt_id != attempt.attempt_id:
        raise ValueError("decision terminal record belongs to a different attempt")
    if attempt.request_id != request_id or attempt.request_digest != request_digest:
        raise ValueError("decision attempt belongs to a different request")


__all__ = [
    "DecisionAdapterResult",
    "DecisionAffordance",
    "DecisionArgument",
    "DecisionArgumentSpec",
    "DecisionAttempt",
    "DecisionAttemptClaim",
    "DecisionAttemptRecord",
    "DecisionBudget",
    "DecisionCapability",
    "DecisionClassification",
    "DecisionFailure",
    "DecisionFailureKind",
    "DecisionFailureRecord",
    "DecisionLocality",
    "DecisionPreparation",
    "DecisionProposal",
    "DecisionRequestRecord",
    "DecisionRequirements",
    "DecisionResponse",
    "DecisionRoute",
    "DecisionSuccessRecord",
    "DecisionTerminalRecord",
    "DecisionUsage",
]
