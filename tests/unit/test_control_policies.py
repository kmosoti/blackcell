from datetime import UTC, datetime, timedelta

import pytest

from blackcell.control import (
    ActionProposal,
    AffordanceDefinition,
    CheckRequirement,
    ClaimRequirement,
    Constraint,
    ExpectedEffect,
    PolicyEngine,
    PolicyOutcome,
    ProposedAssertion,
)
from blackcell.domains.repository import (
    Claim,
    ClaimConflict,
    EpistemicStatus,
    EvidenceRef,
    OperationalStateEstimate,
    SourceReliability,
)

NOW = datetime(2026, 4, 1, tzinfo=UTC)


def _claim(
    claim_id: str,
    subject: str,
    predicate: str,
    value: str | bool | None,
    *,
    observed_at: datetime = NOW,
    expires_at: datetime | None = None,
    status: EpistemicStatus = EpistemicStatus.OBSERVED,
    group: str | None = None,
) -> Claim:
    return Claim(
        claim_id,
        subject,
        predicate,
        value,
        status,
        SourceReliability.AUTHORITATIVE,
        (EvidenceRef(f"event:{claim_id}", "test"),),
        observed_at,
        observed_at,
        expires_at=expires_at,
        conflict_group=group,
    )


def _state(*claims: Claim, conflicts: tuple[ClaimConflict, ...] = ()):
    return OperationalStateEstimate("repo", 1, NOW, claims, (), conflicts, ())


def _proposal(
    *,
    affordance: str = "mark_ready",
    effects: tuple[ExpectedEffect, ...] = (),
    required: tuple[ClaimRequirement, ...] = (),
) -> ActionProposal:
    return ActionProposal(
        "proposal:1",
        "context:1",
        affordance,
        (),
        effects,
        "advance only when evidence supports it",
        required,
        evidence_ids=("claim:source",),
        assertions=(ProposedAssertion("The task can advance.", ("claim:source",)),),
    )


def test_external_or_mutating_affordance_requires_explicit_approval() -> None:
    proposal = _proposal(affordance="publish")
    definition = AffordanceDefinition(
        "publish", "publish externally", read_only=False, external=True, mutates_state=True
    )
    engine = PolicyEngine()

    pending = engine.evaluate(proposal, definition, _state(), evaluated_at=NOW)
    approved = engine.evaluate(
        proposal, definition, _state(), evaluated_at=NOW, approval_granted=True
    )

    assert pending.outcome is PolicyOutcome.REQUIRE_APPROVAL
    assert approved.outcome is PolicyOutcome.ALLOW


def test_blocked_task_cannot_advance() -> None:
    proposal = _proposal(effects=(ExpectedEffect("task:T1", "status", "ready"),))
    definition = AffordanceDefinition("mark_ready", "mark task ready", read_only=True)
    state = _state(_claim("blocked", "task:T1", "blocked", True))

    decision = PolicyEngine().evaluate(proposal, definition, state, evaluated_at=NOW)

    assert decision.outcome is PolicyOutcome.DENY
    assert any(finding.code == "task_blocked" for finding in decision.findings)


def test_stale_or_conflicting_required_evidence_forces_evidence_action() -> None:
    stale = _claim(
        "stale",
        "repository",
        "git.clean",
        True,
        observed_at=NOW - timedelta(hours=2),
        expires_at=NOW - timedelta(minutes=1),
    )
    requirement = ClaimRequirement("repository", "git.clean", max_age_seconds=60)
    proposal = _proposal(required=(requirement,))
    state = _state(stale)
    action = AffordanceDefinition("mark_ready", "mark ready", read_only=True)
    inspect = AffordanceDefinition(
        "inspect_file", "inspect evidence", read_only=True, evidence_action=True
    )

    denied = PolicyEngine().evaluate(proposal, action, state, evaluated_at=NOW)
    inspect_proposal = ActionProposal(
        "proposal:inspect",
        "context:1",
        "inspect_file",
        (),
        (),
        "refresh stale evidence",
        (requirement,),
    )
    allowed = PolicyEngine().evaluate(inspect_proposal, inspect, state, evaluated_at=NOW)

    assert denied.outcome is PolicyOutcome.DENY
    assert any(finding.code == "stale" for finding in denied.findings)
    assert allowed.outcome is PolicyOutcome.ALLOW

    left = _claim("left", "repository", "git.clean", True, group="git-clean")
    right = _claim("right", "repository", "git.clean", False, group="git-clean")
    conflict_state = _state(left, right, conflicts=(ClaimConflict("git-clean", (left, right)),))
    conflict_decision = PolicyEngine().evaluate(proposal, action, conflict_state, evaluated_at=NOW)
    assert any(finding.code == "conflicting" for finding in conflict_decision.findings)


def test_failing_and_stale_checks_block_readiness() -> None:
    proposal = _proposal(effects=(ExpectedEffect("task:T1", "status", "ready"),))
    action = AffordanceDefinition("mark_ready", "mark ready", read_only=True)
    constraint = Constraint(
        "checks",
        "required checks must pass",
        required_checks=(CheckRequirement("unit", max_age_seconds=300),),
    )
    failing = _state(_claim("check", "check:unit", "status", "failed"))
    stale = _state(
        _claim(
            "check",
            "check:unit",
            "status",
            "passed",
            observed_at=NOW - timedelta(hours=1),
        )
    )

    failed_decision = PolicyEngine().evaluate(
        proposal, action, failing, constraints=(constraint,), evaluated_at=NOW
    )
    stale_decision = PolicyEngine().evaluate(
        proposal, action, stale, constraints=(constraint,), evaluated_at=NOW
    )

    assert any(finding.code == "failing" for finding in failed_decision.findings)
    assert any(finding.code == "stale" for finding in stale_decision.findings)


def test_proposal_evidence_ids_must_be_non_empty_and_unique() -> None:
    with pytest.raises(ValueError, match="unique"):
        ActionProposal(
            "proposal",
            "context",
            "inspect_file",
            (),
            (),
            "inspect",
            evidence_ids=("same", "same"),
        )
