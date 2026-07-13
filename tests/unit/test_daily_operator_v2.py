from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from blackcell.adapters.persistence.sqlite import (
    SQLiteDecisionAttemptJournal,
    SQLiteExecutionJournal,
)
from blackcell.adapters.persistence.sqlite.run_records_v2 import KernelFeedbackRunRecorder
from blackcell.features.authorize_action import AffordancePolicy
from blackcell.features.build_context import BuildContext
from blackcell.features.derive_signal_packet import DeriveSignalPacket
from blackcell.features.evaluate_outcome import (
    EvaluationCriterion,
    EvaluationSpec,
    OutcomeEvaluator,
)
from blackcell.features.execute_affordance import (
    AdapterOutcome,
    AffordanceDefinition,
    AffordanceExecutionHandler,
    SideEffectClass,
    UncertainExecutionError,
)
from blackcell.features.execute_affordance.ports import ExecutionJournal
from blackcell.features.ingest_observation import (
    EvidencePointer,
    IngestObservation,
    IngestObservationHandler,
    ObservationInput,
    ObservedClaim,
)
from blackcell.features.observe_outcome import (
    OutcomeClaim,
    OutcomeEvidencePointer,
    OutcomeObservation,
    OutcomeObservationStatus,
    OutcomeTarget,
)
from blackcell.features.project_operational_state import ProjectOperationalStateHandler
from blackcell.features.request_decision import (
    DecisionAdapterResult,
    DecisionBudget,
    DecisionCapability,
    DecisionClassification,
    DecisionFailureKind,
    DecisionGatewayError,
    DecisionLocality,
    DecisionRequirements,
    DecisionRoute,
    RequestDecision,
    RequestDecisionHandler,
)
from blackcell.features.retrieve_evidence import EvidenceKey, RetrieveEvidence
from blackcell.features.solve_constraints import (
    ConstraintDefinition,
    ConstraintOperator,
    SolveConstraints,
)
from blackcell.kernel import (
    ArtifactStore,
    EventStore,
    JsonValue,
    ProjectionCheckpoint,
    utc_now,
)
from blackcell.workflows import DailyOperatorV2Request, DailyOperatorV2Workflow
from blackcell.workflows.outcome_evidence import OutcomeEvidenceWriter
from blackcell.workflows.run_grammar import validate_run_grammar
from blackcell.workflows.run_protocol import (
    EVALUATION_RECORDED,
    EXECUTION_RECORDED,
    MODEL_FAILED,
    MODEL_RESPONDED,
    OUTCOME_OBSERVED,
    OUTCOME_STATE_RECORDED,
    RUN_COMPLETED,
    RUN_FAILED,
    STATE_TRANSITION_RECORDED,
    RunAlreadyExists,
    RunInterrupted,
    RunOutcome,
    run_stream_id,
)
from blackcell.workflows.state_transition import bind_and_accept_state_transition

BASE = datetime(2026, 7, 1, 12, tzinfo=UTC)
RUN_ID = "run:daily:v2"
DOMAIN = "repository"
STREAM_ID = "repository:blackcell"
ACTOR = "operator"
ADAPTER_ID = "adapter:fixture"
OBSERVER_ID = "observer:fixture"
OBSERVER_VERSION = "observer-fixture/v1"


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
        del expected_position
        return checkpoint


class Gateway:
    def __init__(self, *, fail_route: bool = False, fail_invoke: bool = False) -> None:
        self.fail_route = fail_route
        self.fail_invoke = fail_invoke
        self.route_calls = 0
        self.invoke_calls = 0

    def route(self, request: RequestDecision) -> DecisionRoute:
        self.route_calls += 1
        if self.fail_route:
            raise DecisionGatewayError(
                DecisionFailureKind.ADMISSION,
                "fixture-route-rejected",
            )
        return DecisionRoute(
            "reason-local",
            "fixture-model-adapter",
            "fixture-model",
            request.capability,
            True,
            True,
            utc_now(),
        )

    def invoke(
        self,
        request: RequestDecision,
        route: DecisionRoute,
    ) -> DecisionAdapterResult:
        del route
        self.invoke_calls += 1
        if self.fail_invoke:
            raise DecisionGatewayError(
                DecisionFailureKind.ADAPTER,
                "fixture-invoke-failed",
            )
        output: dict[str, JsonValue] = {
            "proposal_id": "proposal:daily:v2",
            "context_frame_id": request.context_frame_id,
            "affordance": request.affordances[0].name,
            "arguments": (),
            "rationale": "fixture decision",
            "evidence_event_ids": request.evidence_event_ids,
        }
        return DecisionAdapterResult(output, 12, 4, 10, 2, True, utc_now())


class ExecutionAdapter:
    adapter_id = ADAPTER_ID
    contract_version = "fixture-execution/v1"

    def __init__(self, *, succeeds: bool = True, unknown: bool = False) -> None:
        self.succeeds = succeeds
        self.unknown = unknown
        self.execute_calls = 0
        self.reconcile_calls = 0

    def execute(self, invocation, definition) -> AdapterOutcome:
        del invocation, definition
        self.execute_calls += 1
        if self.unknown:
            raise UncertainExecutionError("fixture outcome unknown")
        return AdapterOutcome(
            self.succeeds,
            "fixture-output",
            utc_now(),
            error_code=None if self.succeeds else "fixture-failed",
        )

    def reconcile(self, invocation, definition, previous) -> AdapterOutcome:
        del invocation, definition, previous
        self.reconcile_calls += 1
        raise AssertionError("fresh workflow must not reconcile")


class Observer:
    observer_id = OBSERVER_ID
    contract_version = OBSERVER_VERSION

    def __init__(
        self,
        *,
        value: bool = True,
        status: OutcomeObservationStatus = OutcomeObservationStatus.OBSERVED,
        observer_id: str = OBSERVER_ID,
    ) -> None:
        self.value = value
        self.status = status
        self.observer_id = observer_id
        self.calls = 0
        self.seen_targets: tuple[OutcomeTarget, ...] = ()

    def observe(self, command) -> OutcomeObservation:
        self.calls += 1
        self.seen_targets = command.targets
        claims = (
            ()
            if self.status is OutcomeObservationStatus.INCONCLUSIVE
            else (
                OutcomeClaim(
                    "claim:outcome",
                    command.targets[0].subject,
                    command.targets[0].predicate,
                    self.value,
                ),
            )
        )
        return OutcomeObservation(
            "observation:outcome",
            command.binding,
            command.evaluation_spec_id,
            command.domain,
            command.stream_id,
            self.observer_id,
            self.contract_version,
            self.status,
            utc_now(),
            claims,
            (OutcomeEvidencePointer(locator="fixture://outcome"),),
        )


@dataclass
class WorkflowFixture:
    events: EventStore
    artifacts: ArtifactStore
    decision_journal: SQLiteDecisionAttemptJournal
    execution_journal: SQLiteExecutionJournal
    recorder: KernelFeedbackRunRecorder
    gateway: Gateway
    adapter: ExecutionAdapter
    observer: Observer
    workflow: DailyOperatorV2Workflow

    @classmethod
    def create(
        cls,
        tmp_path: Path,
        *,
        gateway: Gateway | None = None,
        adapter: ExecutionAdapter | None = None,
        observer: Observer | None = None,
        hide_execution_entry: bool = False,
    ) -> WorkflowFixture:
        database = tmp_path / "kernel.sqlite3"
        artifact_root = tmp_path / "artifacts"
        events = EventStore(database)
        artifacts = ArtifactStore(artifact_root, database_path=database)
        decision_journal = SQLiteDecisionAttemptJournal(
            artifact_root,
            database_path=database,
        )
        execution_journal = SQLiteExecutionJournal(
            artifact_root,
            database_path=database,
        )
        selected_gateway = gateway or Gateway()
        selected_adapter = adapter or ExecutionAdapter()
        selected_observer = observer or Observer()
        recorder = KernelFeedbackRunRecorder(
            events,
            artifacts,
            decision_journal,
            execution_journal,
        )
        workflow = DailyOperatorV2Workflow(
            history=events,
            artifacts=artifacts,
            state=ProjectOperationalStateHandler(events, NoCheckpoints()),
            ingestion=IngestObservationHandler(events),
            runs=recorder,
            decisions=RequestDecisionHandler(selected_gateway, decision_journal),
            execution=AffordanceExecutionHandler(
                {selected_adapter.adapter_id: selected_adapter},
                execution_journal,
            ),
            execution_journal=(
                cast(ExecutionJournal, MissingEntryJournal())
                if hide_execution_entry
                else execution_journal
            ),
            outcome_observer=selected_observer,
            outcome_evidence=OutcomeEvidenceWriter(events),
            evaluator=OutcomeEvaluator(),
        )
        return cls(
            events,
            artifacts,
            decision_journal,
            execution_journal,
            recorder,
            selected_gateway,
            selected_adapter,
            selected_observer,
            workflow,
        )

    def event_types(self) -> tuple[str, ...]:
        return tuple(item.event_type for item in self.events.read_stream(run_stream_id(RUN_ID)))


def test_successful_v2_workflow_records_and_rebinds_transition(tmp_path: Path) -> None:
    fixture = WorkflowFixture.create(tmp_path)

    terminal = fixture.workflow.run(_request())

    assert terminal.terminal_event.event_type == RUN_COMPLETED
    assert terminal.terminal_event.payload["outcome"] == RunOutcome.EXECUTED.value
    assert fixture.gateway.invoke_calls == 1
    assert fixture.adapter.execute_calls == 1
    assert fixture.observer.calls == 1
    assert tuple((item.subject, item.predicate) for item in fixture.observer.seen_targets) == (
        ("project:blackcell", "completed"),
    )
    assert not hasattr(fixture.observer.seen_targets[0], "expected_value")
    assert STATE_TRANSITION_RECORDED in fixture.event_types()
    events = fixture.events.read_stream(run_stream_id(RUN_ID))
    assert validate_run_grammar(events, run_id=RUN_ID).terminal
    acceptance = bind_and_accept_state_transition(RUN_ID, fixture.events, fixture.artifacts)
    assert acceptance.transition is not None


@pytest.mark.parametrize(
    ("observer", "transition_expected"),
    [
        (Observer(value=False), True),
        (Observer(status=OutcomeObservationStatus.INCONCLUSIVE), False),
    ],
)
def test_definitive_failure_or_inconclusive_evidence_controls_transition(
    tmp_path: Path,
    observer: Observer,
    transition_expected: bool,
) -> None:
    fixture = WorkflowFixture.create(tmp_path, observer=observer)

    terminal = fixture.workflow.run(_request())

    assert terminal.terminal_event.payload["outcome"] == RunOutcome.EXECUTED.value
    assert (STATE_TRANSITION_RECORDED in fixture.event_types()) is transition_expected
    assert OUTCOME_OBSERVED in fixture.event_types()
    assert OUTCOME_STATE_RECORDED in fixture.event_types()
    assert EVALUATION_RECORDED in fixture.event_types()


@pytest.mark.parametrize(
    ("initial_status", "side_effect_class", "expected"),
    [
        ("blocked", SideEffectClass.READ_ONLY, RunOutcome.DENIED),
        (
            "ready",
            SideEffectClass.REVERSIBLE,
            RunOutcome.APPROVAL_REQUIRED,
        ),
    ],
)
def test_blocked_authorization_skips_execution_and_observation(
    tmp_path: Path,
    initial_status: str,
    side_effect_class: SideEffectClass,
    expected: RunOutcome,
) -> None:
    fixture = WorkflowFixture.create(tmp_path)

    terminal = fixture.workflow.run(
        _request(
            initial_status=initial_status,
            side_effect_class=side_effect_class,
        )
    )

    assert terminal.terminal_event.payload["outcome"] == expected.value
    assert fixture.adapter.execute_calls == 0
    assert fixture.observer.calls == 0
    assert EXECUTION_RECORDED not in fixture.event_types()
    assert OUTCOME_OBSERVED not in fixture.event_types()
    assert STATE_TRANSITION_RECORDED not in fixture.event_types()


@pytest.mark.parametrize(
    ("adapter", "expected", "transition_expected"),
    [
        (ExecutionAdapter(succeeds=False), RunOutcome.EXECUTION_FAILED, True),
        (ExecutionAdapter(unknown=True), RunOutcome.REQUIRES_RECONCILIATION, False),
    ],
)
def test_execution_status_controls_observation_and_transition(
    tmp_path: Path,
    adapter: ExecutionAdapter,
    expected: RunOutcome,
    transition_expected: bool,
) -> None:
    fixture = WorkflowFixture.create(tmp_path, adapter=adapter)

    terminal = fixture.workflow.run(_request())

    assert terminal.terminal_event.payload["outcome"] == expected.value
    assert (STATE_TRANSITION_RECORDED in fixture.event_types()) is transition_expected
    if expected is RunOutcome.REQUIRES_RECONCILIATION:
        assert fixture.observer.calls == 0
        assert OUTCOME_OBSERVED not in fixture.event_types()
    else:
        assert fixture.observer.calls == 1


@pytest.mark.parametrize("gateway", [Gateway(fail_route=True), Gateway(fail_invoke=True)])
def test_gateway_failure_is_durable_and_never_executes(
    tmp_path: Path,
    gateway: Gateway,
) -> None:
    fixture = WorkflowFixture.create(tmp_path, gateway=gateway)

    terminal = fixture.workflow.run(_request())

    assert terminal.terminal_event.event_type == RUN_FAILED
    assert MODEL_FAILED in fixture.event_types()
    assert MODEL_RESPONDED not in fixture.event_types()
    assert fixture.adapter.execute_calls == 0
    assert fixture.observer.calls == 0


def test_duplicate_terminal_delivery_does_not_repeat_live_work(tmp_path: Path) -> None:
    fixture = WorkflowFixture.create(tmp_path)
    request = _request()
    fixture.workflow.run(request)
    calls = (
        fixture.gateway.invoke_calls,
        fixture.adapter.execute_calls,
        fixture.observer.calls,
    )

    with pytest.raises(RunAlreadyExists):
        fixture.workflow.run(request)

    assert calls == (
        fixture.gateway.invoke_calls,
        fixture.adapter.execute_calls,
        fixture.observer.calls,
    )


def test_interrupted_prefix_does_not_enter_live_ports(tmp_path: Path) -> None:
    fixture = WorkflowFixture.create(tmp_path)
    request = _request()
    fixture.recorder.open(request)

    with pytest.raises(RunInterrupted):
        fixture.workflow.run(request)

    assert fixture.gateway.invoke_calls == 0
    assert fixture.adapter.execute_calls == 0
    assert fixture.observer.calls == 0


def test_observer_policy_mismatch_fails_before_run_creation(tmp_path: Path) -> None:
    fixture = WorkflowFixture.create(
        tmp_path,
        observer=Observer(observer_id="observer:wrong"),
    )

    with pytest.raises(ValueError, match="observer differs"):
        fixture.workflow.run(_request())

    assert fixture.events.read_stream(run_stream_id(RUN_ID)) == ()
    assert fixture.gateway.route_calls == 0
    assert fixture.adapter.execute_calls == 0
    assert fixture.observer.calls == 0


def test_unrecorded_terminal_execution_remains_nonterminal(tmp_path: Path) -> None:
    fixture = WorkflowFixture.create(tmp_path, hide_execution_entry=True)

    with pytest.raises(ExceptionGroup, match="durable failure recording"):
        fixture.workflow.run(_request())

    assert fixture.adapter.execute_calls == 1
    assert fixture.observer.calls == 0
    assert EXECUTION_RECORDED not in fixture.event_types()
    assert RUN_FAILED not in fixture.event_types()
    assert not validate_run_grammar(
        fixture.events.read_stream(run_stream_id(RUN_ID)),
        run_id=RUN_ID,
    ).terminal


class MissingEntryJournal:
    def get_entry_by_invocation(self, invocation_id: str):
        del invocation_id
        return None


def _request(
    *,
    initial_status: str = "ready",
    side_effect_class: SideEffectClass = SideEffectClass.READ_ONLY,
) -> DailyOperatorV2Request:
    objective = "inspect project status"
    read_only = side_effect_class is SideEffectClass.READ_ONLY
    return DailyOperatorV2Request(
        run_id=RUN_ID,
        ingestion=IngestObservation(
            STREAM_ID,
            0,
            ACTOR,
            "fixture.initial",
            RUN_ID,
            (
                ObservationInput(
                    "observation:initial",
                    BASE,
                    (
                        ObservedClaim(
                            "claim:status",
                            "project:blackcell",
                            "status",
                            initial_status,
                        ),
                    ),
                    (EvidencePointer(locator="fixture://initial"),),
                    "initial:1",
                ),
            ),
            None,
            DOMAIN,
        ),
        initial_effective_time_cutoff=BASE,
        signal=DeriveSignalPacket("daily", BASE, 3_600),
        retrieval=RetrieveEvidence(
            objective,
            (EvidenceKey("project:blackcell", "status"),),
            4,
        ),
        context=BuildContext("task:daily", objective, BASE, 4_000),
        constraints=SolveConstraints(
            BASE,
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
            "decision:daily:v2",
            "node:planner",
            DecisionCapability.REASON,
            DecisionClassification.PRIVATE,
            DecisionLocality.LOCAL_ONLY,
            DecisionBudget(512, 128, 1_000, 100),
            64,
            True,
            BASE,
        ),
        authorization_affordance=AffordancePolicy("inspect", read_only),
        execution_affordance=AffordanceDefinition(
            "inspect",
            ADAPTER_ID,
            side_effect_class,
            10,
        ),
        invocation_id="invocation:daily:v2",
        idempotency_key="execution:daily:v2",
        expected_observer_id=OBSERVER_ID,
        expected_observer_contract_version=OBSERVER_VERSION,
    )
