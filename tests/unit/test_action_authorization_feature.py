from datetime import UTC, datetime

from blackcell.features.authorize_action import (
    ActionArgument,
    ActionAuthorizer,
    ActionProposal,
    AffordancePolicy,
    AuthorizationOutcome,
    AuthorizeAction,
)
from blackcell.features.solve_constraints import (
    ConstraintEvaluation,
    ConstraintOutcome,
    ConstraintProof,
)

NOW = datetime(2026, 7, 10, 20, tzinfo=UTC)


def test_symbolic_violation_denies_even_with_human_approval() -> None:
    evaluation = _evaluation(ConstraintOutcome.VIOLATED)
    decision = ActionAuthorizer().handle(
        _command(approval=True),
        evaluation,
    )

    assert decision.outcome is AuthorizationOutcome.DENY
    assert decision.findings[0].code == "constraint_violated"
    assert decision.findings[0].proof_ids == (evaluation.proofs[0].proof_id,)


def test_unknown_state_denies_action_but_allows_evidence_gathering() -> None:
    unknown = _evaluation(ConstraintOutcome.UNKNOWN)

    denied = ActionAuthorizer().handle(_command(), unknown)
    evidence_action = ActionAuthorizer().handle(
        _command(affordance=AffordancePolicy("inspect", True, evidence_action=True)),
        unknown,
    )

    assert denied.outcome is AuthorizationOutcome.DENY
    assert evidence_action.outcome is AuthorizationOutcome.ALLOW


def test_mutating_action_requires_approval_then_allows() -> None:
    policy = AffordancePolicy("update", False, mutates_state=True)
    constraints = _evaluation(ConstraintOutcome.SATISFIED)

    pending = ActionAuthorizer().handle(_command(affordance=policy), constraints)
    approved = ActionAuthorizer().handle(_command(affordance=policy, approval=True), constraints)

    assert pending.outcome is AuthorizationOutcome.REQUIRE_APPROVAL
    assert approved.outcome is AuthorizationOutcome.ALLOW


def test_citations_and_arguments_must_be_declared_in_context_and_affordance() -> None:
    policy = AffordancePolicy("inspect", True, allowed_arguments=("path",))
    proposal = ActionProposal(
        "proposal:1",
        "frame:1",
        "inspect",
        (ActionArgument("command", "unsafe"),),
        "inspect repository",
        ("event:outside",),
    )
    command = AuthorizeAction(proposal, policy, NOW, ("event:inside",))

    decision = ActionAuthorizer().handle(command, _evaluation(ConstraintOutcome.SATISFIED))

    assert decision.outcome is AuthorizationOutcome.DENY
    assert {item.code for item in decision.findings} == {
        "evidence_outside_context",
        "unexpected_arguments",
    }


def _command(
    *,
    affordance: AffordancePolicy | None = None,
    approval: bool = False,
) -> AuthorizeAction:
    policy = affordance or AffordancePolicy("inspect", True)
    proposal = ActionProposal(
        "proposal:1",
        "frame:1",
        policy.name,
        (),
        "perform a bounded action",
        ("event:1",),
    )
    return AuthorizeAction(proposal, policy, NOW, ("event:1",), approval)


def _evaluation(outcome: ConstraintOutcome) -> ConstraintEvaluation:
    proof = ConstraintProof(
        "constraint:1",
        outcome,
        outcome.value,
        "fixture proof",
        ("event:1",),
        NOW,
    )
    return ConstraintEvaluation("frame:1", (proof,), NOW)
