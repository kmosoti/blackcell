from __future__ import annotations

from blackcell.features.authorize_action.command import AuthorizeAction
from blackcell.features.authorize_action.models import (
    AuthorizationDecision,
    AuthorizationFinding,
    AuthorizationOutcome,
)
from blackcell.features.authorize_action.ports import ConstraintEvaluationLike, ConstraintProofLike


class ActionAuthorizer:
    def handle(
        self,
        command: AuthorizeAction,
        constraints: ConstraintEvaluationLike,
    ) -> AuthorizationDecision:
        findings: list[AuthorizationFinding] = []
        violated = tuple(proof for proof in constraints.proofs if _outcome(proof) == "violated")
        unknown = tuple(proof for proof in constraints.proofs if _outcome(proof) == "unknown")
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
            command.proposal.proposal_id,
            constraints.evaluation_id,
            outcome,
            tuple(findings),
            command.evaluated_at,
            command.approval_granted,
        )


def _outcome(proof: ConstraintProofLike) -> str:
    return str(proof.outcome)
