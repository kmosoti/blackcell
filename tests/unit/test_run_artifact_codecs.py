from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from blackcell.features.authorize_action import (
    ActionArgument,
    ActionProposal,
    AuthorizationDecision,
    AuthorizationFinding,
    AuthorizationOutcome,
)
from blackcell.features.authorize_action.artifacts import (
    AuthorizationArtifactCodecError,
    decode_action_proposal,
    decode_authorization_decision,
    encode_action_proposal,
    encode_authorization_decision,
)
from blackcell.features.solve_constraints import (
    ConstraintEvaluation,
    ConstraintOutcome,
    ConstraintProof,
)
from blackcell.features.solve_constraints.artifacts import (
    ConstraintArtifactCodecError,
    decode_constraint_evaluation,
    encode_constraint_evaluation,
)
from blackcell.kernel._json import canonical_json_bytes

NOW = datetime(2026, 7, 11, 12, tzinfo=UTC)
DIGEST = f"sha256:{'1' * 64}"


def test_proposal_codec_is_canonical_complete_and_identity_checked() -> None:
    proposal = _proposal()
    encoded = encode_action_proposal(proposal)

    assert decode_action_proposal(encoded) == proposal
    assert encoded == canonical_json_bytes(json.loads(encoded))
    assert json.loads(encoded)["arguments"] == [
        {"name": "path", "value": "README.md"},
        {"name": "encoding", "value": "utf-8"},
    ]

    forged = json.loads(encoded)
    forged["rationale"] = "changed after identity calculation"
    with pytest.raises(AuthorizationArtifactCodecError, match="proposal_digest"):
        decode_action_proposal(canonical_json_bytes(forged))


def test_authorization_and_constraint_codecs_round_trip_complete_proofs() -> None:
    evaluation = _evaluation()
    decision = _decision(evaluation)

    assert decode_constraint_evaluation(encode_constraint_evaluation(evaluation)) == evaluation
    assert decode_authorization_decision(encode_authorization_decision(decision)) == decision
    proof_payload = json.loads(encode_constraint_evaluation(evaluation))["proofs"][0]
    assert proof_payload["proof_id"] == evaluation.proofs[0].proof_id
    assert proof_payload["evidence_event_ids"] == ["event:1"]


def test_codecs_fail_closed_on_future_schema() -> None:
    proposal_payload = json.loads(encode_action_proposal(_proposal()))
    proposal_payload["schema_version"] = "action-proposal/v99"
    authorization_payload = json.loads(encode_authorization_decision(_decision(_evaluation())))
    authorization_payload["schema_version"] = "authorization-decision/v99"
    evaluation_payload = json.loads(encode_constraint_evaluation(_evaluation()))
    evaluation_payload["schema_version"] = "constraint-evaluation/v99"
    proof_payload = json.loads(encode_constraint_evaluation(_evaluation()))
    proof_payload["proofs"][0]["schema_version"] = "constraint-proof/v99"
    cases = (
        (
            canonical_json_bytes(proposal_payload),
            decode_action_proposal,
            AuthorizationArtifactCodecError,
        ),
        (
            canonical_json_bytes(authorization_payload),
            decode_authorization_decision,
            AuthorizationArtifactCodecError,
        ),
        (
            canonical_json_bytes(evaluation_payload),
            decode_constraint_evaluation,
            ConstraintArtifactCodecError,
        ),
        (
            canonical_json_bytes(proof_payload),
            decode_constraint_evaluation,
            ConstraintArtifactCodecError,
        ),
    )
    for encoded, decoder, error in cases:
        with pytest.raises(error, match="unsupported"):
            decoder(encoded)


def test_codecs_reject_noncanonical_json() -> None:
    pretty = json.dumps(json.loads(encode_action_proposal(_proposal())), indent=2).encode()

    with pytest.raises(AuthorizationArtifactCodecError, match="canonical"):
        decode_action_proposal(pretty)


def _proposal() -> ActionProposal:
    return ActionProposal(
        "proposal:1",
        f"sha256:{'2' * 64}",
        "inspect",
        (
            ActionArgument("path", "README.md"),
            ActionArgument("encoding", "utf-8"),
        ),
        "inspect cited evidence",
        ("event:1",),
    )


def _evaluation() -> ConstraintEvaluation:
    proof = ConstraintProof(
        "constraint:1",
        DIGEST,
        ConstraintOutcome.SATISFIED,
        "satisfied",
        "constraint satisfied",
        ("event:1",),
        NOW,
    )
    return ConstraintEvaluation(_proposal().context_frame_id, (proof,), NOW)


def _decision(evaluation: ConstraintEvaluation) -> AuthorizationDecision:
    proposal = _proposal()
    return AuthorizationDecision(
        proposal_id=proposal.proposal_id,
        proposal_digest=proposal.proposal_digest,
        context_frame_id=proposal.context_frame_id,
        constraint_evaluation_id=evaluation.evaluation_id,
        authorized_action_digest=proposal.action_digest,
        affordance_policy_digest=DIGEST,
        authorized_read_only=True,
        authorized_external=False,
        authorized_mutates_state=False,
        outcome=AuthorizationOutcome.ALLOW,
        findings=(AuthorizationFinding(AuthorizationOutcome.ALLOW, "allowed", "fixture"),),
        evaluated_at=NOW,
        approval_granted=False,
    )
