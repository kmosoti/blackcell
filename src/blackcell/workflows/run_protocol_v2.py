from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
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
    RunOutcome,
    RunTerminal,
)

V2_EVENT_PAYLOAD_FIELDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        RUN_STARTED: frozenset(
            {
                "run_id",
                "request_digest",
                "workflow",
                "workflow_version",
                "task_id",
                "objective",
                "domain",
                "observation_stream_id",
                "artifact",
            }
        ),
        EVALUATION_SPECIFIED: frozenset(
            {
                "run_id",
                "evaluation_spec_id",
                "evaluation_spec_digest",
                "request_digest",
                "artifact",
            }
        ),
        INITIAL_STATE_RECORDED: frozenset(
            {
                "run_id",
                "snapshot_digest",
                "domain",
                "stream_id",
                "cutoff_global_position",
                "last_source_stream_sequence",
                "effective_time_cutoff",
                "artifact",
            }
        ),
        CONTEXT_RECORDED: frozenset(
            {
                "run_id",
                "frame_id",
                "task_id",
                "state_domain",
                "state_stream_id",
                "state_global_position",
                "state_stream_position",
                "source_packet_id",
                "source_selection_id",
                "artifact",
            }
        ),
        MODEL_REQUESTED: frozenset(
            {"run_id", "request_id", "request_digest", "context_frame_id", "artifact"}
        ),
        MODEL_ATTEMPT_RECORDED: frozenset(
            {
                "run_id",
                "attempt_id",
                "request_id",
                "request_digest",
                "route_id",
                "attempt_number",
                "route_artifact",
                "artifact",
            }
        ),
        MODEL_RESPONDED: frozenset(
            {
                "run_id",
                "response_id",
                "request_id",
                "request_digest",
                "attempt_id",
                "route_id",
                "proposal_id",
                "usage_id",
                "input_tokens",
                "output_tokens",
                "latency_ms",
                "cost_microusd",
                "deterministic",
                "artifact",
                "usage_artifact",
            }
        ),
        MODEL_FAILED: frozenset(
            {
                "run_id",
                "failure_id",
                "request_id",
                "request_digest",
                "kind",
                "code",
                "retryable",
                "route_id",
                "attempt_id",
                "usage_id",
                "input_tokens",
                "output_tokens",
                "latency_ms",
                "cost_microusd",
                "deterministic",
                "route_artifact",
                "artifact",
                "usage_artifact",
            }
        ),
        PROPOSAL_RECORDED: frozenset(
            {
                "run_id",
                "proposal_id",
                "proposal_digest",
                "action_digest",
                "context_frame_id",
                "artifact",
            }
        ),
        CONSTRAINTS_EVALUATED: frozenset(
            {
                "run_id",
                "evaluation_id",
                "context_frame_id",
                "proof_ids",
                "safe",
                "artifact",
            }
        ),
        AUTHORIZATION_DECIDED: frozenset(
            {
                "run_id",
                "decision_id",
                "proposal_id",
                "constraint_evaluation_id",
                "outcome",
                "artifact",
            }
        ),
        EXECUTION_RECORDED: frozenset(
            {
                "run_id",
                "preparation_id",
                "result_id",
                "invocation_id",
                "proposal_id",
                "proposal_digest",
                "authorization_decision_id",
                "authorized_action_digest",
                "execution_identity_digest",
                "status",
                "affordance",
                "adapter_id",
                "adapter_contract_version",
                "journal_position",
                "completed_at",
                "arguments",
                "preparation_artifact",
                "artifact",
            }
        ),
        OUTCOME_OBSERVED: frozenset(
            {
                "run_id",
                "observation_id",
                "observation_digest",
                "evaluation_spec_id",
                "execution_binding_id",
                "status",
                "outcome_event_ids",
                "artifact",
            }
        ),
        OUTCOME_STATE_RECORDED: frozenset(
            {
                "run_id",
                "snapshot_digest",
                "domain",
                "stream_id",
                "cutoff_global_position",
                "last_source_stream_sequence",
                "effective_time_cutoff",
                "artifact",
            }
        ),
        EVALUATION_RECORDED: frozenset(
            {"run_id", "evaluation_id", "evaluation_spec_id", "verdict", "artifact"}
        ),
        STATE_TRANSITION_RECORDED: frozenset(
            {
                "run_id",
                "transition_id",
                "initial_snapshot_digest",
                "outcome_snapshot_digest",
                "evaluation_id",
                "accepted_claim_ids",
                "accepted_source_event_ids",
                "artifact",
            }
        ),
        TRACE_RECORDED: frozenset({"run_id", "outcome", "entry_count", "artifact"}),
        RUN_COMPLETED: frozenset(
            {
                "run_id",
                "outcome",
                "authorization_outcome",
                "execution_status",
                "trace_artifact_digest",
            }
        ),
        RUN_FAILED: frozenset(
            {
                "run_id",
                "outcome",
                "phase",
                "error_type",
                "trace_artifact_digest",
                "artifact",
            }
        ),
    }
)


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


__all__ = ["V2_EVENT_PAYLOAD_FIELDS", "FeedbackRunOpening", "FeedbackRunRecorder"]
