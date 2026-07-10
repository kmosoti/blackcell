from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from blackcell.domains.repository import OperationalStateEstimate, Scalar


class PolicyOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class AttemptStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ArgumentValueType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


@dataclass(frozen=True, slots=True)
class ActionArgument:
    name: str
    value: Scalar


@dataclass(frozen=True, slots=True)
class ExpectedEffect:
    subject: str
    predicate: str
    value: Scalar


@dataclass(frozen=True, slots=True)
class ProposedAssertion:
    """Model-authored prose kept distinct from an accepted structured state claim."""

    text: str
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("assertion text must be non-empty")
        _validate_evidence_ids(self.evidence_ids)


@dataclass(frozen=True, slots=True)
class ClaimRequirement:
    subject: str
    predicate: str
    max_age_seconds: int | None = None
    allow_unknown: bool = False

    def __post_init__(self) -> None:
        if self.max_age_seconds is not None and self.max_age_seconds < 0:
            raise ValueError("max_age_seconds must be non-negative")


@dataclass(frozen=True, slots=True)
class CheckRequirement:
    name: str
    passing_values: tuple[str, ...] = ("passed", "success")
    max_age_seconds: int | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("check requirement name must be non-empty")
        if not self.passing_values:
            raise ValueError("passing_values must be non-empty")
        if self.max_age_seconds is not None and self.max_age_seconds < 0:
            raise ValueError("max_age_seconds must be non-negative")


@dataclass(frozen=True, slots=True)
class Constraint:
    key: str
    description: str
    required_evidence: tuple[ClaimRequirement, ...] = ()
    required_checks: tuple[CheckRequirement, ...] = ()


@dataclass(frozen=True, slots=True)
class AffordanceArgumentSpec:
    name: str
    value_type: ArgumentValueType
    required: bool = True
    allowed_values: tuple[Scalar, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("affordance argument name must be non-empty")


@dataclass(frozen=True, slots=True)
class AffordanceDefinition:
    name: str
    description: str
    read_only: bool
    external: bool = False
    mutates_state: bool = False
    evidence_action: bool = False
    timeout_seconds: float = 10.0
    arguments: tuple[AffordanceArgumentSpec, ...] = ()
    effect_class: str = "observation"

    def __post_init__(self) -> None:
        if not self.name or not self.description:
            raise ValueError("affordance name and description must be non-empty")
        if self.timeout_seconds <= 0:
            raise ValueError("affordance timeout must be positive")
        if self.mutates_state and self.read_only:
            raise ValueError("a state-mutating affordance cannot be read-only")
        names = [argument.name for argument in self.arguments]
        if len(names) != len(set(names)):
            raise ValueError("affordance argument names must be unique")
        if not self.effect_class.strip():
            raise ValueError("affordance effect class must be non-empty")

    def signature(self) -> str:
        arguments = ", ".join(
            f"{item.name}{'' if item.required else '?'}:{item.value_type.value}"
            + (
                "=" + "|".join(str(value) for value in item.allowed_values)
                if item.allowed_values
                else ""
            )
            for item in self.arguments
        )
        mode = "read-only" if self.read_only else "approval-required"
        return f"{self.name}({arguments}) [{mode}; effect={self.effect_class}]"


@dataclass(frozen=True, slots=True)
class ActionProposal:
    proposal_id: str
    context_frame_id: str
    affordance: str
    arguments: tuple[ActionArgument, ...]
    expected_effects: tuple[ExpectedEffect, ...]
    rationale: str
    required_evidence: tuple[ClaimRequirement, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    assertions: tuple[ProposedAssertion, ...] = ()
    schema_version: str = "action-proposal/v1"

    def __post_init__(self) -> None:
        if not self.proposal_id or not self.context_frame_id or not self.affordance:
            raise ValueError("proposal id, context frame id, and affordance must be non-empty")
        if not self.rationale.strip():
            raise ValueError("proposal rationale must be non-empty")
        names = [argument.name for argument in self.arguments]
        if len(names) != len(set(names)):
            raise ValueError("proposal argument names must be unique")
        _validate_evidence_ids(self.evidence_ids)

    def argument(self, name: str, default: Scalar = None) -> Scalar:
        for argument in self.arguments:
            if argument.name == name:
                return argument.value
        return default


def validate_affordance_arguments(
    proposal: ActionProposal, definition: AffordanceDefinition
) -> tuple[tuple[str, str], ...]:
    specs = {item.name: item for item in definition.arguments}
    supplied = {item.name: item.value for item in proposal.arguments}
    violations: list[tuple[str, str]] = []
    for name in sorted(supplied.keys() - specs.keys()):
        violations.append(("unexpected_argument", f"argument {name!r} is not declared"))
    for name, spec in specs.items():
        if name not in supplied:
            if spec.required:
                violations.append(("missing_argument", f"argument {name!r} is required"))
            continue
        value = supplied[name]
        if not _argument_type_matches(value, spec.value_type):
            violations.append(
                (
                    "invalid_argument_type",
                    f"argument {name!r} must be {spec.value_type.value}",
                )
            )
        elif spec.allowed_values and value not in spec.allowed_values:
            violations.append(
                (
                    "invalid_argument_value",
                    f"argument {name!r} is not one of the developer-declared values",
                )
            )
    return tuple(violations)


@dataclass(frozen=True, slots=True)
class PolicyFinding:
    policy: str
    outcome: PolicyOutcome
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    proposal_id: str
    outcome: PolicyOutcome
    findings: tuple[PolicyFinding, ...]
    evaluated_at: datetime
    approval_granted: bool = False
    schema_version: str = "policy-decision/v1"
    decision_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_aware(self.evaluated_at)
        payload = {
            "proposal_id": self.proposal_id,
            "outcome": self.outcome.value,
            "findings": [
                [item.policy, item.outcome.value, item.code, item.message] for item in self.findings
            ],
            "evaluated_at": self.evaluated_at.isoformat(),
            "approval_granted": self.approval_granted,
            "schema_version": self.schema_version,
        }
        object.__setattr__(self, "decision_id", f"decision:{_digest(payload)}")


@dataclass(frozen=True, slots=True)
class ActionAttempt:
    attempt_id: str
    proposal_id: str
    decision_id: str
    affordance: str
    status: AttemptStatus
    started_at: datetime
    completed_at: datetime
    error: str | None = None

    def __post_init__(self) -> None:
        _require_aware(self.started_at)
        _require_aware(self.completed_at)
        if self.completed_at < self.started_at:
            raise ValueError("attempt completion cannot precede start")


@dataclass(frozen=True, slots=True)
class ObservedEffect:
    subject: str
    predicate: str
    value: Scalar


@dataclass(frozen=True, slots=True)
class OutcomeObservation:
    outcome_id: str
    attempt_id: str
    observed_at: datetime
    success: bool
    output: str
    output_digest: str
    truncated: bool
    observed_effects: tuple[ObservedEffect, ...] = ()

    def __post_init__(self) -> None:
        _require_aware(self.observed_at)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    attempt: ActionAttempt
    outcome: OutcomeObservation


@dataclass(frozen=True, slots=True)
class PolicyInput:
    proposal: ActionProposal
    affordance: AffordanceDefinition
    state: OperationalStateEstimate
    constraints: tuple[Constraint, ...]
    evaluated_at: datetime
    approval_granted: bool


class Policy(Protocol):
    name: str

    def evaluate(self, policy_input: PolicyInput) -> tuple[PolicyFinding, ...]: ...


def output_digest(output: bytes) -> str:
    return hashlib.sha256(output).hexdigest()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("control timestamps must be timezone-aware")


def _validate_evidence_ids(values: tuple[str, ...]) -> None:
    if any(not value or not value.strip() for value in values):
        raise ValueError("evidence ids must be non-empty")
    if len(values) != len(set(values)):
        raise ValueError("evidence ids must be unique")


def _argument_type_matches(value: Scalar, expected: ArgumentValueType) -> bool:
    if expected is ArgumentValueType.STRING:
        return isinstance(value, str)
    if expected is ArgumentValueType.INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is ArgumentValueType.NUMBER:
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected is ArgumentValueType.BOOLEAN:
        return isinstance(value, bool)
    return False
