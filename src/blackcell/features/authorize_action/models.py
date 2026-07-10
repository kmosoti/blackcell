from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from blackcell.kernel import JsonScalar
from blackcell.kernel._json import json_digest


class AuthorizationOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require-approval"


@dataclass(frozen=True, slots=True)
class ActionArgument:
    name: str
    value: JsonScalar

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("argument name must not be empty")


@dataclass(frozen=True, slots=True)
class ActionProposal:
    proposal_id: str
    context_frame_id: str
    affordance: str
    arguments: tuple[ActionArgument, ...]
    rationale: str
    evidence_event_ids: tuple[str, ...] = ()
    schema_version: str = "action-proposal/v1"

    def __post_init__(self) -> None:
        for name in ("proposal_id", "context_frame_id", "affordance", "rationale"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        names = tuple(argument.name for argument in self.arguments)
        if len(names) != len(set(names)):
            raise ValueError("action argument names must be unique")
        if any(not event_id.strip() for event_id in self.evidence_event_ids):
            raise ValueError("evidence event ids must not be blank")
        if len(self.evidence_event_ids) != len(set(self.evidence_event_ids)):
            raise ValueError("evidence event ids must be unique")


@dataclass(frozen=True, slots=True)
class AffordancePolicy:
    name: str
    read_only: bool
    external: bool = False
    mutates_state: bool = False
    evidence_action: bool = False
    allowed_arguments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("affordance name must not be empty")
        if self.read_only and self.mutates_state:
            raise ValueError("a state-mutating affordance cannot be read-only")
        if len(self.allowed_arguments) != len(set(self.allowed_arguments)):
            raise ValueError("allowed argument names must be unique")


@dataclass(frozen=True, slots=True)
class AuthorizationFinding:
    outcome: AuthorizationOutcome
    code: str
    message: str
    proof_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    proposal_id: str
    constraint_evaluation_id: str
    outcome: AuthorizationOutcome
    findings: tuple[AuthorizationFinding, ...]
    evaluated_at: datetime
    approval_granted: bool
    schema_version: str = "authorization-decision/v1"
    decision_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decision_id",
            json_digest(
                {
                    "schema_version": self.schema_version,
                    "proposal_id": self.proposal_id,
                    "constraint_evaluation_id": self.constraint_evaluation_id,
                    "outcome": self.outcome.value,
                    "finding_codes": [item.code for item in self.findings],
                    "evaluated_at": self.evaluated_at.isoformat(),
                    "approval_granted": self.approval_granted,
                }
            ),
        )
