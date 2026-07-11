from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from blackcell.features.authorize_action import (
    ActionAuthorizer,
    ActionProposal,
    AffordancePolicy,
    AuthorizationDecision,
    AuthorizationOutcome,
    AuthorizeAction,
)
from blackcell.features.build_context import (
    BuildContext,
    ContextFrame,
    ContextFrameBuilder,
    ContextFrameIntegrityError,
    ContextFrameStorage,
)
from blackcell.features.derive_signal_packet import (
    DeriveSignalPacket,
    SignalPacket,
    SignalPacketProjector,
)
from blackcell.features.execute_affordance import (
    AffordanceArgument,
    AffordanceDefinition,
    AffordanceExecutionHandler,
    AffordanceInvocation,
    ExecutionResult,
    ExecutionStatus,
    SideEffectClass,
)
from blackcell.features.ingest_observation import IngestObservation, IngestObservationHandler
from blackcell.features.project_operational_state import (
    OperationalBeliefState,
    OperationalStateProjector,
    OperationalStateScope,
)
from blackcell.features.retrieve_evidence import (
    DeterministicEvidenceRetriever,
    EvidenceSelection,
    RetrieveEvidence,
)
from blackcell.features.solve_constraints import (
    ConstraintEvaluation,
    DeterministicConstraintSolver,
    SolveConstraints,
)
from blackcell.kernel import EventEnvelope
from blackcell.workflows.daily_operator_identity import daily_operator_request_digest
from blackcell.workflows.run_protocol import RunOutcome, RunRecorder, RunStart


class EventReader(Protocol):
    def read_all(
        self,
        *,
        after_position: int = 0,
        limit: int | None = None,
    ) -> Sequence[EventEnvelope]: ...


class DecisionPort(Protocol):
    def propose(self, frame: ContextFrame) -> ActionProposal: ...


@dataclass(frozen=True, slots=True)
class DailyOperatorRequest:
    run_id: str
    ingestion: IngestObservation
    signal: DeriveSignalPacket
    retrieval: RetrieveEvidence
    context: BuildContext
    constraints: SolveConstraints
    authorization_affordance: AffordancePolicy
    execution_affordance: AffordanceDefinition
    invocation_id: str
    idempotency_key: str
    approval_granted: bool = False

    def __post_init__(self) -> None:
        for name in ("run_id", "invocation_id", "idempotency_key"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.retrieval.objective != self.context.objective:
            raise ValueError("retrieval and context objectives must match")
        if self.ingestion.correlation_id != self.run_id:
            raise ValueError("ingestion correlation_id must match run_id")
        if self.ingestion.causation_id is not None:
            raise ValueError("Daily Operator owns ingestion causation")
        if self.authorization_affordance.name != self.execution_affordance.name:
            raise ValueError("authorization and execution affordances must match")
        execution_is_read_only = (
            self.execution_affordance.side_effect_class is SideEffectClass.READ_ONLY
        )
        if self.authorization_affordance.read_only != execution_is_read_only:
            raise ValueError("authorization and execution side-effect classes must agree")


@dataclass(frozen=True, slots=True)
class DailyOperatorResult:
    run_id: str
    observations: tuple[EventEnvelope, ...]
    state: OperationalBeliefState
    signal_packet: SignalPacket
    evidence_selection: EvidenceSelection
    context_frame: ContextFrame
    proposal: ActionProposal
    constraint_evaluation: ConstraintEvaluation
    authorization: AuthorizationDecision
    execution: ExecutionResult | None


class DailyOperatorWorkflow:
    def __init__(
        self,
        event_reader: EventReader,
        ingestion: IngestObservationHandler,
        context_frames: ContextFrameStorage,
        runs: RunRecorder,
        decision: DecisionPort,
        execution: AffordanceExecutionHandler,
    ) -> None:
        self._events = event_reader
        self._ingestion = ingestion
        self._context_frames = context_frames
        self._runs = runs
        self._decision = decision
        self._execution = execution
        self._state = OperationalStateProjector()
        self._signals = SignalPacketProjector()
        self._retrieval = DeterministicEvidenceRetriever()
        self._contexts = ContextFrameBuilder()
        self._constraints = DeterministicConstraintSolver()
        self._authorization = ActionAuthorizer()

    def run(self, request: DailyOperatorRequest) -> DailyOperatorResult:
        started = self._runs.start(
            RunStart(
                run_id=request.run_id,
                request_digest=daily_operator_request_digest(request),
                actor=request.ingestion.actor,
                task_id=request.context.task_id,
                objective=request.context.objective,
                domain=request.ingestion.domain,
                observation_stream_id=request.ingestion.stream_id,
            )
        )
        phase = "ingestion"
        try:
            observations = self._ingestion.handle(
                replace(request.ingestion, causation_id=started.event_id)
            )
            phase = "state-projection"
            state = self._state.replay(
                tuple(self._events.read_all()),
                scope=OperationalStateScope(
                    request.ingestion.domain,
                    request.ingestion.stream_id,
                ),
            )
            phase = "signal-projection"
            signal = self._signals.handle(request.signal, state)
            phase = "evidence-retrieval"
            selection = self._retrieval.handle(request.retrieval, signal)
            phase = "context-build"
            built_frame = self._contexts.handle(request.context, selection)
            phase = "context-persistence"
            frame = self._context_frames.put(built_frame)
            phase = "context-integrity"
            if frame != built_frame:
                raise ContextFrameIntegrityError(
                    "ContextFrame storage returned content different from the frame it was given"
                )
            phase = "context-recording"
            self._runs.record_context(request.run_id, frame)
            phase = "decision"
            proposal = self._decision.propose(frame)
            if proposal.context_frame_id != frame.frame_id:
                raise ValueError("decision proposal belongs to a different ContextFrame")
            phase = "proposal-recording"
            self._runs.record_proposal(request.run_id, proposal)
            phase = "constraint-evaluation"
            constraints = self._constraints.handle(request.constraints, frame)
            phase = "constraint-recording"
            self._runs.record_constraints(request.run_id, constraints)
            phase = "authorization"
            authorization = self._authorization.handle(
                AuthorizeAction(
                    proposal,
                    request.authorization_affordance,
                    request.constraints.evaluated_at,
                    frame.provenance_event_ids,
                    request.approval_granted,
                ),
                constraints,
            )
            phase = "authorization-recording"
            self._runs.record_authorization(request.run_id, authorization)
            execution = None
            if authorization.outcome is AuthorizationOutcome.ALLOW:
                invocation = AffordanceInvocation(
                    request.invocation_id,
                    proposal.proposal_id,
                    proposal.affordance,
                    tuple(AffordanceArgument(item.name, item.value) for item in proposal.arguments),
                    request.idempotency_key,
                    request.constraints.evaluated_at,
                )
                phase = "execution"
                execution = self._execution.handle(
                    invocation,
                    request.execution_affordance,
                    authorization,
                    run_id=request.run_id,
                )
                phase = "execution-recording"
                self._runs.record_execution(request.run_id, execution)
            outcome = _run_outcome(authorization.outcome, execution)
            phase = "completion"
            self._runs.complete(request.run_id, outcome)
            return DailyOperatorResult(
                request.run_id,
                observations,
                state,
                signal,
                selection,
                frame,
                proposal,
                constraints,
                authorization,
                execution,
            )
        except Exception as error:
            try:
                self._runs.fail(
                    request.run_id,
                    phase=phase,
                    error_type=type(error).__name__,
                )
            except Exception as recording_error:
                raise ExceptionGroup(
                    "Daily Operator failed and durable failure recording also failed",
                    (error, recording_error),
                ) from error
            raise


def _run_outcome(
    authorization: AuthorizationOutcome,
    execution: ExecutionResult | None,
) -> RunOutcome:
    if authorization is AuthorizationOutcome.DENY:
        if execution is not None:
            raise ValueError("denied authorization cannot have an execution result")
        return RunOutcome.DENIED
    if authorization is AuthorizationOutcome.REQUIRE_APPROVAL:
        if execution is not None:
            raise ValueError("approval-required authorization cannot have an execution result")
        return RunOutcome.APPROVAL_REQUIRED
    if execution is None:
        raise ValueError("allowed authorization requires an execution result")
    return {
        ExecutionStatus.SUCCEEDED: RunOutcome.EXECUTED,
        ExecutionStatus.FAILED: RunOutcome.EXECUTION_FAILED,
        ExecutionStatus.UNKNOWN: RunOutcome.REQUIRES_RECONCILIATION,
    }[execution.status]
