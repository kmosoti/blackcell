from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from blackcell.adapters.persistence.sqlite import (
    ArtifactContextFrameStore,
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
from blackcell.kernel import EventStore
from blackcell.workflows import DailyOperatorRequest, DailyOperatorWorkflow

NOW = datetime(2026, 7, 10, 22, tzinfo=UTC)


class Decision:
    def __init__(self, events: list[tuple[str, ContextFrame]]) -> None:
        self.frames: list[ContextFrame] = []
        self.events = events

    def propose(self, frame: ContextFrame) -> ActionProposal:
        self.frames.append(frame)
        self.events.append(("decision", frame))
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
        events: list[tuple[str, ContextFrame]],
        *,
        failure: ContextFrameStorageError | None = None,
        return_different: bool = False,
    ) -> None:
        self.events = events
        self.failure = failure
        self.return_different = return_different
        self.frames: dict[str, ContextFrame] = {}
        self.persisted: list[ContextFrame] = []

    def put(self, frame: ContextFrame) -> ContextFrame:
        if self.failure is not None:
            self.events.append(("persistence-failed", frame))
            raise self.failure
        stored = (
            replace(frame, task_id="task:different") if self.return_different else replace(frame)
        )
        self.frames[stored.frame_id] = stored
        self.persisted.append(stored)
        self.events.append(("persisted", stored))
        return stored

    def get(self, frame_id: str) -> ContextFrame | None:
        return self.frames.get(frame_id)

    def list_frames(self) -> tuple[ContextFrame, ...]:
        return tuple(
            sorted(
                self.frames.values(),
                key=lambda frame: (frame.generated_at, frame.frame_id),
            )
        )


class Adapter:
    adapter_id = "fixture"
    contract_version = "fixture/v1"

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, invocation, definition):
        self.calls += 1
        return AdapterOutcome(True, "sha256:output", NOW)

    def reconcile(self, invocation, definition, previous):
        raise AssertionError("read-only fixture should not reconcile")


def test_daily_operator_runs_the_complete_allowed_control_loop(tmp_path: Path) -> None:
    workflow, adapter, decision, context_frames, events = _workflow(tmp_path)

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


def test_symbolic_violation_stops_daily_operator_before_execution(tmp_path: Path) -> None:
    workflow, adapter, _, _, _ = _workflow(tmp_path)

    result = workflow.run(_request("blocked", ConstraintOperator.NOT_EQUALS, ("blocked",)))

    assert result.authorization.outcome is AuthorizationOutcome.DENY
    assert result.execution is None
    assert adapter.calls == 0


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
    workflow, _, _, _, _ = _workflow(tmp_path)

    result = workflow.run(_request("ready", ConstraintOperator.EQUALS, ("ready",)))

    assert result.state.scope.domain == "repository"
    assert result.state.scope.stream_id == "observations:daily"
    assert tuple(claim.value for claim in result.state.claims) == ("ready",)
    assert result.state.cutoff_global_position == 2
    assert result.state.last_source_stream_sequence == 1


def test_context_persistence_failure_prevents_reasoning_and_execution(tmp_path: Path) -> None:
    failure = ContextFrameStorageError("storage unavailable")
    workflow, adapter, decision, context_frames, events = _workflow(
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


def test_changed_context_returned_by_storage_prevents_reasoning_and_execution(
    tmp_path: Path,
) -> None:
    workflow, adapter, decision, context_frames, events = _workflow(
        tmp_path,
        return_different=True,
    )

    with pytest.raises(ContextFrameIntegrityError, match="returned content different"):
        workflow.run(_request("ready", ConstraintOperator.EQUALS, ("ready",)))

    assert decision.frames == []
    assert len(context_frames.persisted) == 1
    assert adapter.calls == 0
    assert events == [("persisted", context_frames.persisted[0])]


def test_daily_operator_composes_with_the_kernel_artifact_store(tmp_path: Path) -> None:
    database_path = tmp_path / "kernel.sqlite3"
    artifact_root = tmp_path / "artifacts"
    event_store = EventStore(database_path)
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


def _workflow(
    tmp_path: Path,
    *,
    storage_failure: ContextFrameStorageError | None = None,
    return_different: bool = False,
):
    store = EventStore(tmp_path / "kernel.sqlite3")
    adapter = Adapter()
    events: list[tuple[str, ContextFrame]] = []
    decision = Decision(events)
    context_frames = ContextFrames(
        events,
        failure=storage_failure,
        return_different=return_different,
    )
    workflow = DailyOperatorWorkflow(
        store,
        IngestObservationHandler(store, clock=lambda: NOW),
        context_frames,
        decision,
        AffordanceExecutionHandler(
            {"fixture": adapter},
            SQLiteExecutionJournal(
                tmp_path / "artifacts",
                database_path=store.path,
            ),
            clock=lambda: NOW,
        ),
    )
    return workflow, adapter, decision, context_frames, events


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
