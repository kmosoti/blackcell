from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

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
from blackcell.features.execute_affordance import (
    AdapterOutcome,
    AffordanceArgument,
    AffordanceArgumentSpec,
    AffordanceDefinition,
    AffordanceInvocation,
    ExecutionBinding,
    ExecutionClaim,
    ExecutionJournalEntry,
    ExecutionJournalStatus,
    ExecutionOperation,
    ExecutionPreparation,
    ExecutionRecovery,
    ExecutionRecoveryAuthorization,
    ExecutionResult,
    ExecutionStatus,
    ObservedEffect,
    SideEffectClass,
    deserialize_execution_preparation,
    deserialize_execution_result,
    serialize_execution_preparation,
    serialize_execution_result,
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
from blackcell.kernel import JsonScalar
from blackcell.kernel._json import canonical_json, canonical_json_bytes

NOW = datetime(2026, 7, 11, 12, tzinfo=UTC)
DIGEST = f"sha256:{'1' * 64}"


def test_execution_preparation_and_result_round_trip_with_canonical_identities() -> None:
    preparation = _preparation()
    result = _result(preparation.binding)

    serialized_preparation = serialize_execution_preparation(preparation)
    serialized_result = serialize_execution_result(result)

    assert serialized_preparation == canonical_json(json.loads(serialized_preparation))
    assert serialized_result == canonical_json(json.loads(serialized_result))
    assert (
        deserialize_execution_preparation(
            serialized_preparation.encode(),
            expected_preparation_id=preparation.preparation_id,
        )
        == preparation
    )
    assert (
        deserialize_execution_result(
            serialized_result.encode(),
            expected_result_id=result.result_id,
        )
        == result
    )


@pytest.mark.parametrize(
    ("build", "message"),
    (
        (lambda: AffordanceArgument(" ", "value"), "argument name"),
        (
            lambda: AffordanceArgument(
                "nested",
                cast("JsonScalar", {"not": "a scalar"}),
            ),
            "JSON scalar",
        ),
        (lambda: AffordanceArgumentSpec(""), "specification name"),
        (
            lambda: replace(
                _definition(),
                side_effect_class=cast("SideEffectClass", "unrecognized"),
            ),
            "side_effect_class",
        ),
        (lambda: replace(_definition(), name=""), "name and adapter id"),
        (lambda: replace(_definition(), timeout_seconds=cast("float", True)), "numeric"),
        (lambda: replace(_definition(), timeout_seconds=0), "positive"),
        (
            lambda: replace(
                _definition(),
                arguments=(AffordanceArgumentSpec("path"), AffordanceArgumentSpec("path")),
            ),
            "unique",
        ),
        (lambda: replace(_invocation(), invocation_id=""), "invocation_id"),
        (
            lambda: replace(_invocation(), requested_at=datetime(2026, 7, 11, 12)),
            "timezone-aware",
        ),
        (
            lambda: replace(
                _invocation(),
                arguments=(AffordanceArgument("path", "a"), AffordanceArgument("path", "b")),
            ),
            "unique",
        ),
        (lambda: ObservedEffect("", "exists", True), "subject and predicate"),
        (lambda: AdapterOutcome(True, "", NOW), "output_digest"),
        (
            lambda: AdapterOutcome(True, DIGEST, datetime(2026, 7, 11, 12)),
            "timezone-aware",
        ),
    ),
)
def test_affordance_models_reject_ambiguous_contracts(
    build: Callable[[], object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build()


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"reconciled": cast("bool", "yes")}, "reconciled"),
        ({"schema_version": "execution-result/v99"}, "unsupported"),
        ({"status": cast("ExecutionStatus", "succeeded")}, "recognized"),
        ({"invocation_id": ""}, "invocation_id"),
        ({"started_at": datetime(2026, 7, 11, 12)}, "started_at"),
        ({"completed_at": datetime(2026, 7, 11, 12)}, "completed_at"),
        ({"completed_at": NOW - timedelta(seconds=1)}, "precede"),
        ({"output_digest": ""}, "output_digest"),
        ({"error_code": ""}, "error_code"),
        ({"status": ExecutionStatus.UNKNOWN}, "cannot claim output"),
        ({"output_digest": None}, "requires an output"),
    ),
)
def test_execution_result_rejects_incoherent_terminal_state(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_result(_preparation().binding), **changes)


def test_unknown_execution_result_allows_only_uncertain_metadata() -> None:
    result = _result(
        _preparation().binding,
        status=ExecutionStatus.UNKNOWN,
        output_digest=None,
        observed_effects=(),
        error_code="outcome_unknown",
    )

    assert deserialize_execution_result(serialize_execution_result(result)) == result


@pytest.mark.parametrize(
    ("build", "message"),
    (
        (
            lambda: replace(_preparation().binding, schema_version="execution-binding/v99"),
            "unsupported",
        ),
        (lambda: replace(_preparation().binding, run_id=""), "run_id"),
        (
            lambda: replace(_preparation(), schema_version="execution-preparation/v99"),
            "unsupported",
        ),
        (lambda: replace(_preparation(), run_id=""), "run_id"),
        (
            lambda: replace(
                _preparation(),
                definition=replace(_definition(), name="different"),
            ),
            "affordance definition",
        ),
        (
            lambda: replace(_preparation(), authorized_action_digest=DIGEST),
            "authorized action",
        ),
    ),
)
def test_execution_binding_and_preparation_fail_closed(
    build: Callable[[], object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build()


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"schema_version": "execution-recovery-authorization/v99"}, "unsupported"),
        ({"reason": ""}, "reason"),
        ({"expected_fencing_revision": 0}, "positive"),
        ({"authorized_at": datetime(2026, 7, 11, 12)}, "timezone-aware"),
        ({"original_worker_stopped": False}, "worker stopped"),
    ),
)
def test_manual_recovery_authorization_requires_an_exact_operator_attestation(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_recovery_authorization(_claim()), **changes)


@pytest.mark.parametrize(
    ("build", "message"),
    (
        (
            lambda: replace(_claim(), operation=cast("ExecutionOperation", "execute")),
            "recognized",
        ),
        (lambda: replace(_claim(), journal_position=0), "positive"),
        (lambda: replace(_claim(), fencing_revision=0), "positive"),
        (lambda: replace(_claim(), claim_token=""), "claim token"),
        (
            lambda: replace(_claim(), acquired_at=datetime(2026, 7, 11, 12)),
            "timezone-aware",
        ),
        (
            lambda: replace(_claim(), previous=_result(_preparation().binding)),
            "initial execution",
        ),
    ),
)
def test_execution_claim_rejects_invalid_ownership_state(
    build: Callable[[], object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build()


def test_reconciliation_claim_requires_matching_unknown_result() -> None:
    binding = _preparation().binding
    mismatched = replace(
        _result(
            binding,
            status=ExecutionStatus.UNKNOWN,
            output_digest=None,
            observed_effects=(),
        ),
        invocation_id="invocation:other",
    )
    with pytest.raises(ValueError, match="invocation_id"):
        replace(
            _claim(),
            operation=ExecutionOperation.RECONCILE,
            previous=mismatched,
        )

    with pytest.raises(ValueError, match="only an unknown"):
        replace(
            _claim(),
            operation=ExecutionOperation.RECONCILE,
            previous=_result(binding),
        )


def test_recovery_requires_preparation_claim_and_authorization_to_share_identity() -> None:
    claim = _claim()
    authorization = _recovery_authorization(claim)
    other_preparation = replace(_preparation(), run_id="run:other")

    with pytest.raises(ValueError, match="preparation"):
        ExecutionRecovery(other_preparation, claim, authorization)
    with pytest.raises(ValueError, match="authorization"):
        ExecutionRecovery(
            _preparation(),
            claim,
            replace(authorization, execution_identity_digest=DIGEST),
        )


@pytest.mark.parametrize(
    ("build", "message"),
    (
        (
            lambda: replace(
                _entry(),
                status=cast("ExecutionJournalStatus", "prepared"),
            ),
            "recognized",
        ),
        (lambda: replace(_entry(), journal_position=0), "positive"),
        (
            lambda: replace(_entry(), created_at=datetime(2026, 7, 11, 12)),
            "created_at",
        ),
        (
            lambda: replace(_entry(), updated_at=datetime(2026, 7, 11, 12)),
            "updated_at",
        ),
        (lambda: replace(_entry(), updated_at=NOW - timedelta(seconds=1)), "precede"),
        (
            lambda: replace(_entry(), current_result=_result(_preparation().binding)),
            "prepared execution",
        ),
        (
            lambda: replace(
                _entry(),
                status=ExecutionJournalStatus.SUCCEEDED,
                current_result=None,
            ),
            "requires a result",
        ),
        (
            lambda: replace(
                _entry(),
                status=ExecutionJournalStatus.FAILED,
                current_result=_result(_preparation().binding),
            ),
            "does not match",
        ),
        (
            lambda: replace(_entry(), active_claim=replace(_claim(), journal_position=2)),
            "different journal entry",
        ),
    ),
)
def test_execution_journal_entry_rejects_impossible_snapshots(
    build: Callable[[], object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build()


def test_execution_journal_entry_rejects_claim_from_another_binding() -> None:
    entry = _entry()
    other_claim = replace(
        _claim(),
        binding=replace(_preparation().binding, run_id="run:other"),
    )

    with pytest.raises(ValueError, match="different execution binding"):
        replace(entry, active_claim=other_claim)


@pytest.mark.parametrize(
    ("artifact", "message"),
    (
        ("not-json", "valid JSON"),
        (b"\xff", "valid JSON"),
        (canonical_json({"schema_version": "execution-preparation/v1"}), "unexpected"),
    ),
)
def test_execution_preparation_decoder_rejects_malformed_envelopes(
    artifact: str | bytes,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        deserialize_execution_preparation(artifact)


def test_execution_preparation_decoder_rejects_tampering() -> None:
    preparation = _preparation()
    base = json.loads(serialize_execution_preparation(preparation))
    cases: tuple[tuple[dict[str, object], str], ...] = (
        ({**base, "extra": True}, "unexpected fields"),
        ({**base, "schema_version": "execution-preparation/v99"}, "unsupported"),
        ({**base, "invocation": []}, "invocation has unexpected"),
        (
            {**base, "invocation": {**base["invocation"], "arguments": {}}},
            "arguments must be an array",
        ),
        (
            {
                **base,
                "invocation": {
                    **base["invocation"],
                    "arguments": [{"name": "path", "value": {"nested": True}}],
                },
            },
            "JSON scalar",
        ),
        (
            {**base, "invocation": {**base["invocation"], "requested_at": "not-a-date"}},
            "ISO timestamp",
        ),
        (
            {
                **base,
                "definition": {**base["definition"], "timeout_seconds": True},
            },
            "must be a number",
        ),
        (
            {
                **base,
                "definition": {**base["definition"], "side_effect_class": "unsafe"},
            },
            "side_effect_class is invalid",
        ),
        (
            {
                **base,
                "definition": {
                    **base["definition"],
                    "arguments": [{"name": "path", "required": "yes"}],
                },
            },
            "required must be a boolean",
        ),
    )

    for artifact, message in cases:
        with pytest.raises(ValueError, match=message):
            deserialize_execution_preparation(canonical_json(artifact))

    with pytest.raises(ValueError, match="identity"):
        deserialize_execution_preparation(
            serialize_execution_preparation(preparation),
            expected_preparation_id=DIGEST,
        )


def test_execution_result_decoder_rejects_tampering() -> None:
    result = _result(_preparation().binding)
    base = json.loads(serialize_execution_result(result))
    cases: tuple[tuple[dict[str, object], str], ...] = (
        ({**base, "extra": True}, "unexpected fields"),
        ({**base, "schema_version": "execution-result/v99"}, "unsupported"),
        ({**base, "observed_effects": {}}, "must be an array"),
        ({**base, "observed_effects": [{"subject": "task"}]}, "unexpected fields"),
        (
            {
                **base,
                "observed_effects": [{"subject": "task", "predicate": "done", "value": [True]}],
            },
            "JSON scalar",
        ),
        ({**base, "reconciled": "false"}, "must be a boolean"),
        ({**base, "status": "lost"}, "invalid enum or timestamp"),
        ({**base, "started_at": "not-a-date"}, "invalid enum or timestamp"),
        ({**base, "invocation_id": ""}, "non-empty string"),
    )

    for artifact, message in cases:
        with pytest.raises(ValueError, match=message):
            deserialize_execution_result(canonical_json(artifact))

    with pytest.raises(ValueError, match="identity"):
        deserialize_execution_result(
            serialize_execution_result(result),
            expected_result_id=DIGEST,
        )


def test_authorization_artifacts_round_trip_scalar_arguments_and_proof_links() -> None:
    proposal = _proposal(
        arguments=(
            ActionArgument("none", None),
            ActionArgument("enabled", True),
            ActionArgument("retries", 2),
            ActionArgument("threshold", 0.75),
        )
    )
    evaluation = _evaluation()
    decision = _decision(evaluation, proof_ids=(evaluation.proofs[0].proof_id,))

    assert decode_action_proposal(encode_action_proposal(proposal)) == proposal
    assert decode_authorization_decision(encode_authorization_decision(decision)) == decision


def test_authorization_artifact_decoders_reject_ambiguous_shapes_and_values() -> None:
    proposal = _proposal()
    proposal_base = json.loads(encode_action_proposal(proposal))
    proposal_cases: tuple[tuple[object, str], ...] = (
        ([], "JSON object"),
        ({**proposal_base, "extra": True}, "fields differ"),
        ({**proposal_base, "arguments": {}}, "JSON array"),
        ({**proposal_base, "arguments": ["path"]}, "JSON object"),
        (
            {**proposal_base, "arguments": [{"name": "path", "value": []}]},
            "JSON scalar",
        ),
        ({**proposal_base, "evidence_event_ids": [""]}, "domain contract"),
        (
            {**proposal_base, "evidence_event_ids": ["event:1", "event:1"]},
            "domain contract",
        ),
        ({**proposal_base, "action_digest": DIGEST}, "action_digest"),
    )
    for artifact, message in proposal_cases:
        with pytest.raises(AuthorizationArtifactCodecError, match=message):
            decode_action_proposal(canonical_json_bytes(artifact))

    decision_base = json.loads(encode_authorization_decision(_decision(_evaluation())))
    decision_cases: tuple[tuple[object, str], ...] = (
        ({**decision_base, "findings": {}}, "JSON array"),
        ({**decision_base, "findings": ["allow"]}, "JSON object"),
        (
            {**decision_base, "findings": [{"outcome": "allow"}]},
            "fields differ",
        ),
        (
            {
                **decision_base,
                "findings": [{"outcome": "allow", "code": "", "message": "ok", "proof_ids": []}],
            },
            "violates its contract",
        ),
        ({**decision_base, "authorized_read_only": "yes"}, "domain contract"),
        ({**decision_base, "outcome": "maybe"}, "domain contract"),
        ({**decision_base, "evaluated_at": "not-a-date"}, "domain contract"),
        ({**decision_base, "evaluated_at": "2026-07-11T12:00:00"}, "domain contract"),
        ({**decision_base, "decision_id": DIGEST}, "decision_id"),
    )
    for artifact, message in decision_cases:
        with pytest.raises(AuthorizationArtifactCodecError, match=message):
            decode_authorization_decision(canonical_json_bytes(artifact))


def test_authorization_artifacts_require_bytes_and_valid_utf8() -> None:
    with pytest.raises(TypeError, match="bytes"):
        decode_action_proposal(cast("bytes", "not-bytes"))
    with pytest.raises(AuthorizationArtifactCodecError, match="UTF-8 JSON"):
        decode_authorization_decision(b"\xff")


def test_constraint_artifact_rejects_tampered_proof_and_evaluation_identities() -> None:
    evaluation = _evaluation()
    base = json.loads(encode_constraint_evaluation(evaluation))

    forged_proof = {**base, "proofs": [{**base["proofs"][0], "proof_id": DIGEST}]}
    with pytest.raises(ConstraintArtifactCodecError, match="proof_id"):
        decode_constraint_evaluation(canonical_json_bytes(forged_proof))

    forged_evaluation = {**base, "evaluation_id": DIGEST}
    with pytest.raises(ConstraintArtifactCodecError, match="evaluation_id"):
        decode_constraint_evaluation(canonical_json_bytes(forged_evaluation))


def test_constraint_artifact_decoder_rejects_ambiguous_shapes_and_values() -> None:
    base = json.loads(encode_constraint_evaluation(_evaluation()))
    cases: tuple[tuple[object, str], ...] = (
        ([], "JSON object"),
        ({**base, "extra": True}, "fields differ"),
        ({**base, "proofs": {}}, "JSON array"),
        ({**base, "proofs": ["proof"]}, "JSON object"),
        ({**base, "proofs": [{**base["proofs"][0], "extra": True}]}, "fields differ"),
        (
            {**base, "proofs": [{**base["proofs"][0], "outcome": "maybe"}]},
            "violates its contract",
        ),
        (
            {**base, "proofs": [{**base["proofs"][0], "evidence_event_ids": {}}]},
            "violates its contract",
        ),
        (
            {**base, "proofs": [{**base["proofs"][0], "evidence_event_ids": [""]}]},
            "violates its contract",
        ),
        (
            {**base, "proofs": [{**base["proofs"][0], "evaluated_at": "not-a-date"}]},
            "violates its contract",
        ),
        (
            {**base, "proofs": [{**base["proofs"][0], "evaluated_at": "2026-07-11T12:00:00"}]},
            "violates its contract",
        ),
        ({**base, "evaluated_at": "2026-07-11T13:00:00+00:00"}, "domain contract"),
    )
    for artifact, message in cases:
        with pytest.raises(ConstraintArtifactCodecError, match=message):
            decode_constraint_evaluation(canonical_json_bytes(artifact))


def test_constraint_artifacts_require_bytes_and_valid_utf8() -> None:
    with pytest.raises(TypeError, match="bytes"):
        decode_constraint_evaluation(cast("bytes", "not-bytes"))
    with pytest.raises(ConstraintArtifactCodecError, match="UTF-8 JSON"):
        decode_constraint_evaluation(b"\xff")


def _definition() -> AffordanceDefinition:
    return AffordanceDefinition(
        name="inspect",
        adapter_id="fixture",
        side_effect_class=SideEffectClass.READ_ONLY,
        timeout_seconds=10,
        arguments=(AffordanceArgumentSpec("path"),),
    )


def _invocation() -> AffordanceInvocation:
    return AffordanceInvocation(
        invocation_id="invocation:1",
        proposal_id="proposal:1",
        affordance="inspect",
        arguments=(AffordanceArgument("path", "README.md"),),
        idempotency_key="key:1",
        requested_at=NOW,
    )


def _preparation() -> ExecutionPreparation:
    invocation = _invocation()
    return ExecutionPreparation(
        run_id="run:1",
        invocation=invocation,
        definition=_definition(),
        authorization_decision_id="authorization:1",
        authorized_action_digest=invocation.action_digest,
        adapter_contract_version="fixture/v1",
    )


def _result(
    binding: ExecutionBinding,
    *,
    status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    output_digest: str | None = DIGEST,
    observed_effects: tuple[ObservedEffect, ...] = (ObservedEffect("task:1", "inspected", True),),
    error_code: str | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        invocation_id=binding.invocation_id,
        proposal_id=binding.proposal_id,
        authorization_decision_id=binding.authorization_decision_id,
        affordance=binding.affordance,
        adapter_id=binding.adapter_id,
        idempotency_key=binding.idempotency_key,
        authorized_action_digest=binding.authorized_action_digest,
        execution_identity_digest=binding.execution_identity_digest,
        status=status,
        started_at=NOW,
        completed_at=NOW,
        output_digest=output_digest,
        observed_effects=observed_effects,
        error_code=error_code,
        reconciled=False,
    )


def _claim() -> ExecutionClaim:
    return ExecutionClaim(
        journal_position=1,
        binding=_preparation().binding,
        fencing_revision=1,
        claim_token="claim:1",
        operation=ExecutionOperation.EXECUTE,
        acquired_at=NOW,
    )


def _entry() -> ExecutionJournalEntry:
    return ExecutionJournalEntry(
        journal_position=1,
        binding=_preparation().binding,
        status=ExecutionJournalStatus.PREPARED,
        current_result=None,
        active_claim=_claim(),
        created_at=NOW,
        updated_at=NOW,
    )


def _recovery_authorization(claim: ExecutionClaim) -> ExecutionRecoveryAuthorization:
    return ExecutionRecoveryAuthorization(
        execution_identity_digest=claim.binding.execution_identity_digest,
        expected_claim_token=claim.claim_token,
        expected_fencing_revision=claim.fencing_revision,
        authorized_by="operator:test",
        reason="worker stopped before recording the outcome",
        authorized_at=NOW,
        original_worker_stopped=True,
    )


def _proposal(
    *,
    arguments: tuple[ActionArgument, ...] = (ActionArgument("path", "README.md"),),
) -> ActionProposal:
    return ActionProposal(
        proposal_id="proposal:1",
        context_frame_id=DIGEST,
        affordance="inspect",
        arguments=arguments,
        rationale="inspect cited evidence",
        evidence_event_ids=("event:1",),
    )


def _evaluation() -> ConstraintEvaluation:
    proof = ConstraintProof(
        constraint_id="constraint:1",
        constraint_definition_digest=DIGEST,
        outcome=ConstraintOutcome.SATISFIED,
        code="satisfied",
        message="constraint satisfied",
        evidence_event_ids=("event:1",),
        evaluated_at=NOW,
    )
    return ConstraintEvaluation(_proposal().context_frame_id, (proof,), NOW)


def _decision(
    evaluation: ConstraintEvaluation,
    *,
    proof_ids: tuple[str, ...] = (),
) -> AuthorizationDecision:
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
        findings=(
            AuthorizationFinding(
                AuthorizationOutcome.ALLOW,
                "allowed",
                "fixture",
                proof_ids,
            ),
        ),
        evaluated_at=NOW,
        approval_granted=False,
    )
