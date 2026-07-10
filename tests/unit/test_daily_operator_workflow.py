from datetime import UTC, datetime
from pathlib import Path

from blackcell.features.authorize_action import (
    ActionArgument,
    ActionProposal,
    AffordancePolicy,
    AuthorizationOutcome,
)
from blackcell.features.build_context import BuildContext
from blackcell.features.derive_signal_packet import DeriveSignalPacket
from blackcell.features.execute_affordance import (
    AdapterOutcome,
    AffordanceArgumentSpec,
    AffordanceDefinition,
    AffordanceExecutionHandler,
    ExecutionResult,
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
    def __init__(self) -> None:
        self.frames = []

    def propose(self, frame):
        self.frames.append(frame)
        return ActionProposal(
            "proposal:1",
            frame.frame_id,
            "inspect",
            (ActionArgument("path", "README.md"),),
            "inspect the cited repository evidence",
            frame.provenance_event_ids,
        )


class Adapter:
    adapter_id = "fixture"

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, invocation, definition):
        self.calls += 1
        return AdapterOutcome(True, "sha256:output", NOW)

    def reconcile(self, invocation, definition, previous):
        raise AssertionError("read-only fixture should not reconcile")


class Journal:
    def __init__(self) -> None:
        self.results: dict[str, ExecutionResult] = {}

    def get(self, idempotency_key: str):
        return self.results.get(idempotency_key)

    def save(self, result):
        self.results[result.idempotency_key] = result


def test_daily_operator_runs_the_complete_allowed_control_loop(tmp_path: Path) -> None:
    workflow, adapter, decision = _workflow(tmp_path)

    result = workflow.run(_request("ready", ConstraintOperator.EQUALS, ("ready",)))

    assert result.state.claims[0].value == "ready"
    assert result.signal_packet.provenance_event_ids == result.context_frame.provenance_event_ids
    assert result.constraint_evaluation.safe
    assert result.authorization.outcome is AuthorizationOutcome.ALLOW
    assert result.execution is not None
    assert result.execution.status is ExecutionStatus.SUCCEEDED
    assert adapter.calls == 1
    assert decision.frames == [result.context_frame]


def test_symbolic_violation_stops_daily_operator_before_execution(tmp_path: Path) -> None:
    workflow, adapter, _ = _workflow(tmp_path)

    result = workflow.run(_request("blocked", ConstraintOperator.NOT_EQUALS, ("blocked",)))

    assert result.authorization.outcome is AuthorizationOutcome.DENY
    assert result.execution is None
    assert adapter.calls == 0


def _workflow(tmp_path: Path):
    store = EventStore(tmp_path / "kernel.sqlite3")
    adapter = Adapter()
    decision = Decision()
    workflow = DailyOperatorWorkflow(
        store,
        IngestObservationHandler(store, clock=lambda: NOW),
        decision,
        AffordanceExecutionHandler({"fixture": adapter}, Journal()),
    )
    return workflow, adapter, decision


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
