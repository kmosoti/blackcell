from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from blackcell.features.accept_state_transition import AcceptedStateTransition
from blackcell.features.authorize_action import ActionProposal, AuthorizationDecision
from blackcell.features.build_context import ContextFrame
from blackcell.features.evaluate_outcome import OutcomeEvaluation
from blackcell.features.execute_affordance import ExecutionJournalEntry
from blackcell.features.observe_outcome import OutcomeObservation
from blackcell.features.project_operational_state import OperationalBeliefState
from blackcell.features.request_decision import (
    DecisionAttemptRecord,
    DecisionPreparation,
    DecisionRequestRecord,
    DecisionTerminalRecord,
)
from blackcell.features.solve_constraints import ConstraintEvaluation
from blackcell.kernel import EventEnvelope
from blackcell.workflows.daily_operator_v2_request import DailyOperatorV2Request
from blackcell.workflows.run_protocol import RunOutcome, RunTerminal


@dataclass(frozen=True, slots=True)
class FeedbackRunOpening:
    """The mandatory, atomically committed opening of a v2 run."""

    started: EventEnvelope
    evaluation_specified: EventEnvelope


class FeedbackRunRecorder(Protocol):
    """Durable writer for the complete ``daily-operator/v2`` feedback run.

    Implementations own event payload construction. Callers supply feature-owned values,
    never untyped event mappings or caller-assembled artifact links.
    """

    def open(self, request: DailyOperatorV2Request) -> FeedbackRunOpening: ...

    def record_initial_state(
        self,
        run_id: str,
        state: OperationalBeliefState,
    ) -> EventEnvelope: ...

    def record_context(self, run_id: str, frame: ContextFrame) -> EventEnvelope: ...

    def record_model_request(
        self,
        run_id: str,
        record: DecisionRequestRecord,
    ) -> EventEnvelope: ...

    def record_model_attempt(
        self,
        run_id: str,
        preparation: DecisionPreparation,
        attempt: DecisionAttemptRecord,
    ) -> EventEnvelope: ...

    def record_model_terminal(
        self,
        run_id: str,
        record: DecisionTerminalRecord,
    ) -> EventEnvelope: ...

    def record_proposal(self, run_id: str, proposal: ActionProposal) -> EventEnvelope: ...

    def record_constraints(
        self,
        run_id: str,
        evaluation: ConstraintEvaluation,
    ) -> EventEnvelope: ...

    def record_authorization(
        self,
        run_id: str,
        decision: AuthorizationDecision,
    ) -> EventEnvelope: ...

    def record_execution(
        self,
        run_id: str,
        entry: ExecutionJournalEntry,
    ) -> EventEnvelope: ...

    def record_outcome(
        self,
        run_id: str,
        observation: OutcomeObservation,
        *,
        outcome_event_ids: tuple[str, ...],
    ) -> EventEnvelope: ...

    def record_outcome_state(
        self,
        run_id: str,
        state: OperationalBeliefState,
    ) -> EventEnvelope: ...

    def record_evaluation(
        self,
        run_id: str,
        evaluation: OutcomeEvaluation,
    ) -> EventEnvelope: ...

    def record_accepted_transition(
        self,
        run_id: str,
        transition: AcceptedStateTransition,
    ) -> EventEnvelope: ...

    def complete(self, run_id: str, outcome: RunOutcome) -> RunTerminal: ...

    def fail(self, run_id: str, *, phase: str, error_type: str) -> RunTerminal: ...


__all__ = ["FeedbackRunOpening", "FeedbackRunRecorder"]
