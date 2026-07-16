from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from blackcell.features.authorize_action import (
    ActionArgument,
    ActionProposal,
    AffordancePolicy,
    AuthorizationOutcome,
    AuthorizeAction,
    authorize_action,
)
from blackcell.features.solve_constraints import (
    ConstraintEvaluation,
    ConstraintOutcome,
    ConstraintProof,
)

NOW = datetime(2026, 7, 10, 20, tzinfo=UTC)
DEFINITION_DIGEST = f"sha256:{'0' * 64}"


def test_symbolic_violation_denies_even_with_human_approval() -> None:
    evaluation = _evaluation(ConstraintOutcome.VIOLATED)
    decision = authorize_action(
        _command(approval=True),
        evaluation,
    )

    assert decision.outcome is AuthorizationOutcome.DENY
    assert decision.findings[0].code == "constraint_violated"
    assert decision.findings[0].proof_ids == (evaluation.proofs[0].proof_id,)


def test_unknown_state_denies_action_but_allows_evidence_gathering() -> None:
    unknown = _evaluation(ConstraintOutcome.UNKNOWN, evidence_event_id=None)

    denied = authorize_action(_command(), unknown)
    evidence_action = authorize_action(
        _command(affordance=AffordancePolicy("inspect", True, evidence_action=True)),
        unknown,
    )

    assert denied.outcome is AuthorizationOutcome.DENY
    assert evidence_action.outcome is AuthorizationOutcome.ALLOW


def test_mutating_action_requires_approval_then_allows() -> None:
    policy = AffordancePolicy("update", False, mutates_state=True)
    constraints = _evaluation(ConstraintOutcome.SATISFIED)

    pending = authorize_action(_command(affordance=policy), constraints)
    approved = authorize_action(_command(affordance=policy, approval=True), constraints)

    assert pending.outcome is AuthorizationOutcome.REQUIRE_APPROVAL
    assert approved.outcome is AuthorizationOutcome.ALLOW


def test_authorization_binds_proposal_constraint_frame_action_and_policy() -> None:
    command = _command()
    evaluation = _evaluation(ConstraintOutcome.SATISFIED)

    decision = authorize_action(command, evaluation)

    assert decision.context_frame_id == command.proposal.context_frame_id
    assert decision.proposal_digest == command.proposal.proposal_digest
    assert decision.constraint_evaluation_id == evaluation.evaluation_id
    assert decision.authorized_action_digest == command.proposal.action_digest
    assert decision.affordance_policy_digest == command.affordance.policy_digest
    assert decision.authorized_read_only


def test_constraint_evaluation_must_belong_to_the_proposal_context_frame() -> None:
    command = _command()
    evaluation = ConstraintEvaluation(
        "frame:stale",
        _evaluation(ConstraintOutcome.SATISFIED).proofs,
        NOW,
    )

    decision = authorize_action(command, evaluation)

    assert decision.outcome is AuthorizationOutcome.DENY
    assert "constraint_context_mismatch" in {item.code for item in decision.findings}


def test_constraint_evaluation_cannot_be_relabelled_with_a_fresh_decision_time() -> None:
    evaluated_at = NOW - timedelta(minutes=1)
    proof = ConstraintProof(
        "constraint:1",
        DEFINITION_DIGEST,
        ConstraintOutcome.SATISFIED,
        "satisfied",
        "fixture proof",
        ("event:1",),
        evaluated_at,
    )

    decision = authorize_action(
        _command(),
        ConstraintEvaluation("frame:1", (proof,), evaluated_at),
    )

    assert decision.outcome is AuthorizationOutcome.DENY
    assert "constraint_time_mismatch" in {item.code for item in decision.findings}


def test_empty_or_unrecognized_constraint_proofs_fail_closed() -> None:
    empty = InvalidEvaluation("frame:1", "evaluation:empty", NOW, ())
    invalid = InvalidEvaluation(
        "frame:1",
        "evaluation:forged",
        NOW,
        (
            InvalidProof(
                "proof:forged",
                "constraint:forged",
                DEFINITION_DIGEST,
                "forged",
                "forged",
                ("event:1",),
            ),
        ),
    )

    empty_decision = authorize_action(_command(), empty)
    invalid_decision = authorize_action(_command(), invalid)

    assert empty_decision.outcome is AuthorizationOutcome.DENY
    assert "constraints_missing" in {item.code for item in empty_decision.findings}
    assert invalid_decision.outcome is AuthorizationOutcome.DENY
    assert "constraint_outcome_invalid" in {item.code for item in invalid_decision.findings}


def test_constraint_proof_evidence_must_belong_to_the_context_frame() -> None:
    proof = ConstraintProof(
        "constraint:1",
        DEFINITION_DIGEST,
        ConstraintOutcome.SATISFIED,
        "satisfied",
        "fixture proof",
        ("event:outside",),
        NOW,
    )

    decision = authorize_action(
        _command(),
        ConstraintEvaluation("frame:1", (proof,), NOW),
    )

    assert decision.outcome is AuthorizationOutcome.DENY
    finding = next(
        item for item in decision.findings if item.code == "constraint_evidence_outside_context"
    )
    assert finding.proof_ids == (proof.proof_id,)


def test_evidence_action_policy_cannot_bypass_unknown_state_for_side_effects() -> None:
    with pytest.raises(ValueError, match="evidence-gathering affordance"):
        AffordancePolicy(
            "inspect",
            False,
            mutates_state=True,
            evidence_action=True,
        )
    with pytest.raises(ValueError, match="evidence-gathering affordance"):
        AffordancePolicy("inspect", True, external=True, evidence_action=True)


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

    decision = authorize_action(
        command,
        _evaluation(ConstraintOutcome.SATISFIED, evidence_event_id="event:inside"),
    )

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


def _evaluation(
    outcome: ConstraintOutcome,
    *,
    evidence_event_id: str | None = "event:1",
) -> ConstraintEvaluation:
    proof = ConstraintProof(
        "constraint:1",
        DEFINITION_DIGEST,
        outcome,
        outcome.value,
        "fixture proof",
        () if evidence_event_id is None else (evidence_event_id,),
        NOW,
    )
    return ConstraintEvaluation("frame:1", (proof,), NOW)


@dataclass(frozen=True, slots=True)
class InvalidProof:
    proof_id: str
    constraint_id: str
    constraint_definition_digest: str
    outcome: object
    code: str
    evidence_event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InvalidEvaluation:
    context_frame_id: str
    evaluation_id: str
    evaluated_at: datetime
    proofs: tuple[InvalidProof, ...]
