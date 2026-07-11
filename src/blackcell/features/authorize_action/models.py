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
    schema_version: str = "action-proposal/v2"
    action_digest: str = field(init=False)
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
        names = tuple(argument.name for argument in self.arguments)
        if len(names) != len(set(names)):
            raise ValueError("action argument names must be unique")
        if any(not event_id.strip() for event_id in self.evidence_event_ids):
            raise ValueError("evidence event ids must not be blank")
        if len(self.evidence_event_ids) != len(set(self.evidence_event_ids)):
            raise ValueError("evidence event ids must be unique")
        action_digest = _action_digest(self.proposal_id, self.affordance, self.arguments)
        object.__setattr__(self, "action_digest", action_digest)
        object.__setattr__(
            self,
            "proposal_digest",
            json_digest(
                {
                    "schema_version": self.schema_version,
                    "proposal_id": self.proposal_id,
                    "context_frame_id": self.context_frame_id,
                    "action_digest": action_digest,
                    "rationale": self.rationale,
                    "evidence_event_ids": list(self.evidence_event_ids),
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class AffordancePolicy:
    name: str
    read_only: bool
    external: bool = False
    mutates_state: bool = False
    evidence_action: bool = False
    allowed_arguments: tuple[str, ...] = ()
    policy_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("affordance name must not be empty")
        if self.read_only and self.mutates_state:
            raise ValueError("a state-mutating affordance cannot be read-only")
        if self.evidence_action and (not self.read_only or self.external or self.mutates_state):
            raise ValueError(
                "an evidence-gathering affordance must be read-only, local, and non-mutating"
            )
        if len(self.allowed_arguments) != len(set(self.allowed_arguments)):
            raise ValueError("allowed argument names must be unique")
        if any(not name.strip() for name in self.allowed_arguments):
            raise ValueError("allowed argument names must not be blank")
        object.__setattr__(
            self,
            "policy_digest",
            json_digest(
                {
                    "schema_version": "affordance-policy/v1",
                    "name": self.name,
                    "read_only": self.read_only,
                    "external": self.external,
                    "mutates_state": self.mutates_state,
                    "evidence_action": self.evidence_action,
                    "allowed_arguments": sorted(self.allowed_arguments),
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class AuthorizationFinding:
    outcome: AuthorizationOutcome
    code: str
    message: str
    proof_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.message.strip():
            raise ValueError("authorization finding code and message must not be empty")
        if any(not proof_id.strip() for proof_id in self.proof_ids):
            raise ValueError("authorization finding proof ids must not be blank")
        if len(self.proof_ids) != len(set(self.proof_ids)):
            raise ValueError("authorization finding proof ids must be unique")


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    proposal_id: str
    proposal_digest: str
    context_frame_id: str
    constraint_evaluation_id: str
    authorized_action_digest: str
    affordance_policy_digest: str
    authorized_read_only: bool
    authorized_external: bool
    authorized_mutates_state: bool
    outcome: AuthorizationOutcome
    findings: tuple[AuthorizationFinding, ...]
    evaluated_at: datetime
    approval_granted: bool
    schema_version: str = "authorization-decision/v2"
    decision_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "proposal_id",
            "proposal_digest",
            "context_frame_id",
            "constraint_evaluation_id",
            "authorized_action_digest",
            "affordance_policy_digest",
            "schema_version",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        if not self.findings:
            raise ValueError("authorization decision requires at least one finding")
        if self.authorized_read_only and self.authorized_mutates_state:
            raise ValueError("a read-only authorization cannot mutate state")
        if any(item.outcome is AuthorizationOutcome.DENY for item in self.findings):
            expected_outcome = AuthorizationOutcome.DENY
        elif any(item.outcome is AuthorizationOutcome.REQUIRE_APPROVAL for item in self.findings):
            expected_outcome = AuthorizationOutcome.REQUIRE_APPROVAL
        else:
            expected_outcome = AuthorizationOutcome.ALLOW
        if self.outcome is not expected_outcome:
            raise ValueError("authorization outcome does not match its findings")
        needs_approval = (
            self.authorized_external
            or self.authorized_mutates_state
            or not self.authorized_read_only
        )
        if (
            self.outcome is AuthorizationOutcome.ALLOW
            and needs_approval
            and not self.approval_granted
        ):
            raise ValueError("side-effecting authorization requires recorded approval")
        object.__setattr__(
            self,
            "decision_id",
            json_digest(
                {
                    "schema_version": self.schema_version,
                    "proposal_id": self.proposal_id,
                    "proposal_digest": self.proposal_digest,
                    "context_frame_id": self.context_frame_id,
                    "constraint_evaluation_id": self.constraint_evaluation_id,
                    "authorized_action_digest": self.authorized_action_digest,
                    "affordance_policy_digest": self.affordance_policy_digest,
                    "authorized_read_only": self.authorized_read_only,
                    "authorized_external": self.authorized_external,
                    "authorized_mutates_state": self.authorized_mutates_state,
                    "outcome": self.outcome.value,
                    "findings": [
                        {
                            "outcome": item.outcome.value,
                            "code": item.code,
                            "message": item.message,
                            "proof_ids": list(item.proof_ids),
                        }
                        for item in self.findings
                    ],
                    "evaluated_at": self.evaluated_at.isoformat(),
                    "approval_granted": self.approval_granted,
                }
            ),
        )


def _action_digest(
    proposal_id: str,
    affordance: str,
    arguments: tuple[ActionArgument, ...],
) -> str:
    return json_digest(
        {
            "schema_version": "authorized-action/v1",
            "proposal_id": proposal_id,
            "affordance": affordance,
            "arguments": [
                {"name": item.name, "value": item.value}
                for item in sorted(arguments, key=lambda item: item.name)
            ],
        }
    )
