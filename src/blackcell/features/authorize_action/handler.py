from __future__ import annotations

from blackcell.features.authorize_action.command import AuthorizeAction
from blackcell.features.authorize_action.models import (
    AuthorizationDecision,
    AuthorizationFinding,
    AuthorizationOutcome,
)
from blackcell.features.authorize_action.ports import ConstraintEvaluationLike, ConstraintProofLike


def authorize_action(
    command: AuthorizeAction,
    constraints: ConstraintEvaluationLike,
) -> AuthorizationDecision:
    findings: list[AuthorizationFinding] = []
    proofs = tuple(constraints.proofs)
    if constraints.context_frame_id != command.proposal.context_frame_id:
        findings.append(
            AuthorizationFinding(
                AuthorizationOutcome.DENY,
                "constraint_context_mismatch",
                "constraint evaluation belongs to a different ContextFrame",
            )
        )
    if constraints.evaluated_at != command.evaluated_at:
        findings.append(
            AuthorizationFinding(
                AuthorizationOutcome.DENY,
                "constraint_time_mismatch",
                "constraint evaluation time does not match the authorization decision",
            )
        )
    if not proofs:
        findings.append(
            AuthorizationFinding(
                AuthorizationOutcome.DENY,
                "constraints_missing",
                "authorization requires at least one constraint proof",
            )
        )
    invalid_outcomes = tuple(
        proof for proof in proofs if _outcome(proof) not in {"satisfied", "violated", "unknown"}
    )
    if invalid_outcomes:
        findings.append(
            AuthorizationFinding(
                AuthorizationOutcome.DENY,
                "constraint_outcome_invalid",
                "constraint evaluation contains an unrecognized proof outcome",
                tuple(proof.proof_id for proof in invalid_outcomes),
            )
        )
    proof_ids = tuple(proof.proof_id for proof in proofs)
    constraint_ids = tuple(proof.constraint_id for proof in proofs)
    definition_digests = tuple(proof.constraint_definition_digest for proof in proofs)
    proof_evidence_ids = tuple(
        event_id for proof in proofs for event_id in proof.evidence_event_ids
    )
    decisive_without_evidence = tuple(
        proof
        for proof in proofs
        if _outcome(proof) in {"satisfied", "violated"} and not proof.evidence_event_ids
    )
    if (
        any(not proof_id.strip() for proof_id in proof_ids)
        or any(not constraint_id.strip() for constraint_id in constraint_ids)
        or any(not digest.strip() for digest in definition_digests)
        or any(not event_id.strip() for event_id in proof_evidence_ids)
        or len(proof_ids) != len(set(proof_ids))
        or len(constraint_ids) != len(set(constraint_ids))
        or any(
            len(proof.evidence_event_ids) != len(set(proof.evidence_event_ids)) for proof in proofs
        )
        or decisive_without_evidence
    ):
        findings.append(
            AuthorizationFinding(
                AuthorizationOutcome.DENY,
                "constraint_proofs_invalid",
                "constraint proof identities, definitions, and decisive evidence "
                "must be non-empty and unique",
            )
        )
    outside_context = tuple(
        proof
        for proof in proofs
        if any(
            event_id not in command.context_evidence_event_ids
            for event_id in proof.evidence_event_ids
        )
    )
    if outside_context:
        findings.append(
            AuthorizationFinding(
                AuthorizationOutcome.DENY,
                "constraint_evidence_outside_context",
                "constraint proof cites evidence absent from its ContextFrame",
                tuple(proof.proof_id for proof in outside_context),
            )
        )
    violated = tuple(proof for proof in proofs if _outcome(proof) == "violated")
    unknown = tuple(proof for proof in proofs if _outcome(proof) == "unknown")
    if violated:
        findings.append(
            AuthorizationFinding(
                AuthorizationOutcome.DENY,
                "constraint_violated",
                "symbolic constraints rejected the proposed action",
                tuple(proof.proof_id for proof in violated),
            )
        )
    if unknown and not command.affordance.evidence_action:
        findings.append(
            AuthorizationFinding(
                AuthorizationOutcome.DENY,
                "constraint_unknown",
                "required state is unknown; gather evidence before acting",
                tuple(proof.proof_id for proof in unknown),
            )
        )
    missing_citations = tuple(
        event_id
        for event_id in command.proposal.evidence_event_ids
        if event_id not in command.context_evidence_event_ids
    )
    if missing_citations:
        findings.append(
            AuthorizationFinding(
                AuthorizationOutcome.DENY,
                "evidence_outside_context",
                f"proposal cites evidence absent from its ContextFrame: {missing_citations}",
            )
        )
    unexpected_arguments = tuple(
        argument.name
        for argument in command.proposal.arguments
        if argument.name not in command.affordance.allowed_arguments
    )
    if unexpected_arguments:
        findings.append(
            AuthorizationFinding(
                AuthorizationOutcome.DENY,
                "unexpected_arguments",
                f"proposal contains undeclared arguments: {unexpected_arguments}",
            )
        )
    needs_approval = (
        command.affordance.external
        or command.affordance.mutates_state
        or not command.affordance.read_only
    )
    if not findings and needs_approval and not command.approval_granted:
        findings.append(
            AuthorizationFinding(
                AuthorizationOutcome.REQUIRE_APPROVAL,
                "approval_required",
                "external or state-mutating affordance requires approval",
            )
        )
    if any(item.outcome is AuthorizationOutcome.DENY for item in findings):
        outcome = AuthorizationOutcome.DENY
    elif findings:
        outcome = AuthorizationOutcome.REQUIRE_APPROVAL
    else:
        outcome = AuthorizationOutcome.ALLOW
        findings.append(
            AuthorizationFinding(
                AuthorizationOutcome.ALLOW,
                "allowed",
                "symbolic constraints and affordance policy allow the action",
            )
        )
    return AuthorizationDecision(
        proposal_id=command.proposal.proposal_id,
        proposal_digest=command.proposal.proposal_digest,
        context_frame_id=command.proposal.context_frame_id,
        constraint_evaluation_id=constraints.evaluation_id,
        authorized_action_digest=command.proposal.action_digest,
        affordance_policy_digest=command.affordance.policy_digest,
        authorized_read_only=command.affordance.read_only,
        authorized_external=command.affordance.external,
        authorized_mutates_state=command.affordance.mutates_state,
        outcome=outcome,
        findings=tuple(findings),
        evaluated_at=command.evaluated_at,
        approval_granted=command.approval_granted,
    )


def _outcome(proof: ConstraintProofLike) -> str:
    return str(proof.outcome)
