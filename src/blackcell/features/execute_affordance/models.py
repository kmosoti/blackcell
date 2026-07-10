from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from blackcell.kernel import JsonScalar
from blackcell.kernel._json import json_digest


class SideEffectClass(StrEnum):
    READ_ONLY = "read-only"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


class ExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AffordanceArgument:
    name: str
    value: JsonScalar


@dataclass(frozen=True, slots=True)
class AffordanceArgumentSpec:
    name: str
    required: bool = True

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("argument specification name must not be empty")


@dataclass(frozen=True, slots=True)
class AffordanceDefinition:
    name: str
    adapter_id: str
    side_effect_class: SideEffectClass
    timeout_seconds: float
    arguments: tuple[AffordanceArgumentSpec, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.adapter_id.strip():
            raise ValueError("affordance name and adapter id must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("affordance timeout must be positive")
        names = tuple(item.name for item in self.arguments)
        if len(names) != len(set(names)):
            raise ValueError("affordance argument names must be unique")


@dataclass(frozen=True, slots=True)
class AffordanceInvocation:
    invocation_id: str
    proposal_id: str
    affordance: str
    arguments: tuple[AffordanceArgument, ...]
    idempotency_key: str
    requested_at: datetime

    def __post_init__(self) -> None:
        for name in ("invocation_id", "proposal_id", "affordance", "idempotency_key"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.requested_at.tzinfo is None or self.requested_at.utcoffset() is None:
            raise ValueError("requested_at must be timezone-aware")
        names = tuple(item.name for item in self.arguments)
        if len(names) != len(set(names)):
            raise ValueError("invocation argument names must be unique")


@dataclass(frozen=True, slots=True)
class ObservedEffect:
    subject: str
    predicate: str
    value: JsonScalar


@dataclass(frozen=True, slots=True)
class AdapterOutcome:
    success: bool
    output_digest: str
    completed_at: datetime
    observed_effects: tuple[ObservedEffect, ...] = ()
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not self.output_digest.strip():
            raise ValueError("output_digest must not be empty")
        if self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None:
            raise ValueError("completed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    invocation_id: str
    proposal_id: str
    affordance: str
    adapter_id: str
    idempotency_key: str
    execution_identity_digest: str
    status: ExecutionStatus
    started_at: datetime
    completed_at: datetime
    output_digest: str | None
    observed_effects: tuple[ObservedEffect, ...]
    error_code: str | None
    reconciled: bool
    schema_version: str = "execution-result/v2"
    result_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "result_id",
            json_digest(
                {
                    "schema_version": self.schema_version,
                    "invocation_id": self.invocation_id,
                    "proposal_id": self.proposal_id,
                    "affordance": self.affordance,
                    "adapter_id": self.adapter_id,
                    "idempotency_key": self.idempotency_key,
                    "execution_identity_digest": self.execution_identity_digest,
                    "status": self.status.value,
                    "started_at": self.started_at.isoformat(),
                    "completed_at": self.completed_at.isoformat(),
                    "output_digest": self.output_digest,
                    "error_code": self.error_code,
                    "reconciled": self.reconciled,
                }
            ),
        )
