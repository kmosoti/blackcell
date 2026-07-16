from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

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
    ActionArgument,
    ActionProposal,
    AffordancePolicy,
    AuthorizeAction,
    authorize_action,
)
from blackcell.features.build_context import BuildContext, ContextFrame, build_context_frame
from blackcell.features.derive_signal_packet import DeriveSignalPacket, project_signal_packet
from blackcell.features.evaluate_outcome import (
    EvaluateOutcome,
    EvaluationAuthorizationOutcome,
    EvaluationCriterion,
    EvaluationExecutionStatus,
    EvaluationSpec,
    OutcomeEvaluation,
    OutcomeEvaluator,
)
from blackcell.features.execute_affordance import (
    EXECUTION_PREPARATION_MEDIA_TYPE,
    EXECUTION_RESULT_MEDIA_TYPE,
    AffordanceDefinition,
    AffordanceInvocation,
    ExecutionClaim,
    ExecutionJournalEntry,
    ExecutionJournalStatus,
    ExecutionPreparation,
    ExecutionResult,
    ExecutionStatus,
    SideEffectClass,
    serialize_execution_preparation,
    serialize_execution_result,
)
from blackcell.features.ingest_observation import (
    EvidencePointer,
    IngestObservation,
    ObservationInput,
    ObservedClaim,
)
from blackcell.features.ingest_observation.events import observation_events
from blackcell.features.observe_outcome import (
    OutcomeClaim,
    OutcomeEvidencePointer,
    OutcomeExecutionBinding,
    OutcomeObservation,
    OutcomeObservationStatus,
)
from blackcell.features.project_operational_state import (
    OperationalBeliefState,
    OperationalStateScope,
    ProjectOperationalState,
    ProjectOperationalStateHandler,
)
from blackcell.features.request_decision import (
    DECISION_ATTEMPT_MEDIA_TYPE,
    DECISION_REQUEST_MEDIA_TYPE,
    DECISION_RESPONSE_MEDIA_TYPE,
    DECISION_USAGE_MEDIA_TYPE,
    DecisionAffordance,
    DecisionAttemptClaim,
    DecisionAttemptRecord,
    DecisionBudget,
    DecisionCapability,
    DecisionClassification,
    DecisionFailure,
    DecisionFailureKind,
    DecisionLocality,
    DecisionPreparation,
    DecisionProposal,
    DecisionRequestRecord,
    DecisionRequirements,
    DecisionResponse,
    DecisionRoute,
    DecisionSuccessRecord,
    DecisionUsage,
    RequestDecision,
    decode_decision_failure,
    decode_decision_usage,
    encode_decision_attempt,
    encode_decision_request,
    encode_decision_response,
    encode_decision_usage,
)
from blackcell.features.retrieve_evidence import (
    DeterministicEvidenceRetriever,
    EvidenceKey,
    RetrieveEvidence,
)
from blackcell.features.solve_constraints import (
    ConstraintDefinition,
    ConstraintOperator,
    DeterministicConstraintSolver,
    SolveConstraints,
)
from blackcell.kernel import (
    ArtifactStore,
    EventEnvelope,
    EventStore,
    JsonInput,
    ProjectionCheckpoint,
)
from blackcell.workflows.daily_operator_v2_request import DailyOperatorV2Request
from blackcell.workflows.outcome_evidence import (
    OutcomeEvidenceBindingError,
    bind_evaluation_observation,
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
    RunIdentityConflict,
    RunInterrupted,
    RunOutcome,
    RunProtocolIntegrityError,
    run_stream_id,
)
from blackcell.workflows.state_transition import bind_and_accept_state_transition

NOW = datetime(2026, 7, 12, 18, tzinfo=UTC)
MODEL_AT = NOW + timedelta(minutes=1)
EXECUTED_AT = NOW + timedelta(minutes=2)
OBSERVED_AT = NOW + timedelta(minutes=3)
EVALUATED_AT = NOW + timedelta(minutes=4)
RECORDED_AT = NOW + timedelta(minutes=5)
RUN_ID = "run:feedback:1"
DOMAIN = "repository"
OBSERVATION_STREAM = "observations:feedback"
ACTOR = "operator"
DIGEST = f"sha256:{'7' * 64}"

SUCCESS_EVENTS = (
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
    EXECUTION_RECORDED,
    OUTCOME_OBSERVED,
    OUTCOME_STATE_RECORDED,
    EVALUATION_RECORDED,
    STATE_TRANSITION_RECORDED,
    TRACE_RECORDED,
    RUN_COMPLETED,
)


class NoCheckpoints:
    def load(
        self,
        projection_name: str,
        projection_version: int,
        *,
        stream_id: str | None = None,
    ) -> ProjectionCheckpoint | None:
        del projection_name, projection_version, stream_id
        return None

    def save(
        self,
        checkpoint: ProjectionCheckpoint,
        *,
        expected_position: int | None = None,
    ) -> ProjectionCheckpoint:
        del checkpoint, expected_position
        raise AssertionError("historical fixture projection must not write checkpoints")


EventMutation = Callable[[tuple[EventEnvelope, ...]], tuple[EventEnvelope, ...]]


class TamperingStore(EventStore):
    mutation: EventMutation | None = None

    def read_stream(
        self,
        stream_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> tuple[EventEnvelope, ...]:
        events = super().read_stream(
            stream_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        if self.mutation is None or not events:
            return events
        return self.mutation(events)


class FailOnceCompletedStore(EventStore):
    failed = False

    def append(self, event: EventEnvelope, *, expected_sequence: int) -> EventEnvelope:
        if event.event_type == RUN_COMPLETED and not self.failed:
            self.failed = True
            raise RuntimeError("simulated terminal commit failure")
        return super().append(event, expected_sequence=expected_sequence)


@dataclass(slots=True)
class DecisionStage:
    request_record: DecisionRequestRecord
    preparation: DecisionPreparation
    attempt_record: DecisionAttemptRecord
    terminal: DecisionSuccessRecord


@dataclass(slots=True)
class RunFixture:
    database: Path
    root: Path
    events: EventStore
    artifacts: ArtifactStore
    decision_journal: SQLiteDecisionAttemptJournal
    execution_journal: SQLiteExecutionJournal
    recorder: KernelFeedbackRunRecorder
    request: DailyOperatorV2Request
    initial_state: OperationalBeliefState | None = None
    frame: ContextFrame | None = None
    context_event: EventEnvelope | None = None
    decision: DecisionStage | None = None
    proposal: ActionProposal | None = None
    authorization: object | None = None
    execution_entry: ExecutionJournalEntry | None = None
    execution_event: EventEnvelope | None = None
    observation: OutcomeObservation | None = None
    outcome_event: EventEnvelope | None = None
    outcome_state: OperationalBeliefState | None = None
    evaluation: OutcomeEvaluation | None = None

    @classmethod
    def create(
        cls,
        tmp_path: Path,
        event_store_type: type[EventStore] = EventStore,
    ) -> RunFixture:
        database = tmp_path / "kernel.sqlite3"
        root = tmp_path / "artifacts"
        events = event_store_type(database)
        artifacts = ArtifactStore(root, database_path=database)
        decision_journal = SQLiteDecisionAttemptJournal(root, database_path=database)
        execution_journal = SQLiteExecutionJournal(root, database_path=database)
        recorder = KernelFeedbackRunRecorder(
            events,
            artifacts,
            decision_journal,
            execution_journal,
            clock=lambda: RECORDED_AT,
        )
        return cls(
            database,
            root,
            events,
            artifacts,
            decision_journal,
            execution_journal,
            recorder,
            _workflow_request(),
        )

    def through_context(self) -> None:
        opening = self.recorder.open(self.request)
        ingestion = replace(
            self.request.ingestion,
            causation_id=opening.started.event_id,
        )
        initial_event = self.events.append(
            observation_events(ingestion, recorded_at=NOW)[0],
            expected_sequence=0,
        )
        state = _project(self.events, initial_event.global_position, NOW)
        self.recorder.record_initial_state(RUN_ID, state)
        packet = project_signal_packet(self.request.signal, state)
        selection = DeterministicEvidenceRetriever().handle(
            self.request.retrieval,
            packet,
        )
        frame = build_context_frame(self.request.context, selection)
        context_event = self.recorder.record_context(RUN_ID, frame)
        self.initial_state = state
        self.frame = frame
        self.context_event = context_event

    def journal_decision(
        self,
        *,
        deterministic: bool = True,
        proposal: DecisionProposal | None = None,
        record_prefix_before_terminal: bool = False,
    ) -> DecisionStage:
        frame = _required(self.frame, "ContextFrame")
        context_event = _required(self.context_event, "context event")
        request = _decision_request(self.request, frame, context_event)
        registered = self.decision_journal.register(request, registered_at=MODEL_AT)
        preparation = self.decision_journal.record_route(
            registered,
            _route(),
            recorded_at=MODEL_AT,
        )
        assert isinstance(preparation, DecisionPreparation)
        acquired = self.decision_journal.acquire(preparation, acquired_at=MODEL_AT)
        assert isinstance(acquired, DecisionAttemptClaim)
        admitted = self.decision_journal.begin_invoke(
            preparation,
            acquired,
            invoked_at=MODEL_AT,
        )
        assert isinstance(admitted, DecisionAttemptClaim)
        if record_prefix_before_terminal:
            self.recorder.record_model_request(RUN_ID, registered)
            self.recorder.record_model_attempt(RUN_ID, preparation, admitted.attempt_record)
        selected_proposal = proposal or DecisionProposal(
            "proposal:feedback:1",
            frame.frame_id,
            self.request.execution_affordance.name,
            (),
            "inspect the bounded repository state",
            frame.provenance_event_ids,
        )
        response = DecisionResponse(
            request.request_id,
            request.request_digest,
            preparation.route.route_id,
            admitted.attempt_record.attempt.attempt_id,
            selected_proposal,
            MODEL_AT + timedelta(seconds=1),
        )
        usage = DecisionUsage(
            request.request_id,
            admitted.attempt_record.attempt.attempt_id,
            32,
            8,
            10,
            2,
            deterministic,
        )
        terminal = self.decision_journal.succeed(
            preparation,
            admitted,
            response,
            usage,
            recorded_at=MODEL_AT + timedelta(seconds=2),
        )
        stage = DecisionStage(
            registered,
            preparation,
            admitted.attempt_record,
            terminal,
        )
        self.decision = stage
        return stage

    def through_decision(self, *, record_authorization: bool = True) -> None:
        frame = _required(self.frame, "ContextFrame")
        stage = self.journal_decision()
        self.recorder.record_model_request(RUN_ID, stage.request_record)
        self.recorder.record_model_attempt(RUN_ID, stage.preparation, stage.attempt_record)
        self.recorder.record_model_terminal(RUN_ID, stage.terminal)
        terminal = stage.terminal
        model = terminal.response.proposal
        proposal = ActionProposal(
            model.proposal_id,
            model.context_frame_id,
            model.affordance,
            tuple(ActionArgument(item.name, item.value) for item in model.arguments),
            model.rationale,
            model.evidence_event_ids,
        )
        self.recorder.record_proposal(RUN_ID, proposal)
        constraints = DeterministicConstraintSolver().handle(
            self.request.constraints,
            frame,
        )
        self.recorder.record_constraints(RUN_ID, constraints)
        authorization = authorize_action(
            AuthorizeAction(
                proposal,
                self.request.authorization_affordance,
                self.request.constraints.evaluated_at,
                frame.provenance_event_ids,
                self.request.approval_granted,
            ),
            constraints,
        )
        if record_authorization:
            self.recorder.record_authorization(RUN_ID, authorization)
        self.proposal = proposal
        self.authorization = authorization

    def journal_execution(self) -> ExecutionJournalEntry:
        preparation, result = _execution_evidence(self)
        claim = self.execution_journal.acquire(preparation, acquired_at=EXECUTED_AT)
        assert isinstance(claim, ExecutionClaim)
        self.execution_journal.complete(
            claim,
            result,
            recorded_at=EXECUTED_AT + timedelta(seconds=1),
        )
        entry = self.execution_journal.list_entries()[0]
        self.execution_entry = entry
        return entry

    def through_execution(self) -> None:
        entry = self.journal_execution()
        execution_event = self.recorder.record_execution(RUN_ID, entry)
        self.execution_event = execution_event

    def through_outcome(self) -> None:
        entry = _required(self.execution_entry, "execution journal entry")
        execution_event = _required(self.execution_event, "execution event")
        proposal = _required(self.proposal, "ActionProposal")
        authorization = _required_authorization(self.authorization)
        result = _required(entry.current_result, "execution result")
        binding = OutcomeExecutionBinding(
            run_id=RUN_ID,
            invocation_id=result.invocation_id,
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.proposal_digest,
            authorization_decision_id=authorization.decision_id,
            authorized_action_digest=authorization.authorized_action_digest,
            execution_result_id=result.result_id,
            execution_identity_digest=result.execution_identity_digest,
            execution_status=result.status.value,
            affordance=result.affordance,
            arguments=(),
            execution_adapter_id=result.adapter_id,
            execution_adapter_contract_version="fixture-executor/v1",
            completed_at=result.completed_at,
        )
        observation = OutcomeObservation(
            observation_id="observation:outcome:1",
            binding=binding,
            evaluation_spec_id=self.request.evaluation_spec.spec_id,
            domain=DOMAIN,
            stream_id=OBSERVATION_STREAM,
            observer_id=self.request.expected_observer_id,
            observer_contract_version=self.request.expected_observer_contract_version,
            status=OutcomeObservationStatus.OBSERVED,
            observed_at=OBSERVED_AT,
            claims=(
                OutcomeClaim(
                    "claim:completed",
                    "project:blackcell",
                    "completed",
                    True,
                    1.0,
                ),
            ),
            evidence=(OutcomeEvidencePointer(locator="fixture://outcome"),),
        )
        command = IngestObservation(
            OBSERVATION_STREAM,
            1,
            ACTOR,
            observation.observer_id,
            RUN_ID,
            (outcome_observation_input(observation),),
            execution_event.event_id,
            DOMAIN,
        )
        domain_event = self.events.append(
            observation_events(command, recorded_at=OBSERVED_AT)[0],
            expected_sequence=1,
        )
        outcome_event = self.recorder.record_outcome(
            RUN_ID,
            observation,
            outcome_event_ids=(domain_event.event_id,),
        )
        outcome_state = _project(
            self.events,
            domain_event.global_position,
            OBSERVED_AT,
        )
        self.recorder.record_outcome_state(RUN_ID, outcome_state)
        bound = bind_evaluation_observation(
            observation,
            self.events,
            self.artifacts,
            execution_event_id=execution_event.event_id,
            outcome_event_ids=(domain_event.event_id,),
        )
        initial = _required(self.initial_state, "initial state")
        evaluation = OutcomeEvaluator(clock=lambda: EVALUATED_AT).handle(
            EvaluateOutcome(
                RUN_ID,
                self.request.evaluation_spec,
                EvaluationAuthorizationOutcome.ALLOW,
                EvaluationExecutionStatus.SUCCEEDED,
                execution_event.event_id,
                binding.binding_id,
                bound,
                initial.cutoff_global_position,
            )
        )
        self.recorder.record_evaluation(RUN_ID, evaluation)
        self.observation = observation
        self.outcome_event = outcome_event
        self.outcome_state = outcome_state
        self.evaluation = evaluation

    def through_transition(self) -> None:
        acceptance = bind_and_accept_state_transition(
            RUN_ID,
            self.events,
            self.artifacts,
        )
        assert acceptance.status is TransitionAcceptanceStatus.ACCEPTED
        transition = _required(acceptance.transition, "accepted transition")
        self.recorder.record_accepted_transition(RUN_ID, transition)

    def through_success(self) -> None:
        self.through_context()
        self.through_decision()
        self.through_execution()
        self.through_outcome()
        self.through_transition()


def test_successful_v2_run_is_exact_and_rebindable(tmp_path: Path) -> None:
    scenario = RunFixture.create(tmp_path)
    scenario.through_success()

    terminal = scenario.recorder.complete(RUN_ID, RunOutcome.EXECUTED)
    events = scenario.events.read_stream(run_stream_id(RUN_ID))

    assert tuple(item.event_type for item in events) == SUCCESS_EVENTS
    grammar = validate_run_grammar(events, run_id=RUN_ID)
    assert grammar.terminal and not grammar.failed
    assert terminal.trace_event == events[-2]
    assert terminal.terminal_event == events[-1]
    assert terminal.terminal_event.payload["authorization_outcome"] == "allow"
    assert terminal.terminal_event.payload["execution_status"] == "succeeded"

    rebound = bind_and_accept_state_transition(
        RUN_ID,
        scenario.events,
        scenario.artifacts,
    )
    assert rebound.status is TransitionAcceptanceStatus.ACCEPTED
    transition_event = events[-3]
    assert rebound.transition is not None
    assert transition_event.payload["transition_id"] == rebound.transition.transition_id

    trace_link = _artifact_mapping(terminal.trace_event)
    loaded_trace = scenario.artifacts.get_json(cast("str", trace_link["digest"]))
    assert isinstance(loaded_trace, dict)
    trace = cast("dict[str, object]", loaded_trace)
    loaded_entries = trace.get("entries")
    assert isinstance(loaded_entries, list)
    assert all(isinstance(item, dict) for item in loaded_entries)
    entries = cast("list[dict[str, object]]", loaded_entries)
    assert [item["event_id"] for item in entries] == [item.event_id for item in events[:-2]]
    assert [item["event_type"] for item in entries] == [item.event_type for item in events[:-2]]
    assert [item["global_position"] for item in entries] == [
        item.global_position for item in events[:-2]
    ]

    response = next(item for item in events if item.event_type == MODEL_RESPONDED)
    usage_link = _artifact_mapping(response, "usage_artifact")
    usage = decode_decision_usage(
        scenario.artifacts.get_bytes(cast("str", usage_link["digest"])),
        expected_usage_id=cast("str", usage_link["digest"]),
    )
    assert usage == _required(scenario.decision, "decision stage").terminal.usage
    assert response.payload["latency_ms"] == usage.latency_ms
    assert response.payload["cost_microusd"] == usage.cost_microusd
    _assert_all_artifacts_verify(events, scenario.artifacts)


def test_completed_trace_cannot_omit_a_derived_accepted_transition(
    tmp_path: Path,
) -> None:
    scenario = RunFixture.create(tmp_path)
    scenario.through_context()
    scenario.through_decision()
    scenario.through_execution()
    scenario.through_outcome()
    before = scenario.events.current_sequence(run_stream_id(RUN_ID))

    with pytest.raises(RunProtocolIntegrityError, match="accepted transition"):
        scenario.recorder.complete(RUN_ID, RunOutcome.EXECUTED)

    assert scenario.events.current_sequence(run_stream_id(RUN_ID)) == before
    assert all(
        event.event_type != TRACE_RECORDED
        for event in scenario.events.read_stream(run_stream_id(RUN_ID))
    )


@pytest.mark.parametrize("attempted", (False, True))
def test_model_failure_records_attempt_and_known_usage(
    tmp_path: Path,
    attempted: bool,
) -> None:
    scenario = RunFixture.create(tmp_path)
    scenario.through_context()
    frame = _required(scenario.frame, "ContextFrame")
    context_event = _required(scenario.context_event, "context event")
    request = _decision_request(scenario.request, frame, context_event)
    journal = scenario.decision_journal
    registered = journal.register(request, registered_at=MODEL_AT)
    preparation: DecisionPreparation | None = None
    attempt_record: DecisionAttemptRecord | None = None
    if attempted:
        prepared = journal.record_route(registered, _route(), recorded_at=MODEL_AT)
        assert isinstance(prepared, DecisionPreparation)
        preparation = prepared
        acquired = journal.acquire(prepared, acquired_at=MODEL_AT)
        assert isinstance(acquired, DecisionAttemptClaim)
        admitted = journal.begin_invoke(prepared, acquired, invoked_at=MODEL_AT)
        assert isinstance(admitted, DecisionAttemptClaim)
        attempt_record = admitted.attempt_record
        usage = DecisionUsage(
            request.request_id,
            attempt_record.attempt.attempt_id,
            32,
            8,
            10,
            2,
            True,
        )
        failure = DecisionFailure(
            request.request_id,
            request.request_digest,
            DecisionFailureKind.SCHEMA,
            "decision_output_invalid",
            False,
            MODEL_AT + timedelta(seconds=1),
            prepared.route.route_id,
            attempt_record.attempt.attempt_id,
            "ValueError",
        )
        terminal = journal.fail(
            registered,
            failure,
            preparation=prepared,
            claim=admitted,
            usage=usage,
            recorded_at=MODEL_AT + timedelta(seconds=2),
        )
    else:
        failure = DecisionFailure(
            request.request_id,
            request.request_digest,
            DecisionFailureKind.ADMISSION,
            "route_missing",
            False,
            MODEL_AT,
        )
        terminal = journal.reject(
            registered,
            failure,
            recorded_at=MODEL_AT,
        )

    scenario.recorder.record_model_request(RUN_ID, registered)
    if preparation is not None and attempt_record is not None:
        scenario.recorder.record_model_attempt(RUN_ID, preparation, attempt_record)
    failure_event = scenario.recorder.record_model_terminal(RUN_ID, terminal)
    result = scenario.recorder.fail(
        RUN_ID,
        phase="decision",
        error_type="DecisionFailure",
    )

    events = scenario.events.read_stream(run_stream_id(RUN_ID))
    expected = (
        RUN_STARTED,
        EVALUATION_SPECIFIED,
        INITIAL_STATE_RECORDED,
        CONTEXT_RECORDED,
        MODEL_REQUESTED,
        *((MODEL_ATTEMPT_RECORDED,) if attempted else ()),
        MODEL_FAILED,
        TRACE_RECORDED,
        RUN_FAILED,
    )
    assert tuple(item.event_type for item in events) == expected
    assert result.terminal_event == events[-1]
    failure_link = _artifact_mapping(failure_event)
    decoded = decode_decision_failure(
        scenario.artifacts.get_bytes(cast("str", failure_link["digest"])),
        expected_failure_id=cast("str", failure_link["digest"]),
    )
    assert decoded == terminal.failure
    assert "provider detail" not in scenario.artifacts.get_text(cast("str", failure_link["digest"]))
    usage_value = failure_event.payload["usage_artifact"]
    assert (usage_value is not None) is attempted
    if attempted:
        usage_link = _artifact_mapping(failure_event, "usage_artifact")
        assert (
            decode_decision_usage(
                scenario.artifacts.get_bytes(cast("str", usage_link["digest"])),
                expected_usage_id=cast("str", usage_link["digest"]),
            )
            == terminal.usage
        )
    else:
        assert all(
            failure_event.payload[field] is None
            for field in (
                "usage_id",
                "input_tokens",
                "output_tokens",
                "latency_ms",
                "cost_microusd",
                "deterministic",
            )
        )


def test_correctly_encoded_unjournaled_decision_request_is_rejected(
    tmp_path: Path,
) -> None:
    scenario = RunFixture.create(tmp_path)
    scenario.through_context()
    frame = _required(scenario.frame, "ContextFrame")
    context_event = _required(scenario.context_event, "context event")
    request = _decision_request(scenario.request, frame, context_event)
    artifact = scenario.artifacts.put_bytes(
        encode_decision_request(request),
        media_type=DECISION_REQUEST_MEDIA_TYPE,
        encoding="utf-8",
    )
    assert artifact.digest == request.request_digest
    spoofed = DecisionRequestRecord(request, artifact.digest, MODEL_AT)
    before = scenario.events.current_sequence(run_stream_id(RUN_ID))

    with pytest.raises(RunProtocolIntegrityError):
        scenario.recorder.record_model_request(RUN_ID, spoofed)

    assert scenario.events.current_sequence(run_stream_id(RUN_ID)) == before


def test_correctly_encoded_unjournaled_execution_evidence_is_rejected(
    tmp_path: Path,
) -> None:
    scenario = RunFixture.create(tmp_path)
    scenario.through_context()
    scenario.through_decision()
    preparation, result = _execution_evidence(scenario)
    preparation_artifact = scenario.artifacts.put_text(
        serialize_execution_preparation(preparation),
        media_type=EXECUTION_PREPARATION_MEDIA_TYPE,
    )
    result_artifact = scenario.artifacts.put_text(
        serialize_execution_result(result),
        media_type=EXECUTION_RESULT_MEDIA_TYPE,
    )
    assert preparation_artifact.digest == preparation.preparation_id
    assert result_artifact.digest == result.result_id
    spoofed = ExecutionJournalEntry(
        1,
        preparation.binding,
        ExecutionJournalStatus.SUCCEEDED,
        result,
        None,
        EXECUTED_AT,
        EXECUTED_AT + timedelta(seconds=1),
    )
    before = scenario.events.current_sequence(run_stream_id(RUN_ID))

    with pytest.raises(RunProtocolIntegrityError):
        scenario.recorder.record_execution(RUN_ID, spoofed)

    assert scenario.events.current_sequence(run_stream_id(RUN_ID)) == before


def test_route_determinism_downgrade_is_rejected(tmp_path: Path) -> None:
    scenario = RunFixture.create(tmp_path)
    scenario.through_context()
    stage = scenario.journal_decision(deterministic=False)
    scenario.recorder.record_model_request(RUN_ID, stage.request_record)
    scenario.recorder.record_model_attempt(RUN_ID, stage.preparation, stage.attempt_record)
    before = scenario.events.current_sequence(run_stream_id(RUN_ID))

    with pytest.raises(RunProtocolIntegrityError):
        scenario.recorder.record_model_terminal(RUN_ID, stage.terminal)

    assert scenario.events.current_sequence(run_stream_id(RUN_ID)) == before


def test_journal_owned_response_outside_request_output_contract_is_rejected(
    tmp_path: Path,
) -> None:
    scenario = RunFixture.create(tmp_path)
    scenario.through_context()
    frame = _required(scenario.frame, "ContextFrame")
    invalid_proposal = DecisionProposal(
        "proposal:feedback:invalid",
        "context:outside-request",
        scenario.request.execution_affordance.name,
        (),
        "cite a ContextFrame the request did not authorize",
        frame.provenance_event_ids,
    )
    stage = scenario.journal_decision(
        proposal=invalid_proposal,
        record_prefix_before_terminal=True,
    )
    before = scenario.events.current_sequence(run_stream_id(RUN_ID))

    with pytest.raises(RunProtocolIntegrityError, match="violates its request contract"):
        scenario.recorder.record_model_terminal(RUN_ID, stage.terminal)

    assert scenario.events.current_sequence(run_stream_id(RUN_ID)) == before


def test_observation_with_extra_evaluation_target_is_rejected(tmp_path: Path) -> None:
    scenario = RunFixture.create(tmp_path)
    scenario.through_context()
    scenario.through_decision()
    scenario.through_execution()
    execution_event = _required(scenario.execution_event, "execution event")
    observation, domain_event = _unrecorded_outcome(
        scenario,
        causation_id=execution_event.event_id,
        claims=(
            OutcomeClaim("claim:completed", "project:blackcell", "completed", True),
            OutcomeClaim("claim:extra", "project:blackcell", "unexpected", True),
        ),
    )
    before = scenario.events.current_sequence(run_stream_id(RUN_ID))

    with pytest.raises(RunProtocolIntegrityError):
        scenario.recorder.record_outcome(
            RUN_ID,
            observation,
            outcome_event_ids=(domain_event.event_id,),
        )

    assert scenario.events.current_sequence(run_stream_id(RUN_ID)) == before


def test_generic_failure_cannot_conceal_unrecorded_allowed_execution(
    tmp_path: Path,
) -> None:
    scenario = RunFixture.create(tmp_path)
    scenario.through_context()
    scenario.through_decision()
    scenario.journal_execution()
    before = scenario.events.current_sequence(run_stream_id(RUN_ID))

    with pytest.raises(RunInterrupted, match="requiring reconciliation"):
        scenario.recorder.fail(RUN_ID, phase="execution", error_type="RuntimeError")

    assert scenario.events.current_sequence(run_stream_id(RUN_ID)) == before


def test_generic_failure_cannot_conceal_execution_before_authorization_record(
    tmp_path: Path,
) -> None:
    scenario = RunFixture.create(tmp_path)
    scenario.through_context()
    scenario.through_decision(record_authorization=False)
    scenario.journal_execution()
    before = scenario.events.current_sequence(run_stream_id(RUN_ID))

    with pytest.raises(RunInterrupted, match="requiring reconciliation"):
        scenario.recorder.fail(RUN_ID, phase="authorization", error_type="RuntimeError")

    assert scenario.events.current_sequence(run_stream_id(RUN_ID)) == before


def test_generic_failure_cannot_conceal_unrecorded_active_decision_attempt(
    tmp_path: Path,
) -> None:
    scenario = RunFixture.create(tmp_path)
    scenario.through_context()
    frame = _required(scenario.frame, "ContextFrame")
    context_event = _required(scenario.context_event, "context event")
    request = _decision_request(scenario.request, frame, context_event)
    registered = scenario.decision_journal.register(request, registered_at=MODEL_AT)
    preparation = scenario.decision_journal.record_route(
        registered,
        _route(),
        recorded_at=MODEL_AT,
    )
    assert isinstance(preparation, DecisionPreparation)
    acquired = scenario.decision_journal.acquire(preparation, acquired_at=MODEL_AT)
    assert isinstance(acquired, DecisionAttemptClaim)
    admitted = scenario.decision_journal.begin_invoke(
        preparation,
        acquired,
        invoked_at=MODEL_AT,
    )
    assert isinstance(admitted, DecisionAttemptClaim)
    scenario.recorder.record_model_request(RUN_ID, registered)
    before = scenario.events.current_sequence(run_stream_id(RUN_ID))

    with pytest.raises(RunInterrupted, match="decision-journal evidence"):
        scenario.recorder.fail(RUN_ID, phase="model", error_type="RuntimeError")

    assert scenario.events.current_sequence(run_stream_id(RUN_ID)) == before


def test_generic_failure_cannot_conceal_unrecorded_decision_terminal(
    tmp_path: Path,
) -> None:
    scenario = RunFixture.create(tmp_path)
    scenario.through_context()
    frame = _required(scenario.frame, "ContextFrame")
    context_event = _required(scenario.context_event, "context event")
    request = _decision_request(scenario.request, frame, context_event)
    registered = scenario.decision_journal.register(request, registered_at=MODEL_AT)
    failure = DecisionFailure(
        request.request_id,
        request.request_digest,
        DecisionFailureKind.ADMISSION,
        "no_route",
        False,
        MODEL_AT,
    )
    scenario.decision_journal.reject(registered, failure, recorded_at=MODEL_AT)
    scenario.recorder.record_model_request(RUN_ID, registered)
    before = scenario.events.current_sequence(run_stream_id(RUN_ID))

    with pytest.raises(RunInterrupted, match="decision-journal evidence"):
        scenario.recorder.fail(RUN_ID, phase="model", error_type="RuntimeError")

    assert scenario.events.current_sequence(run_stream_id(RUN_ID)) == before


def test_restart_redelivery_and_terminal_commit_recovery(tmp_path: Path) -> None:
    scenario = RunFixture.create(tmp_path, FailOnceCompletedStore)
    scenario.through_context()
    scenario.through_decision()
    stage = _required(scenario.decision, "decision stage")
    attempt_event = next(
        item
        for item in scenario.events.read_stream(run_stream_id(RUN_ID))
        if item.event_type == MODEL_ATTEMPT_RECORDED
    )
    sequence = scenario.events.current_sequence(run_stream_id(RUN_ID))
    restarted = KernelFeedbackRunRecorder(
        scenario.events,
        scenario.artifacts,
        scenario.decision_journal,
        scenario.execution_journal,
        clock=lambda: RECORDED_AT,
    )

    with pytest.raises(RunInterrupted, match="explicit recovery"):
        restarted.open(scenario.request)
    assert (
        restarted.record_model_attempt(
            RUN_ID,
            stage.preparation,
            stage.attempt_record,
        )
        == attempt_event
    )
    assert scenario.events.current_sequence(run_stream_id(RUN_ID)) == sequence

    forged = replace(
        stage.attempt_record.attempt,
        started_at=stage.attempt_record.attempt.started_at + timedelta(seconds=1),
    )
    forged_ref = scenario.artifacts.put_bytes(
        encode_decision_attempt(forged),
        media_type=DECISION_ATTEMPT_MEDIA_TYPE,
        encoding="utf-8",
    )
    with pytest.raises(RunProtocolIntegrityError, match="durable preparation"):
        restarted.record_model_attempt(
            RUN_ID,
            stage.preparation,
            DecisionAttemptRecord(forged, forged_ref.digest),
        )

    scenario.through_execution()
    scenario.through_outcome()
    scenario.through_transition()
    with pytest.raises(RuntimeError, match="terminal commit failure"):
        scenario.recorder.complete(RUN_ID, RunOutcome.EXECUTED)

    stored = scenario.events.read_stream(run_stream_id(RUN_ID))
    assert stored[-1].event_type == TRACE_RECORDED
    assert sum(item.event_type == TRACE_RECORDED for item in stored) == 1
    fresh = KernelFeedbackRunRecorder(
        EventStore(scenario.database),
        ArtifactStore(scenario.root, database_path=scenario.database),
        SQLiteDecisionAttemptJournal(scenario.root, database_path=scenario.database),
        SQLiteExecutionJournal(scenario.root, database_path=scenario.database),
        clock=lambda: RECORDED_AT,
    )
    with pytest.raises(RunIdentityConflict, match="different outcome"):
        fresh.fail(RUN_ID, phase="terminal", error_type="RuntimeError")
    terminal = fresh.complete(RUN_ID, RunOutcome.EXECUTED)
    final = scenario.events.read_stream(run_stream_id(RUN_ID))
    assert sum(item.event_type == TRACE_RECORDED for item in final) == 1
    assert terminal.trace_event == final[-2]
    assert terminal.terminal_event == final[-1]
    with pytest.raises(RunAlreadyExists):
        fresh.open(scenario.request)


@pytest.mark.parametrize(
    "boundary",
    (
        "model-terminal",
        "execution",
        "outcome-causation",
        "outcome-state",
        "evaluation",
        "transition",
    ),
)
def test_cross_identity_joins_fail_without_advancing(
    tmp_path: Path,
    boundary: str,
) -> None:
    scenario = RunFixture.create(tmp_path)
    scenario.through_context()
    if boundary == "model-terminal":
        scenario.through_decision()
        stage = _required(scenario.decision, "decision stage")
        forged_terminal = _forged_terminal(scenario, stage)
        before = scenario.events.current_sequence(run_stream_id(RUN_ID))
        with pytest.raises(RunProtocolIntegrityError, match="not owned by its durable journal"):
            scenario.recorder.record_model_terminal(RUN_ID, forged_terminal)
    else:
        scenario.through_decision()
        if boundary == "execution":
            scenario.through_execution()
            entry = _required(scenario.execution_entry, "execution journal entry")
            changed = replace(entry.binding, run_id="run:other")
            before = scenario.events.current_sequence(run_stream_id(RUN_ID))
            with pytest.raises(RunProtocolIntegrityError, match="journal entry is not terminal"):
                scenario.recorder.record_execution(RUN_ID, replace(entry, binding=changed))
        elif boundary == "outcome-causation":
            scenario.through_execution()
            wrong_cause = _required(scenario.context_event, "context event").event_id
            observation, domain_event = _unrecorded_outcome(
                scenario,
                causation_id=wrong_cause,
            )
            before = scenario.events.current_sequence(run_stream_id(RUN_ID))
            with pytest.raises(OutcomeEvidenceBindingError, match="not caused"):
                scenario.recorder.record_outcome(
                    RUN_ID,
                    observation,
                    outcome_event_ids=(domain_event.event_id,),
                )
        else:
            scenario.through_execution()
            scenario.through_outcome()
            if boundary == "outcome-state":
                initial = _required(scenario.initial_state, "initial state")
                before = scenario.events.current_sequence(run_stream_id(RUN_ID))
                with pytest.raises(RunProtocolIntegrityError, match="does not include"):
                    scenario.recorder.record_outcome_state(RUN_ID, initial)
            elif boundary == "evaluation":
                evaluation = _required(scenario.evaluation, "outcome evaluation")
                before = scenario.events.current_sequence(run_stream_id(RUN_ID))
                with pytest.raises(RunProtocolIntegrityError, match="deterministic replay"):
                    scenario.recorder.record_evaluation(
                        RUN_ID,
                        replace(evaluation, run_id="run:other"),
                    )
            else:
                acceptance = bind_and_accept_state_transition(
                    RUN_ID,
                    scenario.events,
                    scenario.artifacts,
                )
                transition = _required(acceptance.transition, "accepted transition")
                forged = replace(
                    transition,
                    proposal=replace(
                        transition.proposal,
                        proposal_artifact_digest=DIGEST,
                    ),
                )
                before = scenario.events.current_sequence(run_stream_id(RUN_ID))
                with pytest.raises(RunProtocolIntegrityError, match="verified run evidence"):
                    scenario.recorder.record_accepted_transition(RUN_ID, forged)
    assert scenario.events.current_sequence(run_stream_id(RUN_ID)) == before


@pytest.mark.parametrize(
    "corruption",
    (
        "unexpected-field",
        "artifact-logical-id",
        "owner-field",
        "record-time",
        "artifact-bytes",
    ),
)
def test_restart_rejects_stored_corruption(tmp_path: Path, corruption: str) -> None:
    scenario = RunFixture.create(tmp_path, TamperingStore)
    scenario.through_success()
    scenario.recorder.complete(RUN_ID, RunOutcome.EXECUTED)
    assert isinstance(scenario.events, TamperingStore)
    expected_sequence = EventStore(scenario.database).current_sequence(run_stream_id(RUN_ID))

    if corruption == "unexpected-field":
        scenario.events.mutation = lambda stored: _replace_payload(
            stored,
            _index(stored, AUTHORIZATION_DECIDED),
            unexpected=True,
        )
    elif corruption == "artifact-logical-id":

        def mutate_link(stored: tuple[EventEnvelope, ...]) -> tuple[EventEnvelope, ...]:
            index = _index(stored, CONTEXT_RECORDED)
            payload: dict[str, JsonInput] = dict(stored[index].payload)
            artifact = cast(
                "dict[str, JsonInput]",
                dict(_artifact_mapping(stored[index])),
            )
            artifact["logical_id"] = DIGEST
            payload["artifact"] = artifact
            return _replace_event(stored, index, payload=payload)

        scenario.events.mutation = mutate_link
    elif corruption == "owner-field":
        scenario.events.mutation = lambda stored: _replace_payload(
            stored,
            _index(stored, MODEL_RESPONDED),
            proposal_id="proposal:forged",
        )
    elif corruption == "record-time":

        def regress_time(stored: tuple[EventEnvelope, ...]) -> tuple[EventEnvelope, ...]:
            index = _index(stored, CONTEXT_RECORDED)
            return _replace_event(
                stored,
                index,
                recorded_at=RECORDED_AT - timedelta(seconds=1),
            )

        scenario.events.mutation = regress_time
    else:
        evaluation = next(
            item
            for item in EventStore(scenario.database).read_stream(run_stream_id(RUN_ID))
            if item.event_type == EVALUATION_RECORDED
        )
        digest = cast("str", _artifact_mapping(evaluation)["digest"])
        scenario.artifacts.path_for(digest).write_bytes(b"corrupt")

    fresh = KernelFeedbackRunRecorder(
        scenario.events,
        scenario.artifacts,
        scenario.decision_journal,
        scenario.execution_journal,
        clock=lambda: RECORDED_AT,
    )
    with pytest.raises(RunProtocolIntegrityError):
        fresh.open(scenario.request)
    scenario.events.mutation = None
    assert (
        EventStore(scenario.database).current_sequence(run_stream_id(RUN_ID)) == expected_sequence
    )


def _workflow_request() -> DailyOperatorV2Request:
    objective = "inspect project status"
    return DailyOperatorV2Request(
        run_id=RUN_ID,
        ingestion=IngestObservation(
            OBSERVATION_STREAM,
            0,
            ACTOR,
            "fixture.initial",
            RUN_ID,
            (
                ObservationInput(
                    "observation:initial",
                    NOW,
                    (
                        ObservedClaim(
                            "claim:status",
                            "project:blackcell",
                            "status",
                            "ready",
                            1.0,
                        ),
                    ),
                    (EvidencePointer(locator="fixture://initial"),),
                    "initial:1",
                ),
            ),
            None,
            DOMAIN,
        ),
        initial_effective_time_cutoff=NOW,
        signal=DeriveSignalPacket("daily", NOW, 3_600),
        retrieval=RetrieveEvidence(
            objective,
            (EvidenceKey("project:blackcell", "status"),),
            4,
        ),
        context=BuildContext("task:daily", objective, NOW, 4_000),
        constraints=SolveConstraints(
            NOW,
            (
                ConstraintDefinition(
                    "constraint:ready",
                    "project must be ready",
                    "project:blackcell",
                    "status",
                    ConstraintOperator.EQUALS,
                    ("ready",),
                    0.8,
                    3_600,
                ),
            ),
        ),
        evaluation_spec=EvaluationSpec(
            "daily-success",
            objective,
            (
                EvaluationCriterion(
                    "criterion:completed",
                    "project:blackcell",
                    "completed",
                    True,
                    0.8,
                    True,
                ),
            ),
        ),
        gateway_requirements=DecisionRequirements(
            "decision:feedback:1",
            "node:planner",
            DecisionCapability.REASON,
            DecisionClassification.PRIVATE,
            DecisionLocality.LOCAL_ONLY,
            DecisionBudget(512, 128, 1_000, 100),
            64,
            True,
            NOW,
        ),
        authorization_affordance=AffordancePolicy("inspect", True),
        execution_affordance=AffordanceDefinition(
            "inspect",
            "adapter:fixture",
            SideEffectClass.READ_ONLY,
            10,
        ),
        invocation_id="invocation:feedback:1",
        idempotency_key="execution:feedback:1",
        expected_observer_id="observer:fixture",
        expected_observer_contract_version="observer-fixture/v1",
    )


def _decision_request(
    workflow: DailyOperatorV2Request,
    frame: ContextFrame,
    context_event: EventEnvelope,
) -> RequestDecision:
    return RequestDecision(
        workflow.gateway_requirements,
        RUN_ID,
        RUN_ID,
        context_event.event_id,
        frame.frame_id,
        frame.objective,
        frame.model_payload,
        frame.provenance_event_ids,
        (DecisionAffordance(workflow.execution_affordance.name),),
    )


def _route() -> DecisionRoute:
    return DecisionRoute(
        "reason-local",
        "recorded",
        "fixture-model",
        DecisionCapability.REASON,
        True,
        True,
        MODEL_AT,
    )


def _project(
    events: EventStore,
    position: int | None,
    effective_time: datetime,
) -> OperationalBeliefState:
    assert position is not None
    return ProjectOperationalStateHandler(events, NoCheckpoints()).handle(
        ProjectOperationalState(
            OperationalStateScope(DOMAIN, OBSERVATION_STREAM),
            as_of_time=effective_time,
            as_of_position=position,
        )
    )


def _execution_evidence(
    scenario: RunFixture,
) -> tuple[ExecutionPreparation, ExecutionResult]:
    proposal = _required(scenario.proposal, "ActionProposal")
    authorization = _required_authorization(scenario.authorization)
    invocation = AffordanceInvocation(
        scenario.request.invocation_id,
        proposal.proposal_id,
        proposal.affordance,
        (),
        scenario.request.idempotency_key,
        EXECUTED_AT,
    )
    preparation = ExecutionPreparation(
        RUN_ID,
        invocation,
        scenario.request.execution_affordance,
        authorization.decision_id,
        proposal.action_digest,
        "fixture-executor/v1",
    )
    result = ExecutionResult(
        invocation_id=invocation.invocation_id,
        proposal_id=proposal.proposal_id,
        authorization_decision_id=authorization.decision_id,
        affordance=proposal.affordance,
        adapter_id=scenario.request.execution_affordance.adapter_id,
        idempotency_key=invocation.idempotency_key,
        authorized_action_digest=proposal.action_digest,
        execution_identity_digest=preparation.binding.execution_identity_digest,
        status=ExecutionStatus.SUCCEEDED,
        started_at=EXECUTED_AT,
        completed_at=EXECUTED_AT + timedelta(seconds=1),
        output_digest=DIGEST,
        observed_effects=(),
        error_code=None,
        reconciled=False,
    )
    return preparation, result


def _forged_terminal(
    scenario: RunFixture,
    stage: DecisionStage,
) -> DecisionSuccessRecord:
    request = stage.request_record.request
    attempt = replace(
        stage.attempt_record.attempt,
        started_at=stage.attempt_record.attempt.started_at + timedelta(seconds=1),
    )
    attempt_ref = scenario.artifacts.put_bytes(
        encode_decision_attempt(attempt),
        media_type=DECISION_ATTEMPT_MEDIA_TYPE,
        encoding="utf-8",
    )
    attempt_record = DecisionAttemptRecord(attempt, attempt_ref.digest)
    response = DecisionResponse(
        request.request_id,
        request.request_digest,
        stage.preparation.route.route_id,
        attempt.attempt_id,
        stage.terminal.response.proposal,
        stage.terminal.response.completed_at + timedelta(seconds=1),
    )
    response_ref = scenario.artifacts.put_bytes(
        encode_decision_response(response),
        media_type=DECISION_RESPONSE_MEDIA_TYPE,
        encoding="utf-8",
    )
    usage = DecisionUsage(
        request.request_id,
        attempt.attempt_id,
        32,
        8,
        10,
        2,
        True,
    )
    usage_ref = scenario.artifacts.put_bytes(
        encode_decision_usage(usage),
        media_type=DECISION_USAGE_MEDIA_TYPE,
        encoding="utf-8",
    )
    return DecisionSuccessRecord(
        stage.preparation,
        attempt_record,
        response,
        response_ref.digest,
        usage,
        usage_ref.digest,
    )


def _unrecorded_outcome(
    scenario: RunFixture,
    *,
    causation_id: str,
    claims: tuple[OutcomeClaim, ...] | None = None,
) -> tuple[OutcomeObservation, EventEnvelope]:
    entry = _required(scenario.execution_entry, "execution journal entry")
    proposal = _required(scenario.proposal, "ActionProposal")
    authorization = _required_authorization(scenario.authorization)
    result = _required(entry.current_result, "execution result")
    binding = OutcomeExecutionBinding(
        RUN_ID,
        result.invocation_id,
        proposal.proposal_id,
        proposal.proposal_digest,
        authorization.decision_id,
        authorization.authorized_action_digest,
        result.result_id,
        result.execution_identity_digest,
        result.status.value,
        result.affordance,
        (),
        result.adapter_id,
        "fixture-executor/v1",
        result.completed_at,
    )
    observation = OutcomeObservation(
        "observation:outcome:forged",
        binding,
        scenario.request.evaluation_spec.spec_id,
        DOMAIN,
        OBSERVATION_STREAM,
        scenario.request.expected_observer_id,
        scenario.request.expected_observer_contract_version,
        OutcomeObservationStatus.OBSERVED,
        OBSERVED_AT,
        claims or (OutcomeClaim("claim:forged", "project:blackcell", "completed", True),),
        (OutcomeEvidencePointer(locator="fixture://forged"),),
    )
    command = IngestObservation(
        OBSERVATION_STREAM,
        1,
        ACTOR,
        observation.observer_id,
        RUN_ID,
        (outcome_observation_input(observation),),
        causation_id,
        DOMAIN,
    )
    event = scenario.events.append(
        observation_events(command, recorded_at=OBSERVED_AT)[0],
        expected_sequence=1,
    )
    return observation, event


def _required(value, label: str):
    assert value is not None, f"fixture lacks {label}"
    return value


def _required_authorization(value: object | None):
    from blackcell.features.authorize_action import AuthorizationDecision

    assert isinstance(value, AuthorizationDecision)
    return value


def _artifact_mapping(
    event: EventEnvelope,
    field: str = "artifact",
) -> Mapping[str, object]:
    value = event.payload.get(field)
    assert isinstance(value, Mapping)
    return cast("Mapping[str, object]", value)


def _assert_all_artifacts_verify(
    events: tuple[EventEnvelope, ...],
    artifacts: ArtifactStore,
) -> None:
    for event in events:
        for name, value in event.payload.items():
            if (name == "artifact" or name.endswith("_artifact")) and value is not None:
                assert isinstance(value, Mapping)
                link = dict(_artifact_mapping(event, name))
                digest = link.get("digest")
                assert isinstance(digest, str)
                assert artifacts.verify(digest)


def _index(events: tuple[EventEnvelope, ...], event_type: str) -> int:
    return next(index for index, item in enumerate(events) if item.event_type == event_type)


def _replace_payload(
    events: tuple[EventEnvelope, ...],
    index: int,
    **changes: JsonInput,
) -> tuple[EventEnvelope, ...]:
    return _replace_event(events, index, payload={**events[index].payload, **changes})


def _replace_event(
    events: tuple[EventEnvelope, ...],
    index: int,
    *,
    payload: Mapping[str, JsonInput] | None = None,
    recorded_at: datetime | None = None,
) -> tuple[EventEnvelope, ...]:
    event = events[index]
    rebuilt = EventEnvelope.create(
        event_id=event.event_id,
        stream_id=event.stream_id,
        stream_sequence=event.stream_sequence,
        event_type=event.event_type,
        schema_version=event.schema_version,
        actor=event.actor,
        source=event.source,
        payload=event.payload if payload is None else payload,
        recorded_at=event.recorded_at if recorded_at is None else recorded_at,
        effective_at=event.effective_at,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        idempotency_key=event.idempotency_key,
    )
    changed = replace(rebuilt, global_position=event.global_position)
    return (*events[:index], changed, *events[index + 1 :])
