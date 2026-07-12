from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from blackcell.adapters.persistence.sqlite import (
    SQLiteDecisionAttemptJournal,
    SQLiteExecutionJournal,
)
from blackcell.adapters.persistence.sqlite.run_records_v2 import (
    KernelFeedbackRunRecorder,
)
from blackcell.features.accept_state_transition import TransitionAcceptanceStatus
from blackcell.features.authorize_action import (
    AuthorizationDecision,
    AuthorizationOutcome,
)
from blackcell.features.evaluate_outcome import (
    EvaluateOutcome,
    EvaluationAuthorizationOutcome,
    EvaluationExecutionStatus,
    EvaluationObservation,
    EvaluationVerdict,
    OutcomeEvaluation,
    OutcomeEvaluator,
)
from blackcell.features.execute_affordance import (
    ExecutionClaim,
    ExecutionResult,
    ExecutionStatus,
    SideEffectClass,
)
from blackcell.features.ingest_observation import IngestObservation
from blackcell.features.ingest_observation.events import observation_events
from blackcell.features.observe_outcome import (
    OutcomeArgument,
    OutcomeClaim,
    OutcomeEvidencePointer,
    OutcomeExecutionBinding,
    OutcomeObservation,
    OutcomeObservationStatus,
)
from blackcell.features.request_decision import (
    DecisionFailure,
    DecisionFailureKind,
    DecisionPreparation,
)
from blackcell.kernel import ArtifactStore, EventEnvelope, EventStore
from blackcell.workflows.outcome_evidence import (
    bind_evaluation_observation,
    inconclusive_outcome_event,
    outcome_observation_input,
)
from blackcell.workflows.run_grammar import validate_run_grammar
from blackcell.workflows.run_protocol import (
    AUTHORIZATION_DECIDED,
    CONSTRAINTS_EVALUATED,
    CONTEXT_RECORDED,
    EVALUATION_RECORDED,
    EVALUATION_SPECIFIED,
    EXECUTION_RECORDED,
    INITIAL_STATE_RECORDED,
    MODEL_ATTEMPT_RECORDED,
    MODEL_FAILED,
    MODEL_REQUESTED,
    MODEL_RESPONDED,
    OUTCOME_OBSERVED,
    OUTCOME_STATE_RECORDED,
    PROPOSAL_RECORDED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_STARTED,
    STATE_TRANSITION_RECORDED,
    TRACE_RECORDED,
    RunAlreadyExists,
    RunOutcome,
    run_stream_id,
)
from blackcell.workflows.state_transition import bind_and_accept_state_transition
from tests.unit import test_run_records_v2 as base

CONTROL_PREFIX = (
    RUN_STARTED,
    EVALUATION_SPECIFIED,
    INITIAL_STATE_RECORDED,
    CONTEXT_RECORDED,
    MODEL_REQUESTED,
    MODEL_ATTEMPT_RECORDED,
    MODEL_RESPONDED,
    PROPOSAL_RECORDED,
    CONSTRAINTS_EVALUATED,
    AUTHORIZATION_DECIDED,
)


@pytest.mark.parametrize(
    ("branch", "expected_authorization", "expected_outcome", "finding_code"),
    (
        (
            "deny",
            AuthorizationOutcome.DENY,
            RunOutcome.DENIED,
            "authorization-denied",
        ),
        (
            "require-approval",
            AuthorizationOutcome.REQUIRE_APPROVAL,
            RunOutcome.APPROVAL_REQUIRED,
            "authorization-requires-approval",
        ),
    ),
)
def test_blocked_authorization_branches_complete_without_execution(
    tmp_path: Path,
    branch: str,
    expected_authorization: AuthorizationOutcome,
    expected_outcome: RunOutcome,
    finding_code: str,
) -> None:
    scenario = base.RunFixture.create(tmp_path)
    if branch == "deny":
        definition = scenario.request.constraints.constraints[0]
        scenario.request = replace(
            scenario.request,
            constraints=replace(
                scenario.request.constraints,
                constraints=(replace(definition, expected_values=("blocked",)),),
            ),
        )
    else:
        scenario.request = replace(
            scenario.request,
            authorization_affordance=replace(
                scenario.request.authorization_affordance,
                read_only=False,
                mutates_state=True,
            ),
            execution_affordance=replace(
                scenario.request.execution_affordance,
                side_effect_class=SideEffectClass.REVERSIBLE,
            ),
        )

    scenario.through_context()
    scenario.through_decision()
    authorization = _authorization(scenario)
    assert authorization.outcome is expected_authorization
    evaluation = _record_evaluation(scenario)
    assert evaluation.verdict is EvaluationVerdict.NOT_EVALUATED
    assert {item.code for item in evaluation.findings} == {finding_code}
    acceptance = bind_and_accept_state_transition(
        base.RUN_ID,
        scenario.events,
        scenario.artifacts,
    )
    assert acceptance.status is TransitionAcceptanceStatus.NOT_ACCEPTED
    assert acceptance.code == "evaluation-not-evaluated"
    assert acceptance.transition is None

    scenario.recorder.complete(base.RUN_ID, expected_outcome)
    expected_events = (*CONTROL_PREFIX, EVALUATION_RECORDED, TRACE_RECORDED, RUN_COMPLETED)
    _assert_completed_restart(
        scenario,
        expected_events,
        expected_outcome,
        TransitionAcceptanceStatus.NOT_ACCEPTED,
        "evaluation-not-evaluated",
    )


def test_allowed_unknown_execution_requires_reconciliation_without_transition(
    tmp_path: Path,
) -> None:
    scenario = base.RunFixture.create(tmp_path)
    scenario.through_context()
    scenario.through_decision()
    assert _authorization(scenario).outcome is AuthorizationOutcome.ALLOW
    result, execution_event = _record_execution(scenario, ExecutionStatus.UNKNOWN)
    binding = _outcome_binding(scenario, result)
    evaluation = _record_evaluation(
        scenario,
        execution_event=execution_event,
        binding=binding,
    )
    assert evaluation.verdict is EvaluationVerdict.INCONCLUSIVE
    assert {item.code for item in evaluation.findings} == {"execution-unknown"}
    acceptance = bind_and_accept_state_transition(
        base.RUN_ID,
        scenario.events,
        scenario.artifacts,
    )
    assert acceptance.status is TransitionAcceptanceStatus.NOT_ACCEPTED
    assert acceptance.code == "execution-unknown"
    assert acceptance.transition is None

    scenario.recorder.complete(base.RUN_ID, RunOutcome.REQUIRES_RECONCILIATION)
    expected_events = (
        *CONTROL_PREFIX,
        EXECUTION_RECORDED,
        EVALUATION_RECORDED,
        TRACE_RECORDED,
        RUN_COMPLETED,
    )
    _assert_completed_restart(
        scenario,
        expected_events,
        RunOutcome.REQUIRES_RECONCILIATION,
        TransitionAcceptanceStatus.NOT_ACCEPTED,
        "execution-unknown",
    )


def test_allowed_failed_execution_records_definitive_failure_transition(
    tmp_path: Path,
) -> None:
    scenario = base.RunFixture.create(tmp_path)
    scenario.through_context()
    scenario.through_decision()
    assert _authorization(scenario).outcome is AuthorizationOutcome.ALLOW
    _record_execution(scenario, ExecutionStatus.FAILED)
    evaluation = _record_outcome_evaluation(
        scenario,
        status=OutcomeObservationStatus.OBSERVED,
        claim_value=False,
    )
    assert evaluation.verdict is EvaluationVerdict.FAIL
    acceptance = bind_and_accept_state_transition(
        base.RUN_ID,
        scenario.events,
        scenario.artifacts,
    )
    assert acceptance.status is TransitionAcceptanceStatus.ACCEPTED
    assert acceptance.transition is not None
    scenario.recorder.record_accepted_transition(base.RUN_ID, acceptance.transition)

    scenario.recorder.complete(base.RUN_ID, RunOutcome.EXECUTION_FAILED)
    expected_events = (
        *CONTROL_PREFIX,
        EXECUTION_RECORDED,
        OUTCOME_OBSERVED,
        OUTCOME_STATE_RECORDED,
        EVALUATION_RECORDED,
        STATE_TRANSITION_RECORDED,
        TRACE_RECORDED,
        RUN_COMPLETED,
    )
    _assert_completed_restart(
        scenario,
        expected_events,
        RunOutcome.EXECUTION_FAILED,
        TransitionAcceptanceStatus.ACCEPTED,
        "definitive-outcome-evidence-accepted",
    )


def test_inconclusive_observer_completes_without_state_transition(tmp_path: Path) -> None:
    scenario = base.RunFixture.create(tmp_path)
    scenario.through_context()
    scenario.through_decision()
    assert _authorization(scenario).outcome is AuthorizationOutcome.ALLOW
    _record_execution(scenario, ExecutionStatus.SUCCEEDED)
    evaluation = _record_outcome_evaluation(
        scenario,
        status=OutcomeObservationStatus.INCONCLUSIVE,
    )
    assert evaluation.verdict is EvaluationVerdict.INCONCLUSIVE
    assert {item.code for item in evaluation.findings} == {"outcome-observation-inconclusive"}
    acceptance = bind_and_accept_state_transition(
        base.RUN_ID,
        scenario.events,
        scenario.artifacts,
    )
    assert acceptance.status is TransitionAcceptanceStatus.NOT_ACCEPTED
    assert acceptance.code == "evaluation-inconclusive"
    assert acceptance.transition is None

    scenario.recorder.complete(base.RUN_ID, RunOutcome.EXECUTED)
    expected_events = (
        *CONTROL_PREFIX,
        EXECUTION_RECORDED,
        OUTCOME_OBSERVED,
        OUTCOME_STATE_RECORDED,
        EVALUATION_RECORDED,
        TRACE_RECORDED,
        RUN_COMPLETED,
    )
    _assert_completed_restart(
        scenario,
        expected_events,
        RunOutcome.EXECUTED,
        TransitionAcceptanceStatus.NOT_ACCEPTED,
        "evaluation-inconclusive",
    )


def test_routed_model_failure_without_attempt_rebinds_after_restart(tmp_path: Path) -> None:
    scenario = base.RunFixture.create(tmp_path)
    scenario.through_context()
    assert scenario.frame is not None
    assert scenario.context_event is not None
    request = base._decision_request(
        scenario.request,
        scenario.frame,
        scenario.context_event,
    )
    registered = scenario.decision_journal.register(request, registered_at=base.MODEL_AT)
    preparation = scenario.decision_journal.record_route(
        registered,
        base._route(),
        recorded_at=base.MODEL_AT,
    )
    assert isinstance(preparation, DecisionPreparation)
    failure = DecisionFailure(
        request.request_id,
        request.request_digest,
        DecisionFailureKind.ADMISSION,
        "route_rejected",
        False,
        base.MODEL_AT,
        preparation.route.route_id,
    )
    terminal = scenario.decision_journal.fail(
        registered,
        failure,
        preparation=preparation,
        claim=None,
        usage=None,
        recorded_at=base.MODEL_AT,
    )
    scenario.recorder.record_model_request(base.RUN_ID, registered)
    failure_event = scenario.recorder.record_model_terminal(base.RUN_ID, terminal)
    assert failure_event.payload["route_artifact"] is not None
    assert failure_event.payload["attempt_id"] is None
    scenario.recorder.fail(
        base.RUN_ID,
        phase="decision",
        error_type="DecisionFailure",
    )

    expected_events = (
        RUN_STARTED,
        EVALUATION_SPECIFIED,
        INITIAL_STATE_RECORDED,
        CONTEXT_RECORDED,
        MODEL_REQUESTED,
        MODEL_FAILED,
        TRACE_RECORDED,
        RUN_FAILED,
    )
    stored = scenario.events.read_stream(run_stream_id(base.RUN_ID))
    assert tuple(item.event_type for item in stored) == expected_events
    grammar = validate_run_grammar(stored, run_id=base.RUN_ID)
    assert grammar.terminal and grammar.failed

    fresh = _fresh_recorder(scenario)
    with pytest.raises(RunAlreadyExists):
        fresh.open(scenario.request)
    assert fresh.record_model_terminal(base.RUN_ID, terminal) == failure_event
    reopened = SQLiteDecisionAttemptJournal(
        scenario.root,
        database_path=scenario.database,
    )
    assert reopened.get_preparation(request.request_id) == preparation
    assert reopened.get_attempt(request.request_id) is None
    assert reopened.get_terminal(request.request_id) == terminal


def _record_execution(
    scenario: base.RunFixture,
    status: ExecutionStatus,
) -> tuple[ExecutionResult, EventEnvelope]:
    preparation, succeeded = base._execution_evidence(scenario)
    result = replace(
        succeeded,
        status=status,
        output_digest=None if status is ExecutionStatus.UNKNOWN else base.DIGEST,
        error_code=(
            "outcome_unknown"
            if status is ExecutionStatus.UNKNOWN
            else "fixture_failure"
            if status is ExecutionStatus.FAILED
            else None
        ),
    )
    claim = scenario.execution_journal.acquire(
        preparation,
        acquired_at=base.EXECUTED_AT,
    )
    assert isinstance(claim, ExecutionClaim)
    scenario.execution_journal.complete(
        claim,
        result,
        recorded_at=base.EXECUTED_AT + timedelta(seconds=1),
    )
    entry = scenario.execution_journal.list_entries()[0]
    event = scenario.recorder.record_execution(base.RUN_ID, entry)
    scenario.execution_entry = entry
    scenario.execution_event = event
    return result, event


def _record_outcome_evaluation(
    scenario: base.RunFixture,
    *,
    status: OutcomeObservationStatus,
    claim_value: bool = True,
) -> OutcomeEvaluation:
    assert scenario.execution_entry is not None
    assert scenario.execution_entry.current_result is not None
    assert scenario.execution_event is not None
    result = scenario.execution_entry.current_result
    binding = _outcome_binding(scenario, result)
    claims = (
        ()
        if status is OutcomeObservationStatus.INCONCLUSIVE
        else (
            OutcomeClaim(
                "claim:completed:branch",
                "project:blackcell",
                "completed",
                claim_value,
                1.0,
            ),
        )
    )
    observation = OutcomeObservation(
        "observation:outcome:branch",
        binding,
        scenario.request.evaluation_spec.spec_id,
        base.DOMAIN,
        base.OBSERVATION_STREAM,
        scenario.request.expected_observer_id,
        scenario.request.expected_observer_contract_version,
        status,
        base.OBSERVED_AT,
        claims,
        (OutcomeEvidencePointer(locator="fixture://branch-outcome"),),
    )
    if status is OutcomeObservationStatus.OBSERVED:
        command = IngestObservation(
            base.OBSERVATION_STREAM,
            1,
            base.ACTOR,
            observation.observer_id,
            base.RUN_ID,
            (outcome_observation_input(observation),),
            scenario.execution_event.event_id,
            base.DOMAIN,
        )
        candidate = observation_events(command, recorded_at=base.OBSERVED_AT)[0]
    else:
        candidate = inconclusive_outcome_event(
            observation,
            stream_sequence=2,
            actor=base.ACTOR,
            recorded_at=base.OBSERVED_AT,
            execution_event_id=scenario.execution_event.event_id,
        )
    domain_event = scenario.events.append(candidate, expected_sequence=1)
    outcome_event = scenario.recorder.record_outcome(
        base.RUN_ID,
        observation,
        outcome_event_ids=(domain_event.event_id,),
    )
    outcome_state = base._project(
        scenario.events,
        domain_event.global_position,
        base.OBSERVED_AT,
    )
    scenario.recorder.record_outcome_state(base.RUN_ID, outcome_state)
    bound = bind_evaluation_observation(
        observation,
        scenario.events,
        scenario.artifacts,
        execution_event_id=scenario.execution_event.event_id,
        outcome_event_ids=(domain_event.event_id,),
    )
    evaluation = _record_evaluation(
        scenario,
        execution_event=scenario.execution_event,
        binding=binding,
        observation=bound,
    )
    scenario.observation = observation
    scenario.outcome_event = outcome_event
    scenario.outcome_state = outcome_state
    return evaluation


def _record_evaluation(
    scenario: base.RunFixture,
    *,
    execution_event: EventEnvelope | None = None,
    binding: OutcomeExecutionBinding | None = None,
    observation: EvaluationObservation | None = None,
) -> OutcomeEvaluation:
    authorization = _authorization(scenario)
    assert scenario.initial_state is not None
    result = None if scenario.execution_entry is None else scenario.execution_entry.current_result
    evaluation = OutcomeEvaluator(clock=lambda: base.EVALUATED_AT).handle(
        EvaluateOutcome(
            base.RUN_ID,
            scenario.request.evaluation_spec,
            EvaluationAuthorizationOutcome(authorization.outcome.value),
            (None if result is None else EvaluationExecutionStatus(result.status.value)),
            None if execution_event is None else execution_event.event_id,
            None if binding is None else binding.binding_id,
            observation,
            scenario.initial_state.cutoff_global_position,
        )
    )
    scenario.recorder.record_evaluation(base.RUN_ID, evaluation)
    scenario.evaluation = evaluation
    return evaluation


def _outcome_binding(
    scenario: base.RunFixture,
    result: ExecutionResult,
) -> OutcomeExecutionBinding:
    assert scenario.proposal is not None
    authorization = _authorization(scenario)
    return OutcomeExecutionBinding(
        run_id=base.RUN_ID,
        invocation_id=result.invocation_id,
        proposal_id=scenario.proposal.proposal_id,
        proposal_digest=scenario.proposal.proposal_digest,
        authorization_decision_id=authorization.decision_id,
        authorized_action_digest=authorization.authorized_action_digest,
        execution_result_id=result.result_id,
        execution_identity_digest=result.execution_identity_digest,
        execution_status=result.status.value,
        affordance=result.affordance,
        arguments=tuple(
            OutcomeArgument(item.name, item.value) for item in scenario.proposal.arguments
        ),
        execution_adapter_id=result.adapter_id,
        execution_adapter_contract_version="fixture-executor/v1",
        completed_at=result.completed_at,
    )


def _authorization(scenario: base.RunFixture) -> AuthorizationDecision:
    assert isinstance(scenario.authorization, AuthorizationDecision)
    return scenario.authorization


def _fresh_recorder(scenario: base.RunFixture) -> KernelFeedbackRunRecorder:
    return KernelFeedbackRunRecorder(
        EventStore(scenario.database),
        ArtifactStore(scenario.root, database_path=scenario.database),
        SQLiteDecisionAttemptJournal(
            scenario.root,
            database_path=scenario.database,
        ),
        SQLiteExecutionJournal(
            scenario.root,
            database_path=scenario.database,
        ),
        clock=lambda: base.RECORDED_AT,
    )


def _assert_completed_restart(
    scenario: base.RunFixture,
    expected_events: tuple[str, ...],
    outcome: RunOutcome,
    acceptance_status: TransitionAcceptanceStatus,
    acceptance_code: str,
) -> None:
    stored = scenario.events.read_stream(run_stream_id(base.RUN_ID))
    assert tuple(item.event_type for item in stored) == expected_events
    grammar = validate_run_grammar(stored, run_id=base.RUN_ID)
    assert grammar.terminal and not grammar.failed
    assert stored[-1].payload["outcome"] == outcome.value

    fresh = _fresh_recorder(scenario)
    with pytest.raises(RunAlreadyExists):
        fresh.open(scenario.request)
    assert scenario.evaluation is not None
    evaluation_event = next(item for item in stored if item.event_type == EVALUATION_RECORDED)
    assert fresh.record_evaluation(base.RUN_ID, scenario.evaluation) == evaluation_event
    rebound = bind_and_accept_state_transition(
        base.RUN_ID,
        EventStore(scenario.database),
        ArtifactStore(scenario.root, database_path=scenario.database),
    )
    assert rebound.status is acceptance_status
    assert rebound.code == acceptance_code
    if acceptance_status is TransitionAcceptanceStatus.ACCEPTED:
        assert rebound.transition is not None
        transition_event = next(
            item for item in stored if item.event_type == STATE_TRANSITION_RECORDED
        )
        assert fresh.record_accepted_transition(base.RUN_ID, rebound.transition) == transition_event
    else:
        assert rebound.transition is None
