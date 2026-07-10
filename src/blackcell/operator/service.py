from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from blackcell.context import (
    ContextFrame,
    DeterministicContextProjector,
    SignalPacketProjector,
)
from blackcell.control import (
    ActionProposal,
    AffordanceDefinition,
    BoundedExecutor,
    Constraint,
    ExecutionResult,
    ExpectedEffect,
    PolicyDecision,
    PolicyEngine,
    PolicyFinding,
    PolicyOutcome,
    default_affordances,
    validate_affordance_arguments,
)
from blackcell.domains.repository import (
    CORRECTION_RECORDED,
    ClaimCorrection,
    OperationalStateEstimate,
    RepositoryProjector,
    RepositorySemanticEvent,
    ToolEvidence,
    adapt_tool_evidence,
    observe_repository,
)
from blackcell.kernel import ArtifactStore, EventEnvelope, EventStore, new_event_id
from blackcell.models import (
    ACTION_PROPOSAL_SCHEMA,
    DecisionModel,
    DecisionResult,
    RecordedModel,
)
from blackcell.operator.models import (
    ExecutionSummary,
    HistoricalReplay,
    OperatorEvaluation,
    OperatorRunResult,
    OperatorRunStatus,
    ReplayArtifact,
    RunArtifacts,
    StoredContextFrame,
)
from blackcell.operator.serialization import jsonable
from blackcell.telemetry import SpanNames, TraceRecorder

RUN_STARTED = "operator.run-started"
STATE_PROJECTED = "operator.state-projected"
SIGNAL_PACKET_BUILT = "operator.signal-packet-built"
CONTEXT_BUILT = "operator.context-built"
PROPOSAL_RECORDED = "operator.proposal-recorded"
POLICY_EVALUATED = "operator.policy-evaluated"
ACTION_OBSERVED = "operator.action-observed"
EVALUATION_RECORDED = "operator.evaluation-recorded"
TRANSITION_COMMITTED = "operator.state-transition-committed"
TRACE_RECORDED = "operator.trace-recorded"
RUN_COMPLETED = "operator.run-completed"
RUN_FAILED = "operator.run-failed"

DEFAULT_OBJECTIVE = "Inspect current repository readiness and select one safe diagnostic action."
DEFAULT_CONSTRAINTS = (
    "Only declared read-only affordances may execute without explicit approval.",
    "Model assertions must cite evidence present in this ContextFrame.",
)


class RepositoryObserver(Protocol):
    def __call__(
        self,
        repo_root: Path,
        *,
        observed_at: datetime | None = None,
        starting_sequence: int = 1,
    ) -> tuple[RepositorySemanticEvent, ...]: ...


class ActionExecutor(Protocol):
    def execute(self, proposal: ActionProposal, decision: PolicyDecision) -> ExecutionResult: ...


Clock = Callable[[], datetime]


class RepositoryOperator:
    """One complete, local-first Repository Operator control loop."""

    def __init__(
        self,
        repo_root: Path | str,
        *,
        database_path: Path | str | None = None,
        artifact_root: Path | str | None = None,
        model: DecisionModel[ActionProposal] | None = None,
        observer: RepositoryObserver = observe_repository,
        state_projector: RepositoryProjector | None = None,
        context_projector: DeterministicContextProjector | None = None,
        signal_projector: SignalPacketProjector | None = None,
        policy_engine: PolicyEngine | None = None,
        executor: ActionExecutor | None = None,
        affordances: tuple[AffordanceDefinition, ...] | None = None,
        check_commands: Mapping[str, tuple[str, ...]] | None = None,
        trace_recorder: TraceRecorder | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        git_directory = _validated_git_directory(self.repo_root)
        self.database_path = (
            Path(database_path)
            if database_path is not None
            else git_directory / "blackcell" / "kernel.sqlite3"
        )
        resolved_artifacts = (
            Path(artifact_root)
            if artifact_root is not None
            else self.database_path.parent / "artifacts"
        )
        self.events = EventStore(self.database_path)
        self.artifacts = ArtifactStore(resolved_artifacts, database_path=self.database_path)
        self._model = model
        self._observer = observer
        self._state_projector = state_projector or RepositoryProjector()
        self._context_projector = context_projector or DeterministicContextProjector()
        self._signal_projector = signal_projector or SignalPacketProjector()
        self._policy_engine = policy_engine or PolicyEngine()
        self._clock = clock or (lambda: datetime.now(UTC))
        definitions = affordances or default_affordances(tuple((check_commands or {}).keys()))
        self._affordances = {item.name: item for item in definitions}
        if len(self._affordances) != len(definitions):
            raise ValueError("affordance names must be unique")
        self._executor = executor or BoundedExecutor(
            self.repo_root,
            affordances=definitions,
            check_commands=check_commands,
            clock=self._clock,
        )
        self._traces = trace_recorder or TraceRecorder()
        root_digest = hashlib.sha256(str(self.repo_root).encode()).hexdigest()[:20]
        self.repository_stream_id = f"repository:{root_digest}"

    def run(
        self,
        *,
        objective: str = DEFAULT_OBJECTIVE,
        constraints: tuple[Constraint, ...] = (),
        context_constraints: tuple[str, ...] = DEFAULT_CONSTRAINTS,
        required_claim_ids: tuple[str, ...] = (),
        approval_granted: bool = False,
        token_budget: int = 2_000,
        character_budget: int = 8_000,
    ) -> OperatorRunResult:
        run_id = new_event_id()
        run_stream_id = _run_stream_id(run_id)
        started = self._append_run_event(
            run_stream_id,
            run_id,
            RUN_STARTED,
            {
                "run_id": run_id,
                "repository_stream_id": self.repository_stream_id,
                "objective": objective,
            },
        )
        try:
            return self._execute_run(
                run_id=run_id,
                run_stream_id=run_stream_id,
                started_event_id=started.event_id,
                objective=objective,
                constraints=constraints,
                context_constraints=context_constraints,
                required_claim_ids=required_claim_ids,
                approval_granted=approval_granted,
                token_budget=token_budget,
                character_budget=character_budget,
            )
        except Exception as error:
            failure = {
                "run_id": run_id,
                "error_type": type(error).__name__,
                "message": str(error)[:500],
            }
            failure_ref = self.artifacts.put_json(failure)
            recorded = self.events.read_stream(run_stream_id)
            trace_records = self._traces.records(trace_id=run_id)
            trace_ref = self.artifacts.put_json(jsonable(trace_records))
            trace_event = self._append_run_event(
                run_stream_id,
                run_id,
                TRACE_RECORDED,
                {
                    "run_id": run_id,
                    "status": "error",
                    "span_count": len(trace_records),
                    "artifact_id": trace_ref.digest,
                },
                causation_id=recorded[-1].event_id if recorded else started.event_id,
            )
            self._append_run_event(
                run_stream_id,
                run_id,
                RUN_FAILED,
                {
                    "run_id": run_id,
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "artifact_id": failure_ref.digest,
                },
                causation_id=trace_event.event_id,
            )
            raise

    @staticmethod
    def default_database_path(repo_root: Path | str) -> Path:
        root = Path(repo_root).resolve()
        return _validated_git_directory(root) / "blackcell" / "kernel.sqlite3"

    def current_state(self, *, as_of_time: datetime | None = None) -> OperationalStateEstimate:
        events = self.events.read_stream(self.repository_stream_id)
        return self._state_projector.project(
            cast(Any, events),
            repository_id=self.repository_stream_id,
            as_of_time=as_of_time or self._aware_now(),
        )

    def append_correction(
        self,
        correction: ClaimCorrection,
        *,
        actor: str = "human-operator",
        source: str = "human-correction",
    ) -> EventEnvelope:
        """Append a human correction as new evidence; history is never overwritten."""

        if not actor.strip() or not source.strip():
            raise ValueError("correction actor and source must be non-empty")
        sequence = self.events.current_sequence(self.repository_stream_id) + 1
        semantic_event = RepositorySemanticEvent(
            event_id=new_event_id(),
            sequence=sequence,
            kind=CORRECTION_RECORDED,
            source=source,
            occurred_at=self._aware_now(),
            payload=correction,
        )
        return self._append_repository_semantic_event(
            semantic_event,
            run_id=correction.correction_id,
            causation_id=None,
            actor=actor,
        )

    def context(self, run_id: str | None = None) -> StoredContextFrame:
        resolved_run_id = run_id or self._latest_run_id()
        events = self._scoped_run_events(resolved_run_id)
        context_event = next(
            (event for event in reversed(events) if event.event_type == CONTEXT_BUILT),
            None,
        )
        if context_event is None:
            raise LookupError(f"run {resolved_run_id!r} has no recorded ContextFrame")
        digest = _required_payload_text(context_event, "artifact_id")
        payload = self.artifacts.get_json(digest)
        if not isinstance(payload, Mapping):
            raise TypeError("stored ContextFrame artifact must be a JSON object")
        return StoredContextFrame(
            run_id=resolved_run_id,
            frame_id=_required_payload_text(context_event, "context_frame_id"),
            artifact_digest=digest,
            payload=cast(Mapping[str, Any], payload),
        )

    def replay(self, run_id: str | None = None) -> HistoricalReplay:
        """Reconstruct a historical run using ledger and artifact reads only."""

        resolved_run_id = run_id or self._latest_run_id()
        run_stream_id = _run_stream_id(resolved_run_id)
        events = self._scoped_run_events(resolved_run_id)
        artifact_digests = tuple(
            dict.fromkeys(
                digest
                for event in events
                if (digest := _optional_payload_text(event, "artifact_id")) is not None
            )
        )
        reproduced = self._reproduce_state_artifacts(events)
        artifacts = tuple(
            ReplayArtifact(
                digest=reference.digest,
                media_type=reference.media_type,
                size_bytes=reference.size_bytes,
                verified=self.artifacts.verify(reference),
                reproduced=reproduced.get(reference.digest),
            )
            for reference in (self.artifacts.stat(digest) for digest in artifact_digests)
        )
        terminal = next(
            (
                event
                for event in reversed(events)
                if event.event_type in (RUN_COMPLETED, RUN_FAILED)
            ),
            events[-1],
        )
        status = str(terminal.payload.get("status", "failed"))
        return HistoricalReplay(
            run_id=resolved_run_id,
            status=status,
            run_stream_id=run_stream_id,
            events=events,
            artifacts=artifacts,
            projection_hash_match=bool(reproduced) and all(reproduced.values()),
        )

    def _reproduce_state_artifacts(self, run_events: tuple[EventEnvelope, ...]) -> dict[str, bool]:
        start = next(
            (event for event in run_events if event.event_type == RUN_STARTED),
            None,
        )
        if start is None:
            return {}
        repository_stream_id = _required_payload_text(start, "repository_stream_id")
        repository_events = self.events.read_stream(repository_stream_id)
        state_digests = tuple(
            dict.fromkeys(
                digest
                for event in run_events
                if event.event_type in (STATE_PROJECTED, TRANSITION_COMMITTED)
                if (digest := _optional_payload_text(event, "artifact_id")) is not None
            )
        )
        results: dict[str, bool] = {}
        for digest in state_digests:
            payload = self.artifacts.get_json(digest)
            if not isinstance(payload, Mapping):
                results[digest] = False
                continue
            sequence = payload.get("as_of_sequence")
            as_of_time = payload.get("as_of_time")
            if not isinstance(sequence, int) or not isinstance(as_of_time, str):
                results[digest] = False
                continue
            projected = self._state_projector.project(
                cast(
                    Any,
                    tuple(
                        event for event in repository_events if event.stream_sequence <= sequence
                    ),
                ),
                repository_id=repository_stream_id,
                as_of_sequence=sequence,
                as_of_time=datetime.fromisoformat(as_of_time),
            )
            results[digest] = _json_artifact_digest(jsonable(projected)) == digest
        return results

    def _execute_run(
        self,
        *,
        run_id: str,
        run_stream_id: str,
        started_event_id: str,
        objective: str,
        constraints: tuple[Constraint, ...],
        context_constraints: tuple[str, ...],
        required_claim_ids: tuple[str, ...],
        approval_granted: bool,
        token_budget: int,
        character_budget: int,
    ) -> OperatorRunResult:
        correlations = {"run_id": run_id}
        with self._traces.span(
            SpanNames.OBSERVE,
            trace_id=run_id,
            correlation_ids=correlations,
        ):
            observations = self._observe_and_append(run_id, causation_id=started_event_id)

        with self._traces.span(
            SpanNames.PROJECT_STATE,
            trace_id=run_id,
            correlation_ids=correlations,
        ):
            initial_state = self.current_state()
            initial_state_ref = self.artifacts.put_json(jsonable(initial_state))
            state_event = self._append_run_event(
                run_stream_id,
                run_id,
                STATE_PROJECTED,
                {
                    "run_id": run_id,
                    "phase": "before-action",
                    "state_id": initial_state.state_id,
                    "artifact_id": initial_state_ref.digest,
                },
                causation_id=(observations[-1].event_id if observations else started_event_id),
            )
            signal_packet = self._signal_projector.project(initial_state)
            signal_ref = self.artifacts.put_json(jsonable(signal_packet))
            signal_event = self._append_run_event(
                run_stream_id,
                run_id,
                SIGNAL_PACKET_BUILT,
                {
                    "run_id": run_id,
                    "signal_packet_id": signal_packet.packet_id,
                    "state_id": initial_state.state_id,
                    "artifact_id": signal_ref.digest,
                },
                causation_id=state_event.event_id,
            )

        with self._traces.span(
            SpanNames.BUILD_CONTEXT,
            trace_id=run_id,
            correlation_ids=correlations,
        ):
            frame = self._context_projector.project(
                initial_state,
                objective=objective,
                constraints=context_constraints,
                available_affordances=tuple(self._affordances),
                affordance_contracts=tuple(
                    definition.signature() for definition in self._affordances.values()
                ),
                required_claim_ids=required_claim_ids,
                token_budget=token_budget,
                character_budget=character_budget,
            )
            frame_payload = cast(dict[str, Any], jsonable(frame))
            frame_ref = self.artifacts.put_json(frame_payload)
            context_event = self._append_run_event(
                run_stream_id,
                run_id,
                CONTEXT_BUILT,
                {
                    "run_id": run_id,
                    "context_frame_id": frame.frame_id,
                    "state_id": initial_state.state_id,
                    "artifact_id": frame_ref.digest,
                },
                causation_id=signal_event.event_id,
            )

        with self._traces.span(
            SpanNames.MODEL_DECIDE,
            trace_id=run_id,
            correlation_ids=correlations,
        ):
            decision = self._decide(frame, frame_payload, run_id)
            model_ref = self.artifacts.put_json(jsonable(decision))
            proposal_event = self._append_run_event(
                run_stream_id,
                run_id,
                PROPOSAL_RECORDED,
                {
                    "run_id": run_id,
                    "proposal_id": decision.proposal.proposal_id,
                    "context_frame_id": decision.proposal.context_frame_id,
                    "model": decision.invocation.provider,
                    "artifact_id": model_ref.digest,
                },
                causation_id=context_event.event_id,
            )

        with self._traces.span(
            SpanNames.POLICY_EVALUATE,
            trace_id=run_id,
            correlation_ids=correlations,
        ):
            policy = self._evaluate_policy(
                decision.proposal,
                frame,
                initial_state,
                constraints,
                approval_granted,
            )
            policy_ref = self.artifacts.put_json(jsonable(policy))
            policy_event = self._append_run_event(
                run_stream_id,
                run_id,
                POLICY_EVALUATED,
                {
                    "run_id": run_id,
                    "proposal_id": decision.proposal.proposal_id,
                    "decision_id": policy.decision_id,
                    "outcome": policy.outcome.value,
                    "artifact_id": policy_ref.digest,
                },
                causation_id=proposal_event.event_id,
            )

        execution: ExecutionResult | None = None
        execution_ref_digest: str | None = None
        final_state: OperationalStateEstimate | None = None
        final_state_ref_digest: str | None = None
        outcome_causation_id = policy_event.event_id
        if policy.outcome is PolicyOutcome.ALLOW:
            with self._traces.span(
                SpanNames.AFFORDANCE_EXECUTE,
                trace_id=run_id,
                correlation_ids=correlations,
            ):
                execution = self._executor.execute(decision.proposal, policy)
                execution_ref = self.artifacts.put_json(jsonable(execution))
                execution_ref_digest = execution_ref.digest
                action_event = self._append_run_event(
                    run_stream_id,
                    run_id,
                    ACTION_OBSERVED,
                    {
                        "run_id": run_id,
                        "proposal_id": decision.proposal.proposal_id,
                        "attempt_id": execution.attempt.attempt_id,
                        "success": execution.outcome.success,
                        "output_digest": execution.outcome.output_digest,
                        "artifact_id": execution_ref.digest,
                    },
                    causation_id=policy_event.event_id,
                )
                outcome_causation_id = action_event.event_id
                tool_event = self._record_tool_evidence(
                    decision.proposal,
                    execution,
                    artifact_id=execution_ref.digest,
                    run_id=run_id,
                    causation_id=action_event.event_id,
                )
                outcome_causation_id = tool_event.event_id
            with self._traces.span(
                SpanNames.OUTCOME_OBSERVE,
                trace_id=run_id,
                correlation_ids=correlations,
            ):
                outcome_observations = self._observe_and_append(
                    run_id, causation_id=outcome_causation_id
                )
                if outcome_observations:
                    outcome_causation_id = outcome_observations[-1].event_id
                final_state = self.current_state()
                final_state_ref = self.artifacts.put_json(jsonable(final_state))
                final_state_ref_digest = final_state_ref.digest

        with self._traces.span(
            SpanNames.EVALUATION_GRADE,
            trace_id=run_id,
            correlation_ids=correlations,
        ):
            evaluation = _evaluate(
                decision.proposal,
                policy,
                execution,
                initial_state,
                final_state or initial_state,
                execution_ref_digest,
            )
            evaluation_ref = self.artifacts.put_json(jsonable(evaluation))
            evaluation_event = self._append_run_event(
                run_stream_id,
                run_id,
                EVALUATION_RECORDED,
                {
                    "run_id": run_id,
                    "evaluation_id": evaluation.evaluation_id,
                    "passed": evaluation.passed,
                    "artifact_id": evaluation_ref.digest,
                },
                causation_id=outcome_causation_id,
            )

        terminal_causation_id = evaluation_event.event_id
        if policy.outcome is PolicyOutcome.ALLOW:
            with self._traces.span(
                SpanNames.TRANSITION_COMMIT,
                trace_id=run_id,
                correlation_ids=correlations,
            ):
                transition_event = self._append_run_event(
                    run_stream_id,
                    run_id,
                    TRANSITION_COMMITTED,
                    {
                        "run_id": run_id,
                        "from_state_id": initial_state.state_id,
                        "to_state_id": (
                            final_state.state_id
                            if final_state is not None
                            else initial_state.state_id
                        ),
                        "accepted": evaluation.execution_success is True,
                        "artifact_id": final_state_ref_digest or initial_state_ref.digest,
                    },
                    causation_id=evaluation_event.event_id,
                )
                terminal_causation_id = transition_event.event_id

        trace_ref = self.artifacts.put_json(jsonable(self._traces.records(trace_id=run_id)))
        trace_event = self._append_run_event(
            run_stream_id,
            run_id,
            TRACE_RECORDED,
            {
                "run_id": run_id,
                "span_count": len(self._traces.records(trace_id=run_id)),
                "artifact_id": trace_ref.digest,
            },
            causation_id=terminal_causation_id,
        )
        status = (
            OperatorRunStatus.DENIED
            if policy.outcome is not PolicyOutcome.ALLOW
            else (OperatorRunStatus.COMPLETED if evaluation.passed else OperatorRunStatus.FAILED)
        )
        self._append_run_event(
            run_stream_id,
            run_id,
            RUN_COMPLETED,
            {
                "run_id": run_id,
                "status": status.value,
                "evaluation_id": evaluation.evaluation_id,
                "passed": evaluation.passed,
            },
            causation_id=trace_event.event_id,
        )
        run_events = self.events.read_stream(run_stream_id)
        return OperatorRunResult(
            run_id=run_id,
            status=status,
            repository_stream_id=self.repository_stream_id,
            run_stream_id=run_stream_id,
            initial_state_id=initial_state.state_id,
            signal_packet_id=signal_packet.packet_id,
            final_state_id=final_state.state_id if final_state is not None else None,
            context_frame_id=frame.frame_id,
            proposal=decision.proposal,
            invocation=decision.invocation,
            policy=policy,
            execution=_execution_summary(execution),
            evaluation=evaluation,
            artifacts=RunArtifacts(
                initial_state=initial_state_ref.digest,
                signal_packet=signal_ref.digest,
                context_frame=frame_ref.digest,
                model_decision=model_ref.digest,
                policy_decision=policy_ref.digest,
                execution_result=execution_ref_digest,
                final_state=final_state_ref_digest,
                evaluation=evaluation_ref.digest,
                trace=trace_ref.digest,
            ),
            run_event_count=len(run_events),
            trace_span_count=len(self._traces.records(trace_id=run_id)),
        )

    def _observe_and_append(self, run_id: str, *, causation_id: str) -> tuple[EventEnvelope, ...]:
        starting_sequence = self.events.current_sequence(self.repository_stream_id) + 1
        observations = self._observer(
            self.repo_root,
            observed_at=self._aware_now(),
            starting_sequence=starting_sequence,
        )
        appended = []
        for observation in observations:
            appended.append(
                self._append_repository_semantic_event(
                    observation,
                    run_id=run_id,
                    causation_id=causation_id,
                )
            )
        return tuple(appended)

    def _record_tool_evidence(
        self,
        proposal: ActionProposal,
        execution: ExecutionResult,
        *,
        artifact_id: str,
        run_id: str,
        causation_id: str,
    ) -> EventEnvelope:
        subject, predicate, success_value, failure_value = _tool_claim_contract(proposal)
        evidence = ToolEvidence(
            subject=subject,
            predicate=predicate,
            status=(success_value if execution.outcome.success else failure_value),
            output_digest=execution.outcome.output_digest,
            artifact_id=artifact_id,
        )
        sequence = self.events.current_sequence(self.repository_stream_id) + 1
        semantic_event = adapt_tool_evidence(
            evidence,
            observed_at=execution.outcome.observed_at,
            sequence=sequence,
        )
        return self._append_repository_semantic_event(
            semantic_event,
            run_id=run_id,
            causation_id=causation_id,
        )

    def _append_repository_semantic_event(
        self,
        observation: RepositorySemanticEvent,
        *,
        run_id: str,
        causation_id: str | None,
        actor: str = "repository-operator",
    ) -> EventEnvelope:
        payload = cast(dict[str, Any], jsonable(observation.payload))
        payload["domain"] = "repository"
        payload["domain_schema_version"] = observation.schema_version
        occurrence_id = new_event_id()
        _rebind_evidence_event_ids(
            payload,
            original_event_id=observation.event_id,
            occurrence_id=occurrence_id,
        )
        envelope = EventEnvelope.create(
            event_id=occurrence_id,
            stream_id=self.repository_stream_id,
            stream_sequence=observation.sequence,
            event_type=observation.kind,
            actor=actor,
            source=observation.source,
            payload=payload,
            recorded_at=observation.occurred_at,
            effective_at=observation.occurred_at,
            correlation_id=run_id,
            causation_id=causation_id,
        )
        expected = self.events.current_sequence(self.repository_stream_id)
        return self.events.append(envelope, expected_sequence=expected)

    def _decide(
        self,
        frame: ContextFrame,
        frame_payload: Mapping[str, Any],
        run_id: str,
    ) -> DecisionResult[ActionProposal]:
        model = self._model
        if model is None:
            proposal = _default_proposal(frame, run_id)
            model = RecordedModel.for_frames(
                {"default": (frame_payload, proposal)},
                output_schema=ACTION_PROPOSAL_SCHEMA,
            )
        return model.decide(
            frame_payload,
            output_schema=ACTION_PROPOSAL_SCHEMA,
            correlation_id=run_id,
        )

    def _evaluate_policy(
        self,
        proposal: ActionProposal,
        frame: ContextFrame,
        state: OperationalStateEstimate,
        constraints: tuple[Constraint, ...],
        approval_granted: bool,
    ) -> PolicyDecision:
        findings = _proposal_boundary_findings(proposal, frame, self._affordances)
        if findings:
            return PolicyDecision(
                proposal_id=proposal.proposal_id,
                outcome=PolicyOutcome.DENY,
                findings=findings,
                evaluated_at=self._aware_now(),
                approval_granted=approval_granted,
            )
        definition = self._affordances[proposal.affordance]
        return self._policy_engine.evaluate(
            proposal,
            definition,
            state,
            constraints=constraints,
            evaluated_at=self._aware_now(),
            approval_granted=approval_granted,
        )

    def _append_run_event(
        self,
        run_stream_id: str,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        causation_id: str | None = None,
    ) -> EventEnvelope:
        sequence = self.events.current_sequence(run_stream_id)
        envelope = EventEnvelope.create(
            stream_id=run_stream_id,
            stream_sequence=sequence + 1,
            event_type=event_type,
            actor="repository-operator",
            source="blackcell.operator",
            payload=payload,
            recorded_at=self._aware_now(),
            correlation_id=run_id,
            causation_id=causation_id,
        )
        return self.events.append(envelope, expected_sequence=sequence)

    def _latest_run_id(self) -> str:
        starts = tuple(
            event
            for event in self.events.read_all()
            if event.event_type == RUN_STARTED
            and event.payload.get("repository_stream_id") == self.repository_stream_id
        )
        if not starts:
            raise LookupError("no Repository Operator run has been recorded")
        return _required_payload_text(starts[-1], "run_id")

    def _scoped_run_events(self, run_id: str) -> tuple[EventEnvelope, ...]:
        events = self.events.read_stream(_run_stream_id(run_id))
        if not events:
            raise LookupError(f"operator run {run_id!r} does not exist")
        start = next(
            (event for event in events if event.event_type == RUN_STARTED),
            None,
        )
        if start is None or start.payload.get("repository_stream_id") != self.repository_stream_id:
            raise LookupError(f"operator run {run_id!r} does not belong to this repository")
        return events

    def _aware_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("operator clock must return timezone-aware timestamps")
        return value


def _default_proposal(frame: ContextFrame, run_id: str) -> ActionProposal:
    evidence_ids = tuple(
        dict.fromkeys(
            evidence.event_id for claim in frame.selected_claims for evidence in claim.evidence
        )
    )
    return ActionProposal(
        proposal_id=f"proposal:{run_id}",
        context_frame_id=frame.frame_id,
        affordance="git_status",
        arguments=(),
        expected_effects=(ExpectedEffect("affordance:git_status", "status", "succeeded"),),
        rationale="Refresh bounded Git status evidence before any repository mutation.",
        evidence_ids=evidence_ids,
    )


def _proposal_boundary_findings(
    proposal: ActionProposal,
    frame: ContextFrame,
    affordances: Mapping[str, AffordanceDefinition],
) -> tuple[PolicyFinding, ...]:
    findings: list[PolicyFinding] = []
    if proposal.context_frame_id != frame.frame_id:
        findings.append(
            PolicyFinding(
                "proposal-boundary",
                PolicyOutcome.DENY,
                "context_frame_mismatch",
                "proposal does not target the supplied ContextFrame",
            )
        )
    if proposal.affordance not in affordances:
        findings.append(
            PolicyFinding(
                "proposal-boundary",
                PolicyOutcome.DENY,
                "undeclared_affordance",
                f"affordance {proposal.affordance!r} is not declared",
            )
        )
    else:
        for code, message in validate_affordance_arguments(
            proposal, affordances[proposal.affordance]
        ):
            findings.append(
                PolicyFinding(
                    "proposal-boundary",
                    PolicyOutcome.DENY,
                    code,
                    message,
                )
            )
    available_evidence = {
        evidence.event_id for claim in frame.selected_claims for evidence in claim.evidence
    }
    cited = set(proposal.evidence_ids)
    cited.update(
        evidence_id for assertion in proposal.assertions for evidence_id in assertion.evidence_ids
    )
    unsupported = sorted(cited - available_evidence)
    if unsupported:
        findings.append(
            PolicyFinding(
                "proposal-boundary",
                PolicyOutcome.DENY,
                "unsupported_evidence_reference",
                "proposal cites evidence absent from the ContextFrame: " + ", ".join(unsupported),
            )
        )
    if any(not assertion.evidence_ids for assertion in proposal.assertions):
        findings.append(
            PolicyFinding(
                "proposal-boundary",
                PolicyOutcome.DENY,
                "uncited_assertion",
                "every model assertion must cite ContextFrame evidence",
            )
        )
    return tuple(findings)


def _evaluate(
    proposal: ActionProposal,
    policy: PolicyDecision,
    execution: ExecutionResult | None,
    initial_state: OperationalStateEstimate,
    final_state: OperationalStateEstimate,
    execution_artifact_id: str | None,
) -> OperatorEvaluation:
    residuals = tuple(
        _effect_residual(effect, final_state)
        for effect in proposal.expected_effects
        if not _effect_observed(
            effect,
            initial_state,
            final_state,
            execution_artifact_id,
        )
    )
    matched = len(proposal.expected_effects) - len(residuals)
    attempted = execution is not None
    succeeded = execution.outcome.success if execution is not None else None
    policy_enforced = attempted if policy.outcome is PolicyOutcome.ALLOW else not attempted
    effect_match = not residuals if proposal.expected_effects and attempted else None
    passed = policy_enforced and (
        bool(succeeded) and effect_match is not False
        if policy.outcome is PolicyOutcome.ALLOW
        else True
    )
    payload = {
        "proposal_id": proposal.proposal_id,
        "policy_outcome": policy.outcome.value,
        "policy_enforced": policy_enforced,
        "attempted": attempted,
        "succeeded": succeeded,
        "effect_match": effect_match,
        "residuals": residuals,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return OperatorEvaluation(
        evaluation_id=f"evaluation:{digest}",
        proposal_id=proposal.proposal_id,
        policy_outcome=policy.outcome.value,
        policy_enforced=policy_enforced,
        action_attempted=attempted,
        execution_success=succeeded,
        effect_match=effect_match,
        task_success=None,
        expected_effect_count=len(proposal.expected_effects),
        matched_effect_count=matched,
        residuals=residuals,
        passed=passed,
    )


def _effect_observed(
    effect: ExpectedEffect,
    initial_state: OperationalStateEstimate,
    final_state: OperationalStateEstimate,
    execution_artifact_id: str | None,
) -> bool:
    if execution_artifact_id is None:
        return False
    initial_ids = {claim.claim_id for claim in initial_state.claims}
    conflicted_ids = {
        claim.claim_id for conflict in final_state.conflicts for claim in conflict.claims
    }
    return any(
        claim.claim_id not in initial_ids
        and claim.claim_id not in conflicted_ids
        and claim.value == effect.value
        and not claim.is_expired(final_state.as_of_time)
        and any(evidence.artifact_id == execution_artifact_id for evidence in claim.evidence)
        for claim in final_state.find_claims(effect.subject, effect.predicate)
    )


def _effect_residual(effect: ExpectedEffect, state: OperationalStateEstimate) -> str:
    values = [claim.value for claim in state.find_claims(effect.subject, effect.predicate)]
    return f"expected {effect.subject}/{effect.predicate}={effect.value!r}; observed {values!r}"


def _run_stream_id(run_id: str) -> str:
    return f"operator-run:{run_id}"


def _required_payload_text(event: EventEnvelope, key: str) -> str:
    value = _optional_payload_text(event, key)
    if value is None:
        raise ValueError(f"event {event.event_id} has no non-empty {key!r}")
    return value


def _optional_payload_text(event: EventEnvelope, key: str) -> str | None:
    value = event.payload.get(key)
    return value if isinstance(value, str) and value else None


def _json_artifact_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _tool_claim_contract(
    proposal: ActionProposal,
) -> tuple[str, str, str, str]:
    if proposal.affordance == "inspect_file":
        return f"path:{proposal.argument('path')}", "inspection.status", "succeeded", "failed"
    if proposal.affordance == "run_check":
        return f"check:{proposal.argument('check')}", "status", "passed", "failed"
    return f"affordance:{proposal.affordance}", "status", "succeeded", "failed"


def _execution_summary(execution: ExecutionResult | None) -> ExecutionSummary | None:
    if execution is None:
        return None
    return ExecutionSummary(
        attempt_id=execution.attempt.attempt_id,
        affordance=execution.attempt.affordance,
        status=execution.attempt.status.value,
        success=execution.outcome.success,
        output_digest=execution.outcome.output_digest,
        truncated=execution.outcome.truncated,
        error=execution.attempt.error,
    )


def _validated_git_directory(repo_root: Path) -> Path:
    if not repo_root.is_dir():
        raise ValueError(f"repository root does not exist or is not a directory: {repo_root}")
    marker = repo_root / ".git"
    if marker.is_dir():
        return marker
    if marker.is_file():
        try:
            declaration = marker.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ValueError(f"cannot read Git worktree marker: {marker}") from error
        prefix = "gitdir:"
        if declaration.casefold().startswith(prefix):
            candidate = Path(declaration[len(prefix) :].strip())
            resolved = (
                candidate.resolve()
                if candidate.is_absolute()
                else (repo_root / candidate).resolve()
            )
            if resolved.is_dir():
                return resolved
    raise ValueError(f"repository root is not a Git worktree: {repo_root}")


def _rebind_evidence_event_ids(
    payload: dict[str, Any],
    *,
    original_event_id: str,
    occurrence_id: str,
) -> None:
    for claim in payload.get("claims", []):
        if not isinstance(claim, dict):
            continue
        for evidence in claim.get("evidence", []):
            if isinstance(evidence, dict) and evidence.get("event_id") == original_event_id:
                evidence["event_id"] = occurrence_id
    correction = payload.get("correction")
    if not isinstance(correction, dict):
        return
    for evidence in correction.get("evidence", []):
        if isinstance(evidence, dict) and evidence.get("event_id") == original_event_id:
            evidence["event_id"] = occurrence_id
