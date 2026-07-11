from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
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
        decision: DecisionPort,
        execution: AffordanceExecutionHandler,
    ) -> None:
        self._events = event_reader
        self._ingestion = ingestion
        self._context_frames = context_frames
        self._decision = decision
        self._execution = execution
        self._state = OperationalStateProjector()
        self._signals = SignalPacketProjector()
        self._retrieval = DeterministicEvidenceRetriever()
        self._contexts = ContextFrameBuilder()
        self._constraints = DeterministicConstraintSolver()
        self._authorization = ActionAuthorizer()

    def run(self, request: DailyOperatorRequest) -> DailyOperatorResult:
        observations = self._ingestion.handle(request.ingestion)
        state = self._state.replay(
            tuple(self._events.read_all()),
            scope=OperationalStateScope(
                request.ingestion.domain,
                request.ingestion.stream_id,
            ),
        )
        signal = self._signals.handle(request.signal, state)
        selection = self._retrieval.handle(request.retrieval, signal)
        built_frame = self._contexts.handle(request.context, selection)
        frame = self._context_frames.put(built_frame)
        if frame != built_frame:
            raise ContextFrameIntegrityError(
                "ContextFrame storage returned content different from the frame it was given"
            )
        proposal = self._decision.propose(frame)
        if proposal.context_frame_id != frame.frame_id:
            raise ValueError("decision proposal belongs to a different ContextFrame")
        constraints = self._constraints.handle(request.constraints, frame)
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
            execution = self._execution.handle(
                invocation,
                request.execution_affordance,
                authorization,
                run_id=request.run_id,
            )
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
