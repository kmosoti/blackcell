from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import cast

from blackcell.features.authorize_action import ActionProposal, AuthorizationDecision
from blackcell.features.authorize_action.artifacts import (
    ACTION_PROPOSAL_MEDIA_TYPE,
    AUTHORIZATION_DECISION_MEDIA_TYPE,
    encode_action_proposal,
    encode_authorization_decision,
)
from blackcell.features.build_context import ContextFrame
from blackcell.features.execute_affordance import ExecutionResult
from blackcell.features.solve_constraints import ConstraintEvaluation
from blackcell.features.solve_constraints.artifacts import (
    CONSTRAINT_EVALUATION_MEDIA_TYPE,
    encode_constraint_evaluation,
)
from blackcell.kernel import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactRef,
    ArtifactStore,
    ConcurrencyError,
    EventEnvelope,
    EventStore,
    JsonInput,
    utc_now,
)
from blackcell.kernel._json import canonical_json_bytes
from blackcell.workflows.run_protocol import (
    AUTHORIZATION_DECIDED,
    CONSTRAINTS_EVALUATED,
    CONTEXT_RECORDED,
    EXECUTION_RECORDED,
    PROPOSAL_RECORDED,
    RUN_COMPLETED,
    RUN_EVENT_SCHEMA_VERSION,
    RUN_FAILED,
    RUN_FAILURE_MEDIA_TYPE,
    RUN_FAILURE_SCHEMA_VERSION,
    RUN_STARTED,
    RUN_TRACE_MEDIA_TYPE,
    RUN_TRACE_SCHEMA_VERSION,
    RUN_WORKFLOW,
    RUN_WORKFLOW_VERSION,
    TRACE_RECORDED,
    RunAlreadyExists,
    RunArtifactLink,
    RunIdentityConflict,
    RunInterrupted,
    RunOutcome,
    RunProtocolIntegrityError,
    RunProtocolVersion,
    RunStart,
    RunTerminal,
    run_stream_id,
)

_SOURCE = "blackcell.workflows.daily_operator"
_CONTEXT_MEDIA_TYPE = "application/vnd.blackcell.context-frame+json"
_EXECUTION_MEDIA_TYPE = "application/vnd.blackcell.execution-result+json"

_MATERIAL_ORDER = (
    RUN_STARTED,
    CONTEXT_RECORDED,
    PROPOSAL_RECORDED,
    CONSTRAINTS_EVALUATED,
    AUTHORIZATION_DECIDED,
)
_TERMINALS = frozenset({RUN_COMPLETED, RUN_FAILED})

Clock = Callable[[], datetime]


class KernelRunRecorder:
    """Record one strict run aggregate in the existing kernel stores.

    Artifacts are committed and verified before their referencing event. The
    stores expose no shared artifact/event transaction, so a crash may leave an
    unreferenced artifact but never an intentionally appended dangling link.
    """

    def __init__(
        self,
        events: EventStore,
        artifacts: ArtifactStore,
        *,
        clock: Clock = utc_now,
    ) -> None:
        if events.path.resolve() != artifacts.database_path.resolve():
            raise ValueError("run events and artifacts must use the same kernel database")
        self._events = events
        self._artifacts = artifacts
        self._clock = clock

    def start(self, command: RunStart) -> EventEnvelope:
        if command.protocol_version is not RunProtocolVersion.V1:
            raise RunProtocolIntegrityError(
                "daily-operator/v2 writing is not active until the complete feedback loop "
                "and replay verifier are composed"
            )
        stream_id = run_stream_id(command.run_id)
        existing = self._events.read_stream(stream_id)
        if existing:
            self._raise_existing_start(command, existing)

        payload: dict[str, JsonInput] = {
            "run_id": command.run_id,
            "request_digest": command.request_digest,
            "workflow": RUN_WORKFLOW,
            "workflow_version": RUN_WORKFLOW_VERSION,
            "task_id": command.task_id,
            "objective": command.objective,
            "domain": command.domain,
            "observation_stream_id": command.observation_stream_id,
        }
        timestamp = self._clock()
        event = EventEnvelope.create(
            stream_id=stream_id,
            stream_sequence=1,
            event_type=RUN_STARTED,
            schema_version=RUN_EVENT_SCHEMA_VERSION,
            actor=command.actor,
            source=_SOURCE,
            payload=payload,
            recorded_at=timestamp,
            effective_at=timestamp,
            correlation_id=command.run_id,
        )
        try:
            return self._events.append(event, expected_sequence=0)
        except ConcurrencyError:
            self._raise_existing_start(command, self._events.read_stream(stream_id))
            raise AssertionError(
                "existing-run classification must raise"
            ) from None  # pragma: no cover

    def record_context(self, run_id: str, frame: ContextFrame) -> EventEnvelope:
        events = self._expect_last(run_id, RUN_STARTED)
        start = events[0]
        if frame.task_id != _required_text(start.payload, "task_id"):
            raise RunProtocolIntegrityError("ContextFrame task does not match its run")
        if frame.objective != _required_text(start.payload, "objective"):
            raise RunProtocolIntegrityError("ContextFrame objective does not match its run")
        if frame.state_domain != _required_text(start.payload, "domain"):
            raise RunProtocolIntegrityError("ContextFrame domain does not match its run")
        if frame.state_stream_id != _required_text(start.payload, "observation_stream_id"):
            raise RunProtocolIntegrityError(
                "ContextFrame state stream does not match its observation stream"
            )
        artifact = self._existing_link(
            frame.frame_id,
            media_type=_CONTEXT_MEDIA_TYPE,
            schema_version=frame.schema_version,
            logical_id=frame.frame_id,
        )
        return self._append(
            run_id,
            CONTEXT_RECORDED,
            {
                "run_id": run_id,
                "frame_id": frame.frame_id,
                "task_id": frame.task_id,
                "state_domain": frame.state_domain,
                "state_stream_id": frame.state_stream_id,
                "state_global_position": frame.state_global_position,
                "state_stream_position": frame.state_stream_position,
                "source_packet_id": frame.source_packet_id,
                "source_selection_id": frame.source_selection_id,
                "artifact": artifact.as_payload(),
            },
            events=events,
        )

    def record_proposal(self, run_id: str, proposal: ActionProposal) -> EventEnvelope:
        events = self._expect_last(run_id, CONTEXT_RECORDED)
        context_frame_id = _required_text(events[-1].payload, "frame_id")
        if proposal.context_frame_id != context_frame_id:
            raise RunProtocolIntegrityError("proposal belongs to a different ContextFrame")
        artifact = self._put_link(
            encode_action_proposal(proposal),
            media_type=ACTION_PROPOSAL_MEDIA_TYPE,
            schema_version=proposal.schema_version,
            logical_id=proposal.proposal_digest,
        )
        return self._append(
            run_id,
            PROPOSAL_RECORDED,
            {
                "run_id": run_id,
                "proposal_id": proposal.proposal_id,
                "proposal_digest": proposal.proposal_digest,
                "action_digest": proposal.action_digest,
                "context_frame_id": proposal.context_frame_id,
                "artifact": artifact.as_payload(),
            },
            events=events,
        )

    def record_constraints(self, run_id: str, evaluation: ConstraintEvaluation) -> EventEnvelope:
        events = self._expect_last(run_id, PROPOSAL_RECORDED)
        context_frame_id = _required_text(events[-1].payload, "context_frame_id")
        if evaluation.context_frame_id != context_frame_id:
            raise RunProtocolIntegrityError(
                "constraint evaluation belongs to a different ContextFrame"
            )
        artifact = self._put_link(
            encode_constraint_evaluation(evaluation),
            media_type=CONSTRAINT_EVALUATION_MEDIA_TYPE,
            schema_version=evaluation.schema_version,
            logical_id=evaluation.evaluation_id,
        )
        return self._append(
            run_id,
            CONSTRAINTS_EVALUATED,
            {
                "run_id": run_id,
                "evaluation_id": evaluation.evaluation_id,
                "context_frame_id": evaluation.context_frame_id,
                "proof_ids": [item.proof_id for item in evaluation.proofs],
                "safe": evaluation.safe,
                "artifact": artifact.as_payload(),
            },
            events=events,
        )

    def record_authorization(self, run_id: str, decision: AuthorizationDecision) -> EventEnvelope:
        events = self._expect_last(run_id, CONSTRAINTS_EVALUATED)
        proposal = events[-2]
        constraints = events[-1]
        if decision.proposal_id != _required_text(proposal.payload, "proposal_id"):
            raise RunProtocolIntegrityError("authorization belongs to a different proposal")
        if decision.constraint_evaluation_id != _required_text(
            constraints.payload, "evaluation_id"
        ):
            raise RunProtocolIntegrityError(
                "authorization belongs to a different constraint evaluation"
            )
        artifact = self._put_link(
            encode_authorization_decision(decision),
            media_type=AUTHORIZATION_DECISION_MEDIA_TYPE,
            schema_version=decision.schema_version,
            logical_id=decision.decision_id,
        )
        return self._append(
            run_id,
            AUTHORIZATION_DECIDED,
            {
                "run_id": run_id,
                "decision_id": decision.decision_id,
                "proposal_id": decision.proposal_id,
                "constraint_evaluation_id": decision.constraint_evaluation_id,
                "outcome": decision.outcome.value,
                "artifact": artifact.as_payload(),
            },
            events=events,
        )

    def record_execution(self, run_id: str, result: ExecutionResult) -> EventEnvelope:
        events = self._expect_last(run_id, AUTHORIZATION_DECIDED)
        authorization = events[-1]
        if _required_text(authorization.payload, "outcome") != "allow":
            raise RunProtocolIntegrityError("only an allowed run may record execution")
        if result.authorization_decision_id != _required_text(authorization.payload, "decision_id"):
            raise RunProtocolIntegrityError("execution belongs to a different authorization")
        artifact = self._existing_link(
            result.result_id,
            media_type=_EXECUTION_MEDIA_TYPE,
            schema_version=result.schema_version,
            logical_id=result.result_id,
        )
        return self._append(
            run_id,
            EXECUTION_RECORDED,
            {
                "run_id": run_id,
                "result_id": result.result_id,
                "invocation_id": result.invocation_id,
                "authorization_decision_id": result.authorization_decision_id,
                "execution_identity_digest": result.execution_identity_digest,
                "status": result.status.value,
                "reconciled": result.reconciled,
                "artifact": artifact.as_payload(),
            },
            events=events,
        )

    def complete(self, run_id: str, outcome: RunOutcome) -> RunTerminal:
        if outcome is RunOutcome.FAILED:
            raise ValueError("failed runs must use fail()")
        events = self._events_for(run_id)
        if events[-1].event_type in _TERMINALS:
            raise RunAlreadyExists(f"run {run_id!r} is already terminal")
        if events[-1].event_type not in {AUTHORIZATION_DECIDED, EXECUTION_RECORDED, TRACE_RECORDED}:
            raise RunProtocolIntegrityError("run cannot complete before authorization")
        if not any(event.event_type == AUTHORIZATION_DECIDED for event in events):
            raise RunProtocolIntegrityError("run cannot complete without authorization")
        authorization_outcome, execution_status = self._validate_outcome(events, outcome)
        trace = self._ensure_trace(run_id, outcome, events)
        events = self._events_for(run_id)
        terminal = self._append(
            run_id,
            RUN_COMPLETED,
            {
                "run_id": run_id,
                "outcome": outcome.value,
                "authorization_outcome": authorization_outcome,
                "execution_status": execution_status,
                "trace_artifact_digest": _artifact_digest(trace),
            },
            events=events,
        )
        return RunTerminal(trace, terminal)

    def fail(self, run_id: str, *, phase: str, error_type: str) -> RunTerminal:
        if not phase.strip() or not error_type.strip():
            raise ValueError("failure phase and error type must not be empty")
        events = self._events_for(run_id)
        if events[-1].event_type in _TERMINALS:
            raise RunAlreadyExists(f"run {run_id!r} is already terminal")
        failure = self._put_link(
            canonical_json_bytes(
                {
                    "schema_version": RUN_FAILURE_SCHEMA_VERSION,
                    "run_id": run_id,
                    "phase": phase,
                    "error_type": error_type,
                }
            ),
            media_type=RUN_FAILURE_MEDIA_TYPE,
            schema_version=RUN_FAILURE_SCHEMA_VERSION,
            logical_id=f"failure:{run_id}",
        )
        trace = self._ensure_trace(run_id, RunOutcome.FAILED, events)
        events = self._events_for(run_id)
        terminal = self._append(
            run_id,
            RUN_FAILED,
            {
                "run_id": run_id,
                "outcome": RunOutcome.FAILED.value,
                "phase": phase,
                "error_type": error_type,
                "artifact": failure.as_payload(),
                "trace_artifact_digest": _artifact_digest(trace),
            },
            events=events,
        )
        return RunTerminal(trace, terminal)

    def _ensure_trace(
        self,
        run_id: str,
        outcome: RunOutcome,
        events: tuple[EventEnvelope, ...],
    ) -> EventEnvelope:
        if events[-1].event_type == TRACE_RECORDED:
            if _required_text(events[-1].payload, "outcome") != outcome.value:
                raise RunProtocolIntegrityError("recorded trace has a different outcome")
            return events[-1]
        entries = _trace_entries(events)
        artifact = self._put_link(
            canonical_json_bytes(
                {
                    "schema_version": RUN_TRACE_SCHEMA_VERSION,
                    "run_id": run_id,
                    "run_stream_id": run_stream_id(run_id),
                    "outcome": outcome.value,
                    "entries": entries,
                }
            ),
            media_type=RUN_TRACE_MEDIA_TYPE,
            schema_version=RUN_TRACE_SCHEMA_VERSION,
            logical_id=f"trace:{run_id}",
        )
        return self._append(
            run_id,
            TRACE_RECORDED,
            {
                "run_id": run_id,
                "outcome": outcome.value,
                "entry_count": len(entries),
                "artifact": artifact.as_payload(),
            },
            events=events,
        )

    def _validate_outcome(
        self,
        events: tuple[EventEnvelope, ...],
        outcome: RunOutcome,
    ) -> tuple[str, str | None]:
        authorization = next(event for event in events if event.event_type == AUTHORIZATION_DECIDED)
        execution = next(
            (event for event in events if event.event_type == EXECUTION_RECORDED), None
        )
        authorization_outcome = _required_text(authorization.payload, "outcome")
        execution_status = (
            None if execution is None else _required_text(execution.payload, "status")
        )
        expected = {
            RunOutcome.EXECUTED: ("allow", "succeeded"),
            RunOutcome.DENIED: ("deny", None),
            RunOutcome.APPROVAL_REQUIRED: ("require-approval", None),
            RunOutcome.EXECUTION_FAILED: ("allow", "failed"),
            RunOutcome.REQUIRES_RECONCILIATION: ("allow", "unknown"),
        }[outcome]
        if (authorization_outcome, execution_status) != expected:
            raise RunProtocolIntegrityError(
                f"run outcome {outcome.value!r} does not match authorization/execution"
            )
        return authorization_outcome, execution_status

    def _append(
        self,
        run_id: str,
        event_type: str,
        payload: Mapping[str, JsonInput],
        *,
        events: tuple[EventEnvelope, ...],
    ) -> EventEnvelope:
        if not events:
            raise RunProtocolIntegrityError("run must start before recording stages")
        if events[-1].event_type in _TERMINALS:
            raise RunAlreadyExists(f"run {run_id!r} is already terminal")
        timestamp = self._clock()
        event = EventEnvelope.create(
            stream_id=run_stream_id(run_id),
            stream_sequence=len(events) + 1,
            event_type=event_type,
            schema_version=RUN_EVENT_SCHEMA_VERSION,
            actor=events[0].actor,
            source=_SOURCE,
            payload=payload,
            recorded_at=timestamp,
            effective_at=timestamp,
            correlation_id=run_id,
            causation_id=events[-1].event_id,
        )
        stored = self._events.append(event, expected_sequence=len(events))
        self._validate_events(run_id, (*events, stored))
        return stored

    def _expect_last(self, run_id: str, event_type: str) -> tuple[EventEnvelope, ...]:
        events = self._events_for(run_id)
        if events[-1].event_type in _TERMINALS:
            raise RunAlreadyExists(f"run {run_id!r} is already terminal")
        if events[-1].event_type != event_type:
            raise RunProtocolIntegrityError(
                f"run stage requires {event_type!r}, found {events[-1].event_type!r}"
            )
        return events

    def _events_for(self, run_id: str) -> tuple[EventEnvelope, ...]:
        events = self._events.read_stream(run_stream_id(run_id))
        if not events:
            raise RunProtocolIntegrityError(f"run {run_id!r} has not started")
        self._validate_events(run_id, events)
        return events

    def _validate_events(self, run_id: str, events: Sequence[EventEnvelope]) -> None:
        traced = False
        terminal = False
        material_index = 0
        execution_seen = False
        for index, event in enumerate(events, start=1):
            if event.stream_id != run_stream_id(run_id):
                raise RunProtocolIntegrityError("run event belongs to a different stream")
            if event.stream_sequence != index:
                raise RunProtocolIntegrityError("run event sequences are not contiguous")
            if event.correlation_id != run_id:
                raise RunProtocolIntegrityError("run event correlation does not match run_id")
            if event.schema_version != RUN_EVENT_SCHEMA_VERSION:
                raise RunProtocolIntegrityError("run event schema version is unsupported")
            if _required_text(event.payload, "run_id") != run_id:
                raise RunProtocolIntegrityError("run event payload does not match run_id")
            expected_cause = None if index == 1 else events[index - 2].event_id
            if event.causation_id != expected_cause:
                raise RunProtocolIntegrityError(
                    "run events must form one immediate-predecessor causation chain"
                )
            if terminal:
                raise RunProtocolIntegrityError("events cannot follow a terminal run event")
            if event.event_type == TRACE_RECORDED:
                if traced or material_index < 1:
                    raise RunProtocolIntegrityError("trace event is duplicated or premature")
                traced = True
                self._validate_artifact_link(event)
                outcome = _run_outcome(event.payload)
                self._validate_trace_manifest(
                    event,
                    run_id=run_id,
                    outcome=outcome,
                    prior_events=events[: index - 1],
                )
                continue
            if event.event_type in _TERMINALS:
                if not traced or index != len(events):
                    raise RunProtocolIntegrityError("terminal event requires the final trace")
                terminal = True
                trace_outcome = _required_text(events[index - 2].payload, "outcome")
                terminal_outcome = _required_text(event.payload, "outcome")
                if terminal_outcome != trace_outcome:
                    raise RunProtocolIntegrityError(
                        "terminal outcome does not match the recorded trace"
                    )
                if _required_text(event.payload, "trace_artifact_digest") != _artifact_digest(
                    events[index - 2]
                ):
                    raise RunProtocolIntegrityError(
                        "terminal trace reference does not match the recorded trace"
                    )
                if event.event_type == RUN_FAILED:
                    if terminal_outcome != RunOutcome.FAILED.value:
                        raise RunProtocolIntegrityError("run.failed requires failed outcome")
                    self._validate_artifact_link(event)
                elif terminal_outcome == RunOutcome.FAILED.value:
                    raise RunProtocolIntegrityError("run.completed cannot have failed outcome")
                continue
            if traced:
                raise RunProtocolIntegrityError("material events cannot follow the run trace")
            if event.event_type == EXECUTION_RECORDED:
                if material_index != len(_MATERIAL_ORDER) or execution_seen:
                    raise RunProtocolIntegrityError("execution event is out of order")
                execution_seen = True
                self._validate_artifact_link(event)
                continue
            if material_index >= len(_MATERIAL_ORDER):
                raise RunProtocolIntegrityError("unexpected material run event")
            if event.event_type != _MATERIAL_ORDER[material_index]:
                raise RunProtocolIntegrityError(f"run event {event.event_type!r} is out of order")
            material_index += 1
            if event.event_type == RUN_STARTED:
                if (
                    _required_text(event.payload, "workflow") != RUN_WORKFLOW
                    or _required_text(event.payload, "workflow_version") != RUN_WORKFLOW_VERSION
                ):
                    raise RunProtocolIntegrityError("run start workflow contract is unsupported")
                _required_text(event.payload, "request_digest")
            else:
                self._validate_artifact_link(event)

    def _raise_existing_start(
        self,
        command: RunStart,
        existing: Sequence[EventEnvelope],
    ) -> None:
        if not existing:
            raise RunInterrupted(f"run {command.run_id!r} changed concurrently but is not readable")
        self._validate_events(command.run_id, existing)
        stored_digest = _required_text(existing[0].payload, "request_digest")
        if stored_digest != command.request_digest:
            raise RunIdentityConflict(
                f"run {command.run_id!r} is bound to a different request digest"
            )
        if existing[-1].event_type in _TERMINALS:
            raise RunAlreadyExists(f"run {command.run_id!r} is already terminal")
        raise RunInterrupted(
            f"run {command.run_id!r} is nonterminal and requires explicit recovery"
        )

    def _validate_artifact_link(self, event: EventEnvelope) -> None:
        artifact = event.payload.get("artifact")
        if not isinstance(artifact, Mapping):
            raise RunProtocolIntegrityError(
                f"run event {event.event_type!r} lacks an artifact link"
            )
        link = cast("Mapping[str, object]", artifact)
        digest = _required_text(link, "digest")
        media_type = _required_text(link, "media_type")
        encoding = link.get("encoding")
        if encoding is not None and not isinstance(encoding, str):
            raise RunProtocolIntegrityError("artifact encoding must be a string or null")
        size = link.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RunProtocolIntegrityError("artifact size must be a non-negative integer")
        _required_text(link, "schema_version")
        _required_text(link, "logical_id")
        try:
            reference = self._artifacts.stat(digest)
            self._artifacts.verify(reference)
        except (ArtifactIntegrityError, ArtifactNotFoundError, ValueError) as error:
            raise RunProtocolIntegrityError(
                f"run artifact {digest!r} is missing or corrupt"
            ) from error
        if (
            reference.media_type != media_type
            or reference.encoding != encoding
            or reference.size_bytes != size
        ):
            raise RunProtocolIntegrityError(
                f"run artifact {digest!r} metadata does not match its event link"
            )

    def _validate_trace_manifest(
        self,
        event: EventEnvelope,
        *,
        run_id: str,
        outcome: RunOutcome,
        prior_events: Sequence[EventEnvelope],
    ) -> None:
        digest = _artifact_digest(event)
        expected = {
            "schema_version": RUN_TRACE_SCHEMA_VERSION,
            "run_id": run_id,
            "run_stream_id": run_stream_id(run_id),
            "outcome": outcome.value,
            "entries": _trace_entries(prior_events),
        }
        if self._artifacts.get_json(digest) != expected:
            raise RunProtocolIntegrityError(
                f"run trace artifact {digest!r} does not match its event prefix"
            )

    def _put_link(
        self,
        data: bytes,
        *,
        media_type: str,
        schema_version: str,
        logical_id: str,
    ) -> RunArtifactLink:
        reference = self._artifacts.put_bytes(
            data,
            media_type=media_type,
            encoding="utf-8",
        )
        if reference.media_type != media_type or reference.encoding != "utf-8":
            raise RunProtocolIntegrityError(
                "artifact content address is already bound to incompatible metadata"
            )
        if self._artifacts.get_bytes(reference, verify=True) != data:
            raise RunProtocolIntegrityError("artifact verification returned different bytes")
        return _link(reference, schema_version=schema_version, logical_id=logical_id)

    def _existing_link(
        self,
        digest: str,
        *,
        media_type: str,
        schema_version: str,
        logical_id: str,
    ) -> RunArtifactLink:
        try:
            reference = self._artifacts.stat(digest)
            if reference.media_type != media_type or reference.encoding != "utf-8":
                raise RunProtocolIntegrityError(
                    f"artifact {digest!r} has incompatible type or encoding"
                )
            self._artifacts.verify(reference)
        except (ArtifactIntegrityError, ArtifactNotFoundError, ValueError) as error:
            raise RunProtocolIntegrityError(f"artifact {digest!r} is missing or corrupt") from error
        return _link(reference, schema_version=schema_version, logical_id=logical_id)


def _link(reference: ArtifactRef, *, schema_version: str, logical_id: str) -> RunArtifactLink:
    return RunArtifactLink(
        digest=reference.digest,
        media_type=reference.media_type,
        encoding=reference.encoding,
        size_bytes=reference.size_bytes,
        schema_version=schema_version,
        logical_id=logical_id,
    )


def _required_text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RunProtocolIntegrityError(f"run event {field!r} must be a non-empty string")
    return value


def _optional_artifact_digest(payload: Mapping[str, object]) -> str | None:
    artifact = payload.get("artifact")
    if artifact is None:
        return None
    if not isinstance(artifact, Mapping):
        raise RunProtocolIntegrityError("run event artifact link must be an object")
    return _required_text(cast("Mapping[str, object]", artifact), "digest")


def _artifact_digest(event: EventEnvelope) -> str:
    digest = _optional_artifact_digest(event.payload)
    if digest is None:
        raise RunProtocolIntegrityError("run trace event lacks its artifact")
    return digest


def _run_outcome(payload: Mapping[str, object]) -> RunOutcome:
    value = _required_text(payload, "outcome")
    try:
        return RunOutcome(value)
    except ValueError as error:
        raise RunProtocolIntegrityError(f"run outcome {value!r} is not recognized") from error


def _trace_entries(events: Sequence[EventEnvelope]) -> list[dict[str, JsonInput]]:
    return [
        {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "schema_version": event.schema_version,
            "stream_sequence": event.stream_sequence,
            "global_position": event.global_position,
            "recorded_at": event.recorded_at.isoformat(),
            "effective_at": event.effective_at.isoformat(),
            "causation_id": event.causation_id,
            "artifact_digest": _optional_artifact_digest(event.payload),
        }
        for event in events
    ]
