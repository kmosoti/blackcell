from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from blackcell.adapters.persistence.sqlite import (
    ArtifactContextFrameStore,
    KernelRunRecorder,
    SQLiteExecutionJournal,
)
from blackcell.features.authorize_action import (
    ActionArgument,
    ActionProposal,
    AffordancePolicy,
    AuthorizationOutcome,
)
from blackcell.features.build_context import (
    BuildContext,
    ContextFrame,
    ContextFrameIntegrityError,
    ContextFrameStorageError,
)
from blackcell.features.derive_signal_packet import DeriveSignalPacket
from blackcell.features.execute_affordance import (
    AdapterOutcome,
    AffordanceArgumentSpec,
    AffordanceDefinition,
    AffordanceExecutionHandler,
    ExecutionStatus,
    SideEffectClass,
    UncertainExecutionError,
)
from blackcell.features.ingest_observation import (
    EvidencePointer,
    IngestObservation,
    IngestObservationHandler,
    ObservationInput,
    ObservedClaim,
)
from blackcell.features.retrieve_evidence import EvidenceKey, RetrieveEvidence
from blackcell.features.solve_constraints import (
    ConstraintDefinition,
    ConstraintOperator,
    SolveConstraints,
)
from blackcell.kernel import ArtifactStore, EventStore
from blackcell.workflows import (
    DailyOperatorRequest,
    DailyOperatorWorkflow,
    RunAlreadyExists,
    RunInterrupted,
    RunRecorder,
    RunStart,
    daily_operator_request_digest,
    run_stream_id,
)
from blackcell.workflows.run_protocol import (
    AUTHORIZATION_DECIDED,
    CONSTRAINTS_EVALUATED,
    CONTEXT_RECORDED,
    EXECUTION_RECORDED,
    PROPOSAL_RECORDED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_STARTED,
    TRACE_RECORDED,
)

NOW = datetime(2026, 7, 10, 22, tzinfo=UTC)


class Decision:
    def __init__(
        self,
        events: list[tuple[str, ContextFrame]],
        *,
        failure: Exception | None = None,
    ) -> None:
        self.frames: list[ContextFrame] = []
        self.events = events
        self.failure = failure

    def propose(self, frame: ContextFrame) -> ActionProposal:
        self.frames.append(frame)
        self.events.append(("decision", frame))
        if self.failure is not None:
            raise self.failure
        return ActionProposal(
            "proposal:1",
            frame.frame_id,
            "inspect",
            (ActionArgument("path", "README.md"),),
            "inspect the cited repository evidence",
            frame.provenance_event_ids,
        )


class ContextFrames:
    def __init__(
        self,
        delegate: ArtifactContextFrameStore,
        events: list[tuple[str, ContextFrame]],
        *,
        failure: ContextFrameStorageError | None = None,
        return_different: bool = False,
    ) -> None:
        self.delegate = delegate
        self.events = events
        self.failure = failure
        self.return_different = return_different
        self.frames: dict[str, ContextFrame] = {}
        self.persisted: list[ContextFrame] = []

    def put(self, frame: ContextFrame) -> ContextFrame:
        if self.failure is not None:
            self.events.append(("persistence-failed", frame))
            raise self.failure
        persisted = self.delegate.put(frame)
        stored = (
            replace(persisted, task_id="task:different") if self.return_different else persisted
        )
        self.frames[stored.frame_id] = stored
        self.persisted.append(stored)
        self.events.append(("persisted", stored))
        return stored

    def get(self, frame_id: str) -> ContextFrame | None:
        return self.delegate.get(frame_id)

    def list_frames(self) -> tuple[ContextFrame, ...]:
        return self.delegate.list_frames()


class FailingRuns:
    def __init__(self, delegate: KernelRunRecorder, failure: Exception) -> None:
        self.delegate = delegate
        self.failure = failure

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)

    def fail(self, run_id: str, *, phase: str, error_type: str):
        del run_id, phase, error_type
        raise self.failure


class Adapter:
    adapter_id = "fixture"
    contract_version = "fixture/v1"

    def __init__(self, *, success: bool = True, uncertain: bool = False) -> None:
        self.calls = 0
        self.success = success
        self.uncertain = uncertain

    def execute(self, invocation, definition):
        self.calls += 1
        if self.uncertain:
            raise UncertainExecutionError
        return AdapterOutcome(
            self.success,
            "sha256:output",
            NOW,
            error_code=None if self.success else "fixture_failed",
        )

    def reconcile(self, invocation, definition, previous):
        raise AssertionError("read-only fixture should not reconcile")


def test_daily_operator_runs_the_complete_allowed_control_loop(tmp_path: Path) -> None:
    workflow, adapter, decision, context_frames, events, store, artifacts = _workflow(tmp_path)

    result = workflow.run(_request("ready", ConstraintOperator.EQUALS, ("ready",)))

    assert result.state.claims[0].value == "ready"
    assert result.signal_packet.provenance_event_ids == result.context_frame.provenance_event_ids
    assert result.signal_packet.purpose == result.context_frame.source_packet_purpose == "daily"
    assert (
        (
            result.state.scope.domain,
            result.state.scope.stream_id,
        )
        == (
            result.signal_packet.state_domain,
            result.signal_packet.state_stream_id,
        )
        == (
            result.evidence_selection.state_domain,
            result.evidence_selection.state_stream_id,
        )
        == (
            result.context_frame.state_domain,
            result.context_frame.state_stream_id,
        )
    )
    assert result.context_frame.evidence[0].claim_id == result.state.claims[0].claim_id
    assert (
        result.context_frame.evidence[0].global_position == result.state.claims[0].global_position
    )
    assert result.constraint_evaluation.safe
    assert result.authorization.outcome is AuthorizationOutcome.ALLOW
    assert result.execution is not None
    assert result.execution.status is ExecutionStatus.SUCCEEDED
    assert adapter.calls == 1
    assert decision.frames == [result.context_frame]
    assert events == [
        ("persisted", result.context_frame),
        ("decision", result.context_frame),
    ]
    assert context_frames.persisted[0] is decision.frames[0]
    assert decision.frames[0] is result.context_frame
    run_events = store.read_stream(run_stream_id(result.run_id))
    assert tuple(item.event_type for item in run_events) == (
        RUN_STARTED,
        CONTEXT_RECORDED,
        PROPOSAL_RECORDED,
        CONSTRAINTS_EVALUATED,
        AUTHORIZATION_DECIDED,
        EXECUTION_RECORDED,
        TRACE_RECORDED,
        RUN_COMPLETED,
    )
    assert result.observations[0].causation_id == run_events[0].event_id
    assert run_events[1].causation_id == run_events[0].event_id
    assert tuple(item.causation_id for item in run_events[2:]) == tuple(
        item.event_id for item in run_events[1:-1]
    )
    assert all(item.correlation_id == result.run_id for item in run_events)
    assert run_events[-1].payload["outcome"] == "executed"
    assert run_events[5].payload["artifact"]["digest"] == result.execution.result_id
    assert all(
        artifacts.verify(str(item.payload["artifact"]["digest"])) for item in run_events[1:-1]
    )


def test_symbolic_violation_stops_daily_operator_before_execution(tmp_path: Path) -> None:
    workflow, adapter, _, _, _, store, _ = _workflow(tmp_path)

    result = workflow.run(_request("blocked", ConstraintOperator.NOT_EQUALS, ("blocked",)))

    assert result.authorization.outcome is AuthorizationOutcome.DENY
    assert result.execution is None
    assert adapter.calls == 0
    run_events = store.read_stream(run_stream_id(result.run_id))
    assert tuple(item.event_type for item in run_events) == (
        RUN_STARTED,
        CONTEXT_RECORDED,
        PROPOSAL_RECORDED,
        CONSTRAINTS_EVALUATED,
        AUTHORIZATION_DECIDED,
        TRACE_RECORDED,
        RUN_COMPLETED,
    )
    assert run_events[-1].payload["outcome"] == "denied"


@pytest.mark.parametrize(
    ("fixture_options", "expected_status", "expected_outcome"),
    (
        ({"execution_success": False}, ExecutionStatus.FAILED, "execution-failed"),
        (
            {"execution_uncertain": True},
            ExecutionStatus.UNKNOWN,
            "requires-reconciliation",
        ),
    ),
)
def test_execution_terminal_status_maps_to_exact_run_outcome(
    tmp_path: Path,
    fixture_options,
    expected_status: ExecutionStatus,
    expected_outcome: str,
) -> None:
    workflow, adapter, _, _, _, store, _ = _workflow(tmp_path, **fixture_options)

    result = workflow.run(_request("ready", ConstraintOperator.EQUALS, ("ready",)))

    assert adapter.calls == 1
    assert result.execution is not None
    assert result.execution.status is expected_status
    assert store.read_stream(run_stream_id(result.run_id))[-1].payload["outcome"] == (
        expected_outcome
    )


def test_approval_required_is_completed_without_execution(tmp_path: Path) -> None:
    workflow, adapter, _, _, _, store, _ = _workflow(tmp_path)
    request = _request("ready", ConstraintOperator.EQUALS, ("ready",))
    request = replace(
        request,
        authorization_affordance=AffordancePolicy(
            "inspect",
            False,
            mutates_state=True,
            allowed_arguments=("path",),
        ),
        execution_affordance=replace(
            request.execution_affordance,
            side_effect_class=SideEffectClass.REVERSIBLE,
        ),
    )

    result = workflow.run(request)

    assert result.authorization.outcome is AuthorizationOutcome.REQUIRE_APPROVAL
    assert result.execution is None
    assert adapter.calls == 0
    assert store.read_stream(run_stream_id(result.run_id))[-1].payload["outcome"] == (
        "approval-required"
    )


def test_daily_operator_projects_only_the_requested_observation_scope(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    IngestObservationHandler(store, clock=lambda: NOW).handle(
        IngestObservation(
            "observations:personal",
            0,
            "operator",
            "fixture",
            "run:personal",
            (
                ObservationInput(
                    "obs:personal",
                    NOW,
                    (
                        ObservedClaim(
                            "claim:personal",
                            "project:blackcell",
                            "status",
                            "blocked",
                        ),
                    ),
                    (EvidencePointer(locator="fixture://personal"),),
                ),
            ),
            domain="personal-planning",
        )
    )
    workflow, _, _, _, _, _, _ = _workflow(tmp_path)

    result = workflow.run(_request("ready", ConstraintOperator.EQUALS, ("ready",)))

    assert result.state.scope.domain == "repository"
    assert result.state.scope.stream_id == "observations:daily"
    assert tuple(claim.value for claim in result.state.claims) == ("ready",)
    assert result.state.cutoff_global_position == 3
    assert result.state.last_source_stream_sequence == 1


def test_context_persistence_failure_prevents_reasoning_and_execution(tmp_path: Path) -> None:
    failure = ContextFrameStorageError("storage unavailable")
    workflow, adapter, decision, context_frames, events, store, _ = _workflow(
        tmp_path,
        storage_failure=failure,
    )

    with pytest.raises(ContextFrameStorageError, match="storage unavailable"):
        workflow.run(_request("ready", ConstraintOperator.EQUALS, ("ready",)))

    assert decision.frames == []
    assert context_frames.persisted == []
    assert adapter.calls == 0
    assert len(events) == 1
    assert events[0][0] == "persistence-failed"
    run_events = store.read_stream(run_stream_id("run:1"))
    assert tuple(item.event_type for item in run_events) == (
        RUN_STARTED,
        TRACE_RECORDED,
        RUN_FAILED,
    )
    assert run_events[-1].payload["phase"] == "context-persistence"


def test_changed_context_returned_by_storage_prevents_reasoning_and_execution(
    tmp_path: Path,
) -> None:
    workflow, adapter, decision, context_frames, events, store, _ = _workflow(
        tmp_path,
        return_different=True,
    )

    with pytest.raises(ContextFrameIntegrityError, match="returned content different"):
        workflow.run(_request("ready", ConstraintOperator.EQUALS, ("ready",)))

    assert decision.frames == []
    assert len(context_frames.persisted) == 1
    assert adapter.calls == 0
    assert events == [("persisted", context_frames.persisted[0])]
    assert store.read_stream(run_stream_id("run:1"))[-1].payload["phase"] == ("context-integrity")


def test_daily_operator_composes_with_the_kernel_artifact_store(tmp_path: Path) -> None:
    database_path = tmp_path / "kernel.sqlite3"
    artifact_root = tmp_path / "artifacts"
    event_store = EventStore(database_path)
    artifact_store = ArtifactStore(artifact_root, database_path=database_path)
    adapter = Adapter()
    events: list[tuple[str, ContextFrame]] = []

    with ArtifactContextFrameStore(
        artifact_root,
        database_path=database_path,
    ) as context_frames:

        class PersistedDecision(Decision):
            def propose(self, frame: ContextFrame) -> ActionProposal:
                assert context_frames.get(frame.frame_id) == frame
                return super().propose(frame)

        decision = PersistedDecision(events)
        workflow = DailyOperatorWorkflow(
            event_store,
            IngestObservationHandler(event_store, clock=lambda: NOW),
            context_frames,
            KernelRunRecorder(event_store, artifact_store, clock=lambda: NOW),
            decision,
            AffordanceExecutionHandler(
                {"fixture": adapter},
                SQLiteExecutionJournal(artifact_root, database_path=database_path),
                clock=lambda: NOW,
            ),
        )

        result = workflow.run(_request("ready", ConstraintOperator.EQUALS, ("ready",)))
        assert context_frames.get(result.context_frame.frame_id) == result.context_frame

    with ArtifactContextFrameStore(
        artifact_root,
        database_path=database_path,
    ) as reopened:
        assert reopened.get(result.context_frame.frame_id) == result.context_frame

    assert decision.frames == [result.context_frame]
    assert adapter.calls == 1


def test_duplicate_terminal_run_touches_no_live_dependency(tmp_path: Path) -> None:
    workflow, adapter, decision, context_frames, _, store, _ = _workflow(tmp_path)
    request = _request("ready", ConstraintOperator.EQUALS, ("ready",))
    workflow.run(request)
    baseline = (
        len(store),
        adapter.calls,
        len(decision.frames),
        len(context_frames.persisted),
    )

    with pytest.raises(RunAlreadyExists):
        workflow.run(request)

    assert (
        len(store),
        adapter.calls,
        len(decision.frames),
        len(context_frames.persisted),
    ) == baseline
    assert store.read_stream(run_stream_id(request.run_id))[-1].event_type == RUN_COMPLETED


def test_duplicate_nonterminal_run_touches_no_live_dependency(tmp_path: Path) -> None:
    workflow, adapter, decision, context_frames, _, store, artifacts = _workflow(tmp_path)
    request = _request("ready", ConstraintOperator.EQUALS, ("ready",))
    KernelRunRecorder(store, artifacts, clock=lambda: NOW).start(
        RunStart(
            request.run_id,
            daily_operator_request_digest(request),
            request.ingestion.actor,
            request.context.task_id,
            request.context.objective,
            request.ingestion.domain,
            request.ingestion.stream_id,
        )
    )

    with pytest.raises(RunInterrupted):
        workflow.run(request)

    assert adapter.calls == 0
    assert decision.frames == []
    assert context_frames.persisted == []
    assert tuple(item.event_type for item in store.read_stream(run_stream_id(request.run_id))) == (
        RUN_STARTED,
    )


def test_request_preflight_rejects_correlation_and_input_causation_mismatch() -> None:
    request = _request("ready", ConstraintOperator.EQUALS, ("ready",))

    with pytest.raises(ValueError, match="correlation_id"):
        replace(
            request,
            ingestion=replace(request.ingestion, correlation_id="run:different"),
        )
    with pytest.raises(ValueError, match="owns ingestion causation"):
        replace(
            request,
            ingestion=replace(request.ingestion, causation_id="event:parent"),
        )


def test_post_start_failure_is_recorded_and_original_exception_is_preserved(
    tmp_path: Path,
) -> None:
    primary = RuntimeError("decision failed")
    workflow, adapter, _, _, _, store, _ = _workflow(
        tmp_path,
        decision_failure=primary,
    )

    with pytest.raises(RuntimeError, match="decision failed") as caught:
        workflow.run(_request("ready", ConstraintOperator.EQUALS, ("ready",)))

    assert caught.value is primary
    assert adapter.calls == 0
    run_events = store.read_stream(run_stream_id("run:1"))
    assert tuple(item.event_type for item in run_events) == (
        RUN_STARTED,
        CONTEXT_RECORDED,
        TRACE_RECORDED,
        RUN_FAILED,
    )
    assert run_events[-1].payload["phase"] == "decision"
    assert run_events[-1].payload["error_type"] == "RuntimeError"


def test_primary_and_failure_recording_errors_are_both_preserved(tmp_path: Path) -> None:
    primary = RuntimeError("decision failed")
    recording = OSError("run ledger unavailable")
    workflow, adapter, _, _, _, store, _ = _workflow(
        tmp_path,
        decision_failure=primary,
        run_failure=recording,
    )

    with pytest.raises(ExceptionGroup) as caught:
        workflow.run(_request("ready", ConstraintOperator.EQUALS, ("ready",)))

    assert caught.value.exceptions == (primary, recording)
    assert caught.value.__cause__ is primary
    assert adapter.calls == 0
    assert tuple(item.event_type for item in store.read_stream(run_stream_id("run:1"))) == (
        RUN_STARTED,
        CONTEXT_RECORDED,
    )


def _workflow(
    tmp_path: Path,
    *,
    storage_failure: ContextFrameStorageError | None = None,
    return_different: bool = False,
    decision_failure: Exception | None = None,
    run_failure: Exception | None = None,
    execution_success: bool = True,
    execution_uncertain: bool = False,
):
    store = EventStore(tmp_path / "kernel.sqlite3")
    artifact_root = tmp_path / "artifacts"
    artifacts = ArtifactStore(artifact_root, database_path=store.path)
    adapter = Adapter(success=execution_success, uncertain=execution_uncertain)
    events: list[tuple[str, ContextFrame]] = []
    decision = Decision(events, failure=decision_failure)
    context_frames = ContextFrames(
        ArtifactContextFrameStore(artifact_root, database_path=store.path),
        events,
        failure=storage_failure,
        return_different=return_different,
    )
    durable_runs = KernelRunRecorder(store, artifacts, clock=lambda: NOW)
    runs = (
        cast("RunRecorder", FailingRuns(durable_runs, run_failure))
        if run_failure is not None
        else durable_runs
    )
    workflow = DailyOperatorWorkflow(
        store,
        IngestObservationHandler(store, clock=lambda: NOW),
        context_frames,
        runs,
        decision,
        AffordanceExecutionHandler(
            {"fixture": adapter},
            SQLiteExecutionJournal(
                artifact_root,
                database_path=store.path,
            ),
            clock=lambda: NOW,
        ),
    )
    return workflow, adapter, decision, context_frames, events, store, artifacts


def _request(
    value: str,
    operator: ConstraintOperator,
    expected: tuple[str, ...],
) -> DailyOperatorRequest:
    observation = ObservationInput(
        "obs:1",
        NOW,
        (ObservedClaim("claim:1", "project:blackcell", "status", value, 0.9),),
        (EvidencePointer(locator="fixture://status"),),
    )
    ingestion = IngestObservation(
        "observations:daily", 0, "operator", "fixture", "run:1", (observation,)
    )
    constraint = ConstraintDefinition(
        "status-policy",
        "project status must satisfy policy",
        "project:blackcell",
        "status",
        operator,
        expected,
    )
    return DailyOperatorRequest(
        "run:1",
        ingestion,
        DeriveSignalPacket("daily", NOW),
        RetrieveEvidence(
            "inspect project status",
            required_keys=(EvidenceKey("project:blackcell", "status"),),
        ),
        BuildContext("task:daily", "inspect project status", NOW),
        SolveConstraints(NOW, (constraint,)),
        AffordancePolicy("inspect", True, allowed_arguments=("path",)),
        AffordanceDefinition(
            "inspect",
            "fixture",
            SideEffectClass.READ_ONLY,
            10.0,
            (AffordanceArgumentSpec("path"),),
        ),
        "invocation:1",
        "daily:1",
    )
