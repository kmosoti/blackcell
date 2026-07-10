from __future__ import annotations

from datetime import datetime

from blackcell.control.models import (
    ActionProposal,
    AffordanceDefinition,
    CheckRequirement,
    ClaimRequirement,
    Constraint,
    ExpectedEffect,
    Policy,
    PolicyDecision,
    PolicyFinding,
    PolicyInput,
    PolicyOutcome,
)
from blackcell.domains.repository import Claim, EpistemicStatus, OperationalStateEstimate

_ADVANCING_TASK_STATUSES = frozenset(
    {"ready", "in_progress", "done", "completed", "closed", "released"}
)
_READINESS_VALUES = frozenset({"ready", "done", "completed", "released", "true"})


class ExternalOrMutationApprovalPolicy:
    name = "external-or-mutation-requires-approval"

    def evaluate(self, policy_input: PolicyInput) -> tuple[PolicyFinding, ...]:
        affordance = policy_input.affordance
        needs_approval = affordance.external or affordance.mutates_state or not affordance.read_only
        if not needs_approval or policy_input.approval_granted:
            return ()
        return (
            PolicyFinding(
                self.name,
                PolicyOutcome.REQUIRE_APPROVAL,
                "approval_required",
                f"{affordance.name} is external or state-mutating",
            ),
        )


class BlockedTaskPolicy:
    name = "blocked-task-cannot-advance"

    def evaluate(self, policy_input: PolicyInput) -> tuple[PolicyFinding, ...]:
        findings = []
        for effect in policy_input.proposal.expected_effects:
            if not _advances_task(effect):
                continue
            claims = policy_input.state.find_claims(effect.subject, "blocked")
            status_claims = policy_input.state.find_claims(effect.subject, "status")
            if any(claim.value is True for claim in claims) or any(
                str(claim.value).casefold() == "blocked" for claim in status_claims
            ):
                findings.append(
                    PolicyFinding(
                        self.name,
                        PolicyOutcome.DENY,
                        "task_blocked",
                        f"{effect.subject} is blocked and cannot advance to {effect.value}",
                    )
                )
        return tuple(findings)


class RequiredEvidencePolicy:
    name = "required-evidence-integrity"

    def evaluate(self, policy_input: PolicyInput) -> tuple[PolicyFinding, ...]:
        requirements = _evidence_requirements(policy_input)
        issues = tuple(
            issue
            for requirement in requirements
            if (
                issue := _evidence_issue(
                    policy_input.state, requirement, policy_input.evaluated_at
                )
            )
        )
        if not issues or policy_input.affordance.evidence_action:
            return ()
        return tuple(
            PolicyFinding(
                self.name,
                PolicyOutcome.DENY,
                code,
                f"required evidence {requirement.subject}/{requirement.predicate} is {code}; "
                "inspect or clarify before acting",
            )
            for requirement, code in issues
        )


class RequiredChecksPolicy:
    name = "failing-or-stale-checks-block-readiness"

    def evaluate(self, policy_input: PolicyInput) -> tuple[PolicyFinding, ...]:
        if not any(
            _is_readiness_effect(effect) for effect in policy_input.proposal.expected_effects
        ):
            return ()
        requirements = _check_requirements(policy_input)
        findings = []
        for requirement in requirements:
            code = _check_issue(policy_input.state, requirement, policy_input.evaluated_at)
            if code is not None:
                findings.append(
                    PolicyFinding(
                        self.name,
                        PolicyOutcome.DENY,
                        code,
                        f"required check {requirement.name} is {code}",
                    )
                )
        return tuple(findings)


class PolicyEngine:
    def __init__(self, policies: tuple[Policy, ...] | None = None) -> None:
        self._policies = policies or (
            ExternalOrMutationApprovalPolicy(),
            BlockedTaskPolicy(),
            RequiredEvidencePolicy(),
            RequiredChecksPolicy(),
        )

    def evaluate(
        self,
        proposal: ActionProposal,
        affordance: AffordanceDefinition,
        state: OperationalStateEstimate,
        *,
        constraints: tuple[Constraint, ...] = (),
        evaluated_at: datetime,
        approval_granted: bool = False,
    ) -> PolicyDecision:
        if proposal.affordance != affordance.name:
            raise ValueError("proposal affordance does not match its definition")
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            raise ValueError("policy evaluation time must be timezone-aware")
        policy_input = PolicyInput(
            proposal,
            affordance,
            state,
            constraints,
            evaluated_at,
            approval_granted,
        )
        findings = tuple(
            finding for policy in self._policies for finding in policy.evaluate(policy_input)
        )
        outcomes = {finding.outcome for finding in findings}
        if PolicyOutcome.DENY in outcomes:
            outcome = PolicyOutcome.DENY
        elif PolicyOutcome.REQUIRE_APPROVAL in outcomes:
            outcome = PolicyOutcome.REQUIRE_APPROVAL
        else:
            outcome = PolicyOutcome.ALLOW
        if not findings:
            findings = (
                PolicyFinding(
                    "policy-engine",
                    PolicyOutcome.ALLOW,
                    "allowed",
                    "all configured policies allowed the proposal",
                ),
            )
        return PolicyDecision(
            proposal_id=proposal.proposal_id,
            outcome=outcome,
            findings=findings,
            evaluated_at=evaluated_at,
            approval_granted=approval_granted,
        )


def _evidence_requirements(policy_input: PolicyInput) -> tuple[ClaimRequirement, ...]:
    return _deduplicate(
        (
            *policy_input.proposal.required_evidence,
            *(
                item
                for constraint in policy_input.constraints
                for item in constraint.required_evidence
            ),
        ),
        key=lambda item: (item.subject, item.predicate, item.max_age_seconds, item.allow_unknown),
    )


def _check_requirements(policy_input: PolicyInput) -> tuple[CheckRequirement, ...]:
    explicit = tuple(
        item for constraint in policy_input.constraints for item in constraint.required_checks
    )
    required_names = {
        claim.subject.removeprefix("check:")
        for claim in policy_input.state.claims
        if claim.subject.startswith("check:")
        and claim.predicate == "required"
        and claim.value is True
    }
    inferred = tuple(CheckRequirement(name) for name in sorted(required_names))
    return _deduplicate((*explicit, *inferred), key=lambda item: item.name)


def _evidence_issue(
    state: OperationalStateEstimate, requirement: ClaimRequirement, at: datetime
) -> tuple[ClaimRequirement, str] | None:
    claims = state.find_claims(requirement.subject, requirement.predicate)
    if not claims:
        return requirement, "missing"
    current = tuple(
        claim
        for claim in claims
        if not _stale(claim, at, requirement.max_age_seconds)
    )
    if not current:
        return requirement, "stale"
    if _claims_conflict(state, current):
        return requirement, "conflicting"
    if not requirement.allow_unknown and all(
        claim.epistemic_status is EpistemicStatus.UNKNOWN for claim in current
    ):
        return requirement, "unknown"
    return None


def _check_issue(
    state: OperationalStateEstimate, requirement: CheckRequirement, at: datetime
) -> str | None:
    claims = state.find_claims(f"check:{requirement.name}", "status")
    if not claims:
        return "missing"
    current = tuple(
        claim
        for claim in claims
        if not _stale(claim, at, requirement.max_age_seconds)
    )
    if not current:
        return "stale"
    if _claims_conflict(state, current):
        return "conflicting"
    passing = {value.casefold() for value in requirement.passing_values}
    if any(str(claim.value).casefold() not in passing for claim in current):
        return "failing"
    return None


def _claims_conflict(state: OperationalStateEstimate, claims: tuple[Claim, ...]) -> bool:
    ids = {claim.claim_id for claim in claims}
    return any(
        ids.intersection(claim.claim_id for claim in conflict.claims)
        for conflict in state.conflicts
    )


def _stale(claim: Claim, at: datetime, max_age_seconds: int | None) -> bool:
    if claim.is_expired(at):
        return True
    if max_age_seconds is None:
        return False
    return (at - claim.observed_at).total_seconds() > max_age_seconds


def _advances_task(effect: ExpectedEffect) -> bool:
    return (
        effect.subject.startswith("task:")
        and effect.predicate == "status"
        and str(effect.value).casefold() in _ADVANCING_TASK_STATUSES
    )


def _is_readiness_effect(effect: ExpectedEffect) -> bool:
    predicate = effect.predicate.casefold()
    value = str(effect.value).casefold()
    return (predicate == "status" and value in _READINESS_VALUES) or (
        predicate.endswith(".ready") and value == "true"
    )


def _deduplicate(items: tuple, *, key):
    result = []
    seen = set()
    for item in items:
        identity = key(item)
        if identity not in seen:
            seen.add(identity)
            result.append(item)
    return tuple(result)
