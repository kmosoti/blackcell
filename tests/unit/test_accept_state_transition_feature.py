from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from blackcell.features.accept_state_transition import (
    AcceptStateTransition,
    AuthorizationReference,
    EvaluationReference,
    ExecutionReference,
    ProposalReference,
    StateSnapshotReference,
    StateTransitionAcceptor,
    StateTransitionArtifactCodecError,
    StateTransitionIntegrityError,
    TransitionAcceptanceStatus,
    TransitionActionArgument,
    TransitionAuthorizationOutcome,
    TransitionClaim,
    TransitionEpistemicStatus,
    TransitionEvaluationFinding,
    TransitionEvaluationVerdict,
    TransitionEventReference,
    TransitionExecutionStatus,
    TransitionStateView,
    accepted_state_transition_payload,
    decode_accepted_state_transition,
    encode_accepted_state_transition,
)
from blackcell.kernel import JsonScalar
from blackcell.kernel._json import canonical_json_bytes, json_digest

NOW = datetime(2026, 7, 12, 18, tzinfo=UTC)
RUN_ID = "run:transition"
STREAM_ID = "observation:repository:worktree"
EXECUTION_EVENT_ID = "event:execution"


def test_pass_and_fail_both_accept_independently_observed_facts() -> None:
    passed = StateTransitionAcceptor().handle(_command())
    failed = StateTransitionAcceptor().handle(
        _command(verdict=TransitionEvaluationVerdict.FAIL, accepted_value=False)
    )

    assert passed.status is TransitionAcceptanceStatus.ACCEPTED
    assert failed.status is TransitionAcceptanceStatus.ACCEPTED
    assert passed.transition is not None
    assert failed.transition is not None
    assert passed.transition.evaluation.verdict is TransitionEvaluationVerdict.PASS
    assert failed.transition.evaluation.verdict is TransitionEvaluationVerdict.FAIL
    assert passed.transition.accepted_claim_ids == ("claim:outcome",)
    assert passed.transition.accepted_source_event_ids == ("event:outcome",)
    assert passed.transition.claim_deltas[0].before[0].claim_id == "claim:initial"
    assert {item.claim_id for item in passed.transition.claim_deltas[0].after} == {
        "claim:initial",
        "claim:outcome",
    }
    assert passed.transition.conflict_changes[0].before is None
    assert passed.transition.conflict_changes[0].after is not None


@pytest.mark.parametrize(
    ("verdict", "code"),
    (
        (TransitionEvaluationVerdict.INCONCLUSIVE, "evaluation-inconclusive"),
        (TransitionEvaluationVerdict.NOT_EVALUATED, "evaluation-not-evaluated"),
    ),
)
def test_nondefinitive_evaluations_are_typed_not_accepted(verdict, code: str) -> None:
    command = _command()
    if verdict is TransitionEvaluationVerdict.INCONCLUSIVE:
        command = _fold_shaped_inconclusive_command()
    else:
        command = replace(
            command,
            evaluation=_nondefinitive_evaluation(verdict, command.evaluation),
        )
        command = replace(
            command,
            authorization=replace(
                command.authorization,
                outcome=TransitionAuthorizationOutcome.DENY,
            ),
            execution=None,
            outcome_state=None,
            triggering_events=(),
        )

    result = StateTransitionAcceptor().handle(command)

    assert result.status is TransitionAcceptanceStatus.NOT_ACCEPTED
    assert result.code == code
    assert result.transition is None


def test_definitive_verdict_with_unknown_execution_raises_integrity_error() -> None:
    command = _command()
    assert command.execution is not None
    unknown = _execution(
        command.proposal,
        command.authorization,
        status=TransitionExecutionStatus.UNKNOWN,
    )
    command = replace(
        command,
        execution=unknown,
    )

    with pytest.raises(StateTransitionIntegrityError):
        StateTransitionAcceptor().handle(command)


def test_inconclusive_unknown_execution_is_typed_not_accepted() -> None:
    command = _command()
    assert command.execution is not None
    unknown = _execution(
        command.proposal,
        command.authorization,
        status=TransitionExecutionStatus.UNKNOWN,
    )
    command = replace(
        command,
        execution=unknown,
        outcome_state=None,
        triggering_events=(),
        evaluation=_unknown_evaluation(command.evaluation, unknown),
    )

    result = StateTransitionAcceptor().handle(command)

    assert result.status is TransitionAcceptanceStatus.NOT_ACCEPTED
    assert result.code == "execution-unknown"
    assert result.transition is None


def test_nondefinitive_verdict_cannot_hide_identity_or_branch_corruption() -> None:
    command = _command()
    inconclusive = replace(
        command,
        proposal=replace(command.proposal, proposal_digest=_digest("forged")),
        evaluation=_nondefinitive_evaluation(
            TransitionEvaluationVerdict.INCONCLUSIVE,
            command.evaluation,
        ),
    )
    with pytest.raises(StateTransitionIntegrityError, match="integrity-mismatch"):
        StateTransitionAcceptor().handle(inconclusive)

    invalid_not_evaluated = replace(
        command,
        evaluation=_nondefinitive_evaluation(
            TransitionEvaluationVerdict.NOT_EVALUATED,
            command.evaluation,
        ),
        execution=None,
        outcome_state=None,
        triggering_events=(),
    )
    with pytest.raises(StateTransitionIntegrityError, match="integrity-mismatch"):
        StateTransitionAcceptor().handle(invalid_not_evaluated)


def test_definitive_evaluation_cannot_be_created_without_bound_evidence() -> None:
    with pytest.raises(StateTransitionIntegrityError, match="requires bound claims"):
        TransitionEvaluationFinding(
            criterion_id="ready",
            required=True,
            verdict=TransitionEvaluationVerdict.PASS,
            code="expected-value-observed",
            expected_value=True,
            actual_present=False,
            actual_value=None,
        )

    reference = _command().evaluation
    with pytest.raises(StateTransitionIntegrityError, match="requires evidence_binding_id"):
        replace(reference, evidence_binding_id=None)


def test_canonical_evaluation_identity_rejects_forged_id_spec_and_owner_id() -> None:
    evaluation = _command().evaluation
    with pytest.raises(StateTransitionIntegrityError, match="evaluation_id"):
        _rebuild_evaluation(
            evaluation,
            evaluation_id_override=_digest("forged-evaluation-id"),
        )
    with pytest.raises(StateTransitionIntegrityError, match="evaluation_id"):
        replace(evaluation, evaluation_spec_id=_digest("forged-spec"))
    with pytest.raises(StateTransitionIntegrityError, match="evaluation_id"):
        replace(evaluation, owner_observation_id="outcome-observation:forged")


@pytest.mark.parametrize(
    "drop",
    ("id", "digest", "artifact", "evidence"),
)
def test_terminal_evaluation_requires_complete_owner_evidence(drop: str) -> None:
    evaluation = _command().evaluation
    with pytest.raises(StateTransitionIntegrityError, match="terminal evaluation requires"):
        _rebuild_evaluation(
            evaluation,
            drop_owner_id=drop == "id",
            drop_owner_digest=drop == "digest",
            drop_owner_artifact=drop == "artifact",
            drop_evidence_binding=drop == "evidence",
        )


def test_owner_identity_and_artifact_digests_are_distinct_refs() -> None:
    command = _command()
    evaluation = command.evaluation
    assert evaluation.owner_observation_digest != evaluation.owner_observation_artifact_digest
    changed = _rebuild_evaluation(
        evaluation,
        owner_observation_artifact_digest=_digest("different-owner-artifact"),
    )
    assert changed.evaluation_id == evaluation.evaluation_id
    revised = StateTransitionAcceptor().handle(replace(command, evaluation=changed)).transition
    original = StateTransitionAcceptor().handle(command).transition
    assert revised is not None and original is not None
    assert revised.transition_id != original.transition_id


def test_paired_execution_and_evaluation_binding_forgery_is_rejected() -> None:
    command = _command()
    forged_proposal = replace(
        command.proposal,
        proposal_digest=_digest("paired-forgery"),
    )
    forged_execution = _execution(forged_proposal, command.authorization)
    forged_evaluation = _rebuild_evaluation(
        command.evaluation,
        execution_binding_id=forged_execution.execution_binding_id,
    )
    forged = replace(
        command,
        execution=forged_execution,
        evaluation=forged_evaluation,
    )
    with pytest.raises(StateTransitionIntegrityError, match="integrity-mismatch"):
        StateTransitionAcceptor().handle(forged)


def test_execution_result_digest_and_canonical_binding_are_independently_fenced() -> None:
    execution = _command().execution
    assert execution is not None
    with pytest.raises(StateTransitionIntegrityError, match="result digest"):
        replace(execution, execution_result_digest=_digest("other-result-artifact"))
    with pytest.raises(StateTransitionIntegrityError, match="execution_binding_id"):
        replace(execution, execution_binding_id=_digest("forged-binding"))


def test_terminal_inconclusive_retains_owner_and_exact_source_evidence() -> None:
    command = _fold_shaped_inconclusive_command()
    evaluation = command.evaluation
    result = StateTransitionAcceptor().handle(command)

    assert result.status is TransitionAcceptanceStatus.NOT_ACCEPTED
    assert evaluation.execution_status is TransitionExecutionStatus.SUCCEEDED
    assert evaluation.owner_observation_id == "outcome-observation:1"
    assert evaluation.owner_observation_digest is not None
    assert evaluation.owner_observation_artifact_digest is not None
    assert evaluation.evidence_binding_id is not None
    assert evaluation.findings[0].source_event_ids == ("event:outcome",)


def test_claim_free_terminal_inconclusive_allows_unchanged_state_stream_position() -> None:
    command = _fold_shaped_inconclusive_command()

    result = StateTransitionAcceptor().handle(command)

    assert result.status is TransitionAcceptanceStatus.NOT_ACCEPTED
    assert result.code == "evaluation-inconclusive"
    assert result.transition is None
    assert (
        command.outcome_state is not None
        and command.outcome_state.reference.last_source_stream_sequence
        == command.initial_state.reference.last_source_stream_sequence
    )
    assert (
        command.triggering_events[0].stream_sequence
        > command.outcome_state.reference.last_source_stream_sequence
    )


@pytest.mark.parametrize(
    "corruption",
    ("event-type", "stream", "correlation", "causation", "global", "sequence", "regression"),
)
def test_claim_free_terminal_inconclusive_still_rejects_corrupt_evidence(
    corruption: str,
) -> None:
    command = _fold_shaped_inconclusive_command()
    event = command.triggering_events[0]
    if corruption == "event-type":
        event = replace(event, event_type="observation.recorded")
    elif corruption == "stream":
        event = replace(event, stream_id="observation:other")
    elif corruption == "correlation":
        event = replace(event, correlation_id="run:other")
    elif corruption == "causation":
        event = replace(event, causation_id="event:other")
    elif corruption == "global":
        event = replace(event, global_position=14)
    elif corruption == "sequence":
        event = replace(event, stream_sequence=2)
    else:
        outcome = command.outcome_state
        assert outcome is not None
        command = replace(
            command,
            outcome_state=TransitionStateView(
                replace(outcome.reference, last_source_stream_sequence=1),
                (),
            ),
        )
    if corruption != "regression":
        command = replace(command, triggering_events=(event,))
    with pytest.raises(StateTransitionIntegrityError, match="integrity-mismatch"):
        StateTransitionAcceptor().handle(command)


def test_canonical_finding_rejects_value_confidence_code_and_evidence_forgeries() -> None:
    passed = _command().evaluation.findings[0]
    failed = _command(
        verdict=TransitionEvaluationVerdict.FAIL,
        accepted_value=False,
    ).evaluation.findings[0]
    invalid = (
        {"actual_present": False},
        {"actual_confidence": True},
        {"actual_confidence": 2.0},
        {"actual_present": False, "actual_value": None},
        {"observed_claim_ids": ("",)},
        {"observed_claim_ids": ("claim:outcome", "claim:outcome")},
        {"actual_value": False},
        {"code": "forged-code"},
    )
    for changes in invalid:
        with pytest.raises((TypeError, ValueError, StateTransitionIntegrityError)):
            replace(passed, **changes)
    with pytest.raises(ValueError, match="unexpected value"):
        replace(failed, actual_value=True)

    blocked = _nondefinitive_evaluation(
        TransitionEvaluationVerdict.NOT_EVALUATED,
        _command().evaluation,
    ).findings[0]
    with pytest.raises(ValueError, match="code is not recognized"):
        replace(blocked, code="forged-code")
    with pytest.raises(ValueError, match="cannot claim outcome evidence"):
        replace(blocked, source_event_ids=("event:outcome",))


def test_inconclusive_finding_codes_enforce_their_canonical_evidence_shapes() -> None:
    terminal = _nondefinitive_evaluation(
        TransitionEvaluationVerdict.INCONCLUSIVE,
        _command().evaluation,
    ).findings[0]
    with pytest.raises(ValueError, match="requires only source events"):
        replace(terminal, source_event_ids=())
    with pytest.raises(ValueError, match="unobserved inconclusive"):
        replace(terminal, code="no-fresh-outcome-evidence")
    with pytest.raises(ValueError, match="conflicting finding"):
        replace(terminal, code="conflicting-fresh-outcome-evidence")
    with pytest.raises(ValueError, match="low-confidence finding"):
        replace(terminal, code="outcome-confidence-below-threshold")
    with pytest.raises(ValueError, match="code is not recognized"):
        replace(terminal, code="invented-inconclusive-code")


@pytest.mark.parametrize("mismatch", ("run", "initial", "branch", "time"))
def test_evaluation_run_initial_branch_and_time_mismatches_are_rejected(
    mismatch: str,
) -> None:
    command = _command()
    if mismatch == "run":
        command = replace(
            command,
            evaluation=_rebuild_evaluation(command.evaluation, run_id="run:other"),
        )
    elif mismatch == "initial":
        command = replace(
            command,
            evaluation=_rebuild_evaluation(
                command.evaluation,
                initial_state_position=9,
            ),
        )
    elif mismatch == "branch":
        command = replace(
            command,
            authorization=replace(
                command.authorization,
                outcome=TransitionAuthorizationOutcome.DENY,
            ),
        )
    else:
        command = replace(
            command,
            evaluation=_rebuild_evaluation(
                command.evaluation,
                evaluated_at=NOW + timedelta(minutes=3),
            ),
        )
    with pytest.raises(StateTransitionIntegrityError, match="integrity-mismatch"):
        StateTransitionAcceptor().handle(command)


@pytest.mark.parametrize("field", ("proposal", "authorization", "execution", "evaluation"))
def test_identity_mismatches_fail_closed(field: str) -> None:
    command = _command()
    if field == "proposal":
        proposal = replace(command.proposal, proposal_digest=_digest("changed-proposal"))
        command = replace(command, proposal=proposal)
    elif field == "authorization":
        authorization = replace(
            command.authorization,
            proposal_digest=_digest("changed-authorization"),
        )
        command = replace(command, authorization=authorization)
    elif field == "execution":
        assert command.execution is not None
        forged_authorization = replace(
            command.authorization,
            decision_id=_digest("changed-decision"),
        )
        execution = _execution(command.proposal, forged_authorization)
        command = replace(command, execution=execution)
    else:
        evaluation = _rebuild_evaluation(
            command.evaluation,
            execution_binding_id=_digest("changed-binding"),
        )
        command = replace(command, evaluation=evaluation)

    with pytest.raises(StateTransitionIntegrityError, match="integrity-mismatch"):
        StateTransitionAcceptor().handle(command)


def test_trigger_event_and_claim_provenance_must_match_exactly() -> None:
    command = _command()
    wrong_event = replace(command.triggering_events[0], causation_id="event:other")
    with pytest.raises(StateTransitionIntegrityError, match="integrity-mismatch"):
        StateTransitionAcceptor().handle(replace(command, triggering_events=(wrong_event,)))


def test_snapshot_and_action_cross_boundary_integrity_failures_are_not_accepted() -> None:
    command = _command()
    outcome = command.outcome_state
    assert outcome is not None

    cases = (
        replace(
            command,
            authorization=replace(
                command.authorization,
                outcome=TransitionAuthorizationOutcome.DENY,
            ),
        ),
        replace(
            command,
            outcome_state=replace(
                outcome,
                reference=replace(outcome.reference, domain="other"),
                claims=(),
            ),
        ),
        replace(
            command,
            outcome_state=replace(
                outcome,
                reference=replace(
                    outcome.reference,
                    cutoff_global_position=10,
                    last_source_stream_sequence=2,
                ),
                claims=command.initial_state.claims,
            ),
        ),
        replace(command, outcome_state=None),
        replace(command, execution=None),
        replace(
            command,
            outcome_state=replace(
                outcome,
                reference=replace(
                    outcome.reference,
                    effective_time_cutoff=None,
                ),
            ),
        ),
        replace(
            command,
            initial_state=replace(
                command.initial_state,
                reference=replace(
                    command.initial_state.reference,
                    effective_time_cutoff=None,
                ),
            ),
        ),
    )

    for case in cases:
        with pytest.raises(StateTransitionIntegrityError, match="integrity-mismatch"):
            StateTransitionAcceptor().handle(case)


def test_state_view_and_claim_contracts_reject_ambiguous_evidence() -> None:
    claim = _claim(
        "claim:one",
        "event:one",
        value=True,
        position=1,
        sequence=1,
        effective_at=NOW,
    )
    reference = _snapshot("state", 1, 1, NOW)
    with pytest.raises(ValueError, match="claim ids must be unique"):
        TransitionStateView(reference, (claim, claim))
    with pytest.raises(ValueError, match="snapshot scope"):
        TransitionStateView(reference, (replace(claim, domain="other"),))
    with pytest.raises(ValueError, match="global cutoff"):
        TransitionStateView(reference, (replace(claim, global_position=2),))
    with pytest.raises(ValueError, match="stream cutoff"):
        TransitionStateView(reference, (replace(claim, stream_sequence=2),))
    with pytest.raises(ValueError, match="effective-time cutoff"):
        TransitionStateView(
            reference,
            (replace(claim, effective_at=NOW + timedelta(seconds=1)),),
        )
    with pytest.raises(TypeError, match="confidence must be numeric"):
        replace(claim, confidence=True)
    with pytest.raises(ValueError, match="between zero and one"):
        replace(claim, confidence=1.1)
    with pytest.raises(ValueError, match="observed claims"):
        replace(claim, unknown_reason="expired")
    with pytest.raises(ValueError, match="unknown claims require"):
        replace(
            claim,
            epistemic_status=TransitionEpistemicStatus.UNKNOWN,
            unknown_reason="expired",
        )
    with pytest.raises(ValueError, match="only a correction"):
        replace(claim, supersedes_claim_ids=("claim:old",))
    with pytest.raises(ValueError, match="must identify superseded"):
        replace(claim, correction_id="correction:1")


def test_proposal_evaluation_and_command_contracts_normalize_or_reject() -> None:
    proposal = _proposal()
    with pytest.raises(ValueError, match="context_frame_id must be a SHA-256 digest"):
        replace(proposal, context_frame_id="context:non-digest")
    with pytest.raises(ValueError, match="argument names must be unique"):
        replace(proposal, arguments=(proposal.arguments[0], proposal.arguments[0]))
    with pytest.raises(ValueError, match="does not match its action"):
        replace(proposal, action_digest=_digest("wrong-action"))

    evaluation = _command().evaluation
    duplicate = (*evaluation.findings, evaluation.findings[0])
    with pytest.raises(ValueError, match="uniquely identified"):
        replace(evaluation, findings=duplicate)
    with pytest.raises(StateTransitionIntegrityError, match="required finding"):
        replace(
            evaluation,
            findings=(replace(evaluation.findings[0], required=False),),
        )
    with pytest.raises(StateTransitionIntegrityError, match="does not match"):
        replace(
            evaluation,
            verdict=TransitionEvaluationVerdict.FAIL,
        )

    command = _command()
    with pytest.raises(
        ValueError,
        match="constraint_evaluation_id must be a SHA-256 digest",
    ):
        replace(
            command.authorization,
            constraint_evaluation_id="constraint:non-digest",
        )
    with pytest.raises(ValueError, match="global positions must be unique"):
        replace(
            command,
            triggering_events=(
                command.triggering_events[0],
                replace(command.triggering_events[0], event_id="event:duplicate-position"),
            ),
        )

    outcome = command.outcome_state
    assert outcome is not None
    claims = tuple(
        replace(item, global_position=14) if item.claim_id == "claim:outcome" else item
        for item in outcome.claims
    )
    mismatched = replace(command, outcome_state=TransitionStateView(outcome.reference, claims))
    with pytest.raises(StateTransitionIntegrityError, match="integrity-mismatch"):
        StateTransitionAcceptor().handle(mismatched)


def test_unrelated_concurrent_evidence_is_allowed_but_excluded_from_deltas() -> None:
    command = _command(include_concurrent=True)

    result = StateTransitionAcceptor().handle(command)

    assert result.transition is not None
    delta = result.transition.claim_deltas[0]
    assert {item.claim_id for item in delta.after} == {"claim:initial", "claim:outcome"}
    assert "claim:concurrent" not in {item.claim_id for item in (*delta.before, *delta.after)}


def test_transition_binds_complete_refs_and_is_deterministic() -> None:
    command = _command(include_concurrent=True)

    first = StateTransitionAcceptor().handle(command).transition
    second = StateTransitionAcceptor().handle(command).transition

    assert first is not None and second is not None
    assert first == second
    assert first.transition_id == second.transition_id
    assert encode_accepted_state_transition(first) == encode_accepted_state_transition(second)
    payload = accepted_state_transition_payload(first)
    assert payload["initial_state"] == {
        "snapshot_digest": _digest("initial-snapshot"),
        "domain": "repository",
        "stream_id": STREAM_ID,
        "cutoff_global_position": 10,
        "last_source_stream_sequence": 2,
        "effective_time_cutoff": NOW.isoformat(),
    }
    assert payload["outcome_state"] == {
        "snapshot_digest": _digest("outcome-snapshot"),
        "domain": "repository",
        "stream_id": STREAM_ID,
        "cutoff_global_position": 14,
        "last_source_stream_sequence": 4,
        "effective_time_cutoff": (NOW + timedelta(minutes=5)).isoformat(),
    }
    assert first.evaluation.evidence_binding_id == _digest("evidence-binding")
    assert first.evaluation.owner_observation_id == "outcome-observation:1"
    assert first.execution.idempotency_key == "idempotency:1"


def test_identity_changes_change_transition_id_and_bytes() -> None:
    command = _command()
    original = StateTransitionAcceptor().handle(command).transition
    assert original is not None
    outcome = command.outcome_state
    assert outcome is not None
    changed = replace(
        command,
        outcome_state=replace(
            outcome,
            reference=replace(
                outcome.reference,
                snapshot_digest=_digest("different-outcome-snapshot"),
            ),
        ),
    )
    revised = StateTransitionAcceptor().handle(changed).transition

    assert revised is not None
    assert revised.transition_id != original.transition_id
    assert encode_accepted_state_transition(revised) != encode_accepted_state_transition(original)


def test_domain_reordering_is_normalized_before_identity() -> None:
    command = _command(include_concurrent=True)
    outcome = command.outcome_state
    assert outcome is not None
    reordered = replace(
        command,
        outcome_state=TransitionStateView(outcome.reference, tuple(reversed(outcome.claims))),
        triggering_events=tuple(reversed(command.triggering_events)),
        evaluation=replace(
            command.evaluation,
            findings=tuple(reversed(command.evaluation.findings)),
        ),
    )

    original = StateTransitionAcceptor().handle(command).transition
    normalized = StateTransitionAcceptor().handle(reordered).transition

    assert original == normalized
    assert original is not None and normalized is not None
    assert encode_accepted_state_transition(original) == encode_accepted_state_transition(
        normalized
    )


def test_strict_codec_round_trips_exact_canonical_artifact() -> None:
    transition = StateTransitionAcceptor().handle(_command()).transition
    assert transition is not None

    encoded = encode_accepted_state_transition(transition)

    assert decode_accepted_state_transition(encoded) == transition


@pytest.mark.parametrize("mutation", ("extra", "missing", "forged-id", "bool-int"))
def test_strict_codec_rejects_field_identity_and_bool_integer_adversaries(
    mutation: str,
) -> None:
    transition = StateTransitionAcceptor().handle(_command()).transition
    assert transition is not None
    payload = json.loads(encode_accepted_state_transition(transition))
    if mutation == "extra":
        payload["extra"] = True
    elif mutation == "missing":
        del payload["run_id"]
    elif mutation == "forged-id":
        payload["transition_id"] = _digest("forged-transition")
    else:
        payload["initial_state"]["cutoff_global_position"] = True

    with pytest.raises(StateTransitionArtifactCodecError):
        decode_accepted_state_transition(canonical_json_bytes(payload))


def test_strict_codec_rejects_noncanonical_json_and_domain_ordering() -> None:
    transition = StateTransitionAcceptor().handle(_two_target_command()).transition
    assert transition is not None
    encoded = encode_accepted_state_transition(transition)
    with pytest.raises(StateTransitionArtifactCodecError, match="canonical JSON"):
        decode_accepted_state_transition(encoded + b"\n")

    payload = json.loads(encoded)
    payload["claim_deltas"].reverse()
    payload["accepted_claim_ids"].reverse()
    with pytest.raises(StateTransitionArtifactCodecError, match="canonical domain ordering"):
        decode_accepted_state_transition(canonical_json_bytes(payload))


def test_strict_codec_rejects_rehashed_cross_identity_forgery() -> None:
    transition = StateTransitionAcceptor().handle(_command()).transition
    assert transition is not None
    payload = json.loads(encode_accepted_state_transition(transition))
    payload["authorization"]["proposal_digest"] = _digest("forged-proposal")
    identity = {key: value for key, value in payload.items() if key != "transition_id"}
    payload["transition_id"] = json_digest(identity)

    with pytest.raises(StateTransitionArtifactCodecError, match="domain contract"):
        decode_accepted_state_transition(canonical_json_bytes(payload))


def test_accepted_artifact_itself_rejects_nondefinitive_or_unfenced_semantics() -> None:
    transition = StateTransitionAcceptor().handle(_command()).transition
    assert transition is not None
    unknown_execution = _execution(
        transition.proposal,
        transition.authorization,
        status=TransitionExecutionStatus.UNKNOWN,
    )
    with pytest.raises(StateTransitionIntegrityError, match="unknown execution"):
        replace(
            transition,
            execution=unknown_execution,
        )
    with pytest.raises(StateTransitionIntegrityError, match="allowed authorization"):
        replace(
            transition,
            authorization=replace(
                transition.authorization,
                outcome=TransitionAuthorizationOutcome.DENY,
            ),
        )
    with pytest.raises(StateTransitionIntegrityError, match="share one scope"):
        replace(
            transition,
            outcome_state=replace(transition.outcome_state, domain="other"),
        )
    with pytest.raises(StateTransitionIntegrityError, match="advance both state cutoffs"):
        replace(
            transition,
            outcome_state=replace(
                transition.outcome_state,
                cutoff_global_position=10,
                last_source_stream_sequence=2,
            ),
        )
    with pytest.raises(StateTransitionIntegrityError, match="invalid triggering event"):
        replace(
            transition,
            triggering_events=(replace(transition.triggering_events[0], event_type="other"),),
        )


def _command(
    *,
    verdict: TransitionEvaluationVerdict = TransitionEvaluationVerdict.PASS,
    accepted_value: bool = True,
    include_concurrent: bool = False,
) -> AcceptStateTransition:
    initial_claim = _claim(
        "claim:initial",
        "event:initial",
        value=False,
        position=10,
        sequence=2,
        effective_at=NOW,
    )
    outcome_claim = _claim(
        "claim:outcome",
        "event:outcome",
        value=accepted_value,
        position=13,
        sequence=3,
        effective_at=NOW + timedelta(minutes=4),
    )
    concurrent = _claim(
        "claim:concurrent",
        "event:concurrent",
        value="unrelated",
        position=14,
        sequence=4,
        effective_at=NOW + timedelta(minutes=5),
    )
    initial = TransitionStateView(
        _snapshot("initial-snapshot", 10, 2, NOW),
        (initial_claim,),
    )
    outcome_claims = (initial_claim, outcome_claim)
    if include_concurrent:
        outcome_claims = (*outcome_claims, concurrent)
    outcome = TransitionStateView(
        _snapshot("outcome-snapshot", 14, 4, NOW + timedelta(minutes=5)),
        outcome_claims,
    )
    proposal = _proposal()
    authorization = AuthorizationReference(
        decision_id=_digest("authorization-decision"),
        decision_artifact_digest=_digest("authorization-artifact"),
        proposal_id=proposal.proposal_id,
        proposal_digest=proposal.proposal_digest,
        constraint_evaluation_id=_digest("constraint-evaluation"),
        authorized_action_digest=proposal.action_digest,
        affordance_policy_digest=_digest("affordance-policy"),
        outcome=TransitionAuthorizationOutcome.ALLOW,
        approval_granted=True,
    )
    execution = _execution(proposal, authorization)
    evaluation = _evaluation(verdict, execution, actual_value=accepted_value)
    event = TransitionEventReference(
        event_id="event:outcome",
        global_position=13,
        stream_sequence=3,
        event_type="observation.recorded",
        stream_id=STREAM_ID,
        correlation_id=RUN_ID,
        causation_id=EXECUTION_EVENT_ID,
        payload_hash=_digest("outcome-event-payload"),
    )
    return AcceptStateTransition(
        run_id=RUN_ID,
        initial_state=initial,
        outcome_state=outcome,
        proposal=proposal,
        authorization=authorization,
        execution=execution,
        evaluation=evaluation,
        triggering_events=(event,),
    )


def _two_target_command() -> AcceptStateTransition:
    command = _command()
    outcome = command.outcome_state
    assert outcome is not None
    second_claim = _claim(
        "claim:tests",
        "event:tests",
        value=True,
        position=14,
        sequence=4,
        effective_at=NOW + timedelta(minutes=5),
        predicate="tests.pass",
    )
    outcome = replace(outcome, claims=(*outcome.claims, second_claim))
    second_finding = TransitionEvaluationFinding(
        criterion_id="tests",
        required=False,
        verdict=TransitionEvaluationVerdict.PASS,
        code="expected-value-observed",
        expected_value=True,
        actual_present=True,
        actual_value=True,
        actual_confidence=1.0,
        observed_claim_ids=("claim:tests",),
        source_event_ids=("event:tests",),
    )
    evaluation = _rebuild_evaluation(
        command.evaluation,
        findings=(*command.evaluation.findings, second_finding),
    )
    second_event = TransitionEventReference(
        "event:tests",
        14,
        4,
        "observation.recorded",
        STREAM_ID,
        RUN_ID,
        EXECUTION_EVENT_ID,
        _digest("tests-event-payload"),
    )
    return replace(
        command,
        outcome_state=outcome,
        evaluation=evaluation,
        triggering_events=(*command.triggering_events, second_event),
    )


def _fold_shaped_inconclusive_command() -> AcceptStateTransition:
    command = _command()
    initial_claims = command.initial_state.claims
    outcome = TransitionStateView(
        StateSnapshotReference(
            snapshot_digest=_digest("claim-free-outcome-snapshot"),
            domain="repository",
            stream_id=STREAM_ID,
            cutoff_global_position=13,
            last_source_stream_sequence=2,
            effective_time_cutoff=NOW + timedelta(minutes=4),
        ),
        initial_claims,
    )
    evaluation = _nondefinitive_evaluation(
        TransitionEvaluationVerdict.INCONCLUSIVE,
        command.evaluation,
    )
    event = replace(
        command.triggering_events[0],
        event_type="outcome.observation-inconclusive",
        global_position=13,
        stream_sequence=3,
    )
    return replace(
        command,
        outcome_state=outcome,
        evaluation=evaluation,
        triggering_events=(event,),
    )


def _proposal() -> ProposalReference:
    arguments = (TransitionActionArgument("path", "README.md"),)
    action_digest = json_digest(
        {
            "schema_version": "authorized-action/v1",
            "proposal_id": "proposal:1",
            "affordance": "repository.write",
            "arguments": [{"name": "path", "value": "README.md"}],
        }
    )
    return ProposalReference(
        proposal_id="proposal:1",
        proposal_digest=_digest("proposal"),
        proposal_artifact_digest=_digest("proposal-artifact"),
        context_frame_id=_digest("context-frame"),
        affordance="repository.write",
        arguments=arguments,
        action_digest=action_digest,
    )


def _execution(
    proposal: ProposalReference,
    authorization: AuthorizationReference,
    *,
    status: TransitionExecutionStatus = TransitionExecutionStatus.SUCCEEDED,
) -> ExecutionReference:
    result_id = _digest(f"execution-result-{status.value}")
    completed_at = NOW + timedelta(minutes=2)
    binding_id = json_digest(
        {
            "schema_version": "outcome-execution-binding/v1",
            "run_id": RUN_ID,
            "invocation_id": "invocation:1",
            "proposal_id": proposal.proposal_id,
            "proposal_digest": proposal.proposal_digest,
            "authorization_decision_id": authorization.decision_id,
            "authorized_action_digest": proposal.action_digest,
            "execution_result_id": result_id,
            "execution_identity_digest": _digest("execution-identity"),
            "execution_status": status.value,
            "affordance": proposal.affordance,
            "arguments": [{"name": item.name, "value": item.value} for item in proposal.arguments],
            "execution_adapter_id": "adapter:repository",
            "execution_adapter_contract_version": "repository-adapter/v1",
            "completed_at": completed_at.isoformat(),
        }
    )
    return ExecutionReference(
        run_id=RUN_ID,
        execution_event_id=EXECUTION_EVENT_ID,
        execution_result_id=result_id,
        execution_result_digest=result_id,
        invocation_id="invocation:1",
        proposal_id=proposal.proposal_id,
        proposal_digest=proposal.proposal_digest,
        authorization_decision_id=authorization.decision_id,
        execution_binding_id=binding_id,
        execution_identity_digest=_digest("execution-identity"),
        authorized_action_digest=proposal.action_digest,
        idempotency_key="idempotency:1",
        affordance=proposal.affordance,
        arguments=proposal.arguments,
        adapter_id="adapter:repository",
        adapter_contract_version="repository-adapter/v1",
        status=status,
        completed_at=completed_at,
    )


def _evaluation_id(
    *,
    run_id: str,
    evaluation_spec_id: str,
    authorization_outcome: TransitionAuthorizationOutcome,
    execution_status: TransitionExecutionStatus | None,
    execution_event_id: str | None,
    execution_binding_id: str | None,
    owner_observation_id: str | None,
    owner_observation_digest: str | None,
    evidence_binding_id: str | None,
    initial_state_position: int,
    verdict: TransitionEvaluationVerdict,
    findings: tuple[TransitionEvaluationFinding, ...],
    evaluated_at: datetime,
) -> str:
    return json_digest(
        {
            "schema_version": "outcome-evaluation/v1",
            "run_id": run_id,
            "evaluation_spec_id": evaluation_spec_id,
            "authorization_outcome": authorization_outcome.value,
            "execution_status": None if execution_status is None else execution_status.value,
            "execution_event_id": execution_event_id,
            "execution_binding_id": execution_binding_id,
            "outcome_observation_id": owner_observation_id,
            "outcome_observation_digest": owner_observation_digest,
            "outcome_evidence_binding_id": evidence_binding_id,
            "initial_state_position": initial_state_position,
            "verdict": verdict.value,
            "findings": [
                {
                    "criterion_id": item.criterion_id,
                    "required": item.required,
                    "verdict": item.verdict.value,
                    "code": item.code,
                    "expected_value": item.expected_value,
                    "actual_present": item.actual_present,
                    "actual_value": item.actual_value,
                    "actual_confidence": item.actual_confidence,
                    "observed_claim_ids": list(item.observed_claim_ids),
                    "source_event_ids": list(item.source_event_ids),
                }
                for item in sorted(findings, key=lambda item: item.criterion_id)
            ],
            "evaluated_at": evaluated_at.isoformat(),
        }
    )


def _rebuild_evaluation(
    base: EvaluationReference,
    *,
    findings: tuple[TransitionEvaluationFinding, ...] | None = None,
    run_id: str | None = None,
    evaluation_spec_id: str | None = None,
    execution_binding_id: str | None = None,
    initial_state_position: int | None = None,
    evaluated_at: datetime | None = None,
    owner_observation_id: str | None = None,
    owner_observation_artifact_digest: str | None = None,
    drop_owner_id: bool = False,
    drop_owner_digest: bool = False,
    drop_owner_artifact: bool = False,
    drop_evidence_binding: bool = False,
    evaluation_id_override: str | None = None,
) -> EvaluationReference:
    selected_findings = base.findings if findings is None else findings
    selected_run = base.run_id if run_id is None else run_id
    selected_spec = base.evaluation_spec_id if evaluation_spec_id is None else evaluation_spec_id
    selected_binding = (
        base.execution_binding_id if execution_binding_id is None else execution_binding_id
    )
    selected_position = (
        base.initial_state_position if initial_state_position is None else initial_state_position
    )
    selected_time = base.evaluated_at if evaluated_at is None else evaluated_at
    selected_owner_id = (
        None
        if drop_owner_id
        else (base.owner_observation_id if owner_observation_id is None else owner_observation_id)
    )
    selected_owner_digest = None if drop_owner_digest else base.owner_observation_digest
    selected_owner_artifact = (
        None
        if drop_owner_artifact
        else (
            base.owner_observation_artifact_digest
            if owner_observation_artifact_digest is None
            else owner_observation_artifact_digest
        )
    )
    selected_evidence = None if drop_evidence_binding else base.evidence_binding_id
    canonical_id = _evaluation_id(
        run_id=selected_run,
        evaluation_spec_id=selected_spec,
        authorization_outcome=base.authorization_outcome,
        execution_status=base.execution_status,
        execution_event_id=base.execution_event_id,
        execution_binding_id=selected_binding,
        owner_observation_id=selected_owner_id,
        owner_observation_digest=selected_owner_digest,
        evidence_binding_id=selected_evidence,
        initial_state_position=selected_position,
        verdict=base.verdict,
        findings=selected_findings,
        evaluated_at=selected_time,
    )
    return EvaluationReference(
        evaluation_id=(canonical_id if evaluation_id_override is None else evaluation_id_override),
        evaluation_artifact_digest=base.evaluation_artifact_digest,
        evaluation_spec_id=selected_spec,
        evaluation_spec_digest=base.evaluation_spec_digest,
        run_id=selected_run,
        authorization_outcome=base.authorization_outcome,
        execution_status=base.execution_status,
        verdict=base.verdict,
        execution_event_id=base.execution_event_id,
        execution_binding_id=selected_binding,
        evidence_binding_id=selected_evidence,
        owner_observation_id=selected_owner_id,
        owner_observation_digest=selected_owner_digest,
        owner_observation_artifact_digest=selected_owner_artifact,
        initial_state_position=selected_position,
        findings=selected_findings,
        evaluated_at=selected_time,
    )


def _evaluation(
    verdict: TransitionEvaluationVerdict,
    execution: ExecutionReference,
    *,
    actual_value: bool = True,
) -> EvaluationReference:
    finding = TransitionEvaluationFinding(
        criterion_id="ready",
        required=True,
        verdict=verdict,
        code=(
            "expected-value-observed"
            if verdict is TransitionEvaluationVerdict.PASS
            else "unexpected-value-observed"
        ),
        expected_value=True,
        actual_present=True,
        actual_value=actual_value,
        actual_confidence=1.0,
        observed_claim_ids=("claim:outcome",),
        source_event_ids=("event:outcome",),
    )
    spec_id = _digest("evaluation-spec")
    evaluated_at = NOW + timedelta(minutes=6)
    evaluation_id = _evaluation_id(
        run_id=RUN_ID,
        evaluation_spec_id=spec_id,
        authorization_outcome=TransitionAuthorizationOutcome.ALLOW,
        execution_status=execution.status,
        execution_event_id=execution.execution_event_id,
        execution_binding_id=execution.execution_binding_id,
        owner_observation_id="outcome-observation:1",
        owner_observation_digest=_digest("owner-observation"),
        evidence_binding_id=_digest("evidence-binding"),
        initial_state_position=10,
        verdict=verdict,
        findings=(finding,),
        evaluated_at=evaluated_at,
    )
    return EvaluationReference(
        evaluation_id=evaluation_id,
        evaluation_artifact_digest=_digest(f"evaluation-artifact-{verdict.value}"),
        evaluation_spec_id=spec_id,
        evaluation_spec_digest=_digest("evaluation-spec-artifact"),
        run_id=RUN_ID,
        authorization_outcome=TransitionAuthorizationOutcome.ALLOW,
        execution_status=execution.status,
        verdict=verdict,
        execution_event_id=execution.execution_event_id,
        execution_binding_id=execution.execution_binding_id,
        evidence_binding_id=_digest("evidence-binding"),
        owner_observation_id="outcome-observation:1",
        owner_observation_digest=_digest("owner-observation"),
        owner_observation_artifact_digest=_digest("owner-observation-artifact"),
        initial_state_position=10,
        findings=(finding,),
        evaluated_at=evaluated_at,
    )


def _nondefinitive_evaluation(
    verdict: TransitionEvaluationVerdict,
    base: EvaluationReference,
) -> EvaluationReference:
    blocked = verdict is TransitionEvaluationVerdict.NOT_EVALUATED
    finding = TransitionEvaluationFinding(
        criterion_id="ready",
        required=True,
        verdict=verdict,
        code="authorization-denied" if blocked else "outcome-observation-inconclusive",
        expected_value=True,
        actual_present=False,
        actual_value=None,
        source_event_ids=() if blocked else ("event:outcome",),
    )
    authorization_outcome = (
        TransitionAuthorizationOutcome.DENY if blocked else TransitionAuthorizationOutcome.ALLOW
    )
    execution_status = None if blocked else base.execution_status
    execution_event_id = None if blocked else base.execution_event_id
    execution_binding_id = None if blocked else base.execution_binding_id
    evidence_binding_id = None if blocked else base.evidence_binding_id
    owner_id = None if blocked else base.owner_observation_id
    owner_digest = None if blocked else base.owner_observation_digest
    evaluated_at = base.evaluated_at
    evaluation_id = _evaluation_id(
        run_id=base.run_id,
        evaluation_spec_id=base.evaluation_spec_id,
        authorization_outcome=authorization_outcome,
        execution_status=execution_status,
        execution_event_id=execution_event_id,
        execution_binding_id=execution_binding_id,
        owner_observation_id=owner_id,
        owner_observation_digest=owner_digest,
        evidence_binding_id=evidence_binding_id,
        initial_state_position=base.initial_state_position,
        verdict=verdict,
        findings=(finding,),
        evaluated_at=evaluated_at,
    )
    return EvaluationReference(
        evaluation_id=evaluation_id,
        evaluation_artifact_digest=_digest(f"evaluation-artifact-{verdict.value}"),
        evaluation_spec_id=base.evaluation_spec_id,
        evaluation_spec_digest=base.evaluation_spec_digest,
        run_id=base.run_id,
        authorization_outcome=authorization_outcome,
        execution_status=execution_status,
        verdict=verdict,
        execution_event_id=execution_event_id,
        execution_binding_id=execution_binding_id,
        evidence_binding_id=evidence_binding_id,
        owner_observation_id=owner_id,
        owner_observation_digest=owner_digest,
        owner_observation_artifact_digest=(
            None if blocked else base.owner_observation_artifact_digest
        ),
        initial_state_position=base.initial_state_position,
        findings=(finding,),
        evaluated_at=evaluated_at,
    )


def _unknown_evaluation(
    base: EvaluationReference,
    execution: ExecutionReference,
) -> EvaluationReference:
    finding = TransitionEvaluationFinding(
        criterion_id="ready",
        required=True,
        verdict=TransitionEvaluationVerdict.INCONCLUSIVE,
        code="execution-unknown",
        expected_value=True,
        actual_present=False,
        actual_value=None,
    )
    evaluation_id = _evaluation_id(
        run_id=base.run_id,
        evaluation_spec_id=base.evaluation_spec_id,
        authorization_outcome=TransitionAuthorizationOutcome.ALLOW,
        execution_status=TransitionExecutionStatus.UNKNOWN,
        execution_event_id=execution.execution_event_id,
        execution_binding_id=execution.execution_binding_id,
        owner_observation_id=None,
        owner_observation_digest=None,
        evidence_binding_id=None,
        initial_state_position=base.initial_state_position,
        verdict=TransitionEvaluationVerdict.INCONCLUSIVE,
        findings=(finding,),
        evaluated_at=base.evaluated_at,
    )
    return EvaluationReference(
        evaluation_id=evaluation_id,
        evaluation_artifact_digest=_digest("evaluation-artifact-unknown"),
        evaluation_spec_id=base.evaluation_spec_id,
        evaluation_spec_digest=base.evaluation_spec_digest,
        run_id=base.run_id,
        authorization_outcome=TransitionAuthorizationOutcome.ALLOW,
        execution_status=TransitionExecutionStatus.UNKNOWN,
        verdict=TransitionEvaluationVerdict.INCONCLUSIVE,
        execution_event_id=execution.execution_event_id,
        execution_binding_id=execution.execution_binding_id,
        evidence_binding_id=None,
        owner_observation_id=None,
        owner_observation_digest=None,
        owner_observation_artifact_digest=None,
        initial_state_position=base.initial_state_position,
        findings=(finding,),
        evaluated_at=base.evaluated_at,
    )


def _snapshot(
    name: str,
    position: int,
    sequence: int,
    effective_at: datetime,
) -> StateSnapshotReference:
    return StateSnapshotReference(
        snapshot_digest=_digest(name),
        domain="repository",
        stream_id=STREAM_ID,
        cutoff_global_position=position,
        last_source_stream_sequence=sequence,
        effective_time_cutoff=effective_at,
    )


def _claim(
    claim_id: str,
    event_id: str,
    *,
    value: JsonScalar,
    position: int,
    sequence: int,
    effective_at: datetime,
    predicate: str = "ready",
) -> TransitionClaim:
    return TransitionClaim(
        claim_id=claim_id,
        subject="repository",
        predicate=predicate,
        value=value,
        confidence=1.0,
        effective_at=effective_at,
        recorded_at=effective_at,
        source_event_id=event_id,
        source="outcome-observer" if event_id != "event:initial" else "initial-observer",
        actor="blackcell",
        correlation_id=RUN_ID,
        domain="repository",
        stream_id=STREAM_ID,
        stream_sequence=sequence,
        global_position=position,
    )


def _digest(label: str) -> str:
    return json_digest({"label": label})
