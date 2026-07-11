from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from blackcell.kernel import EventEnvelope
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
    RUN_EVENT_SCHEMA_VERSION_V1,
    RUN_EVENT_SCHEMA_VERSION_V2,
    RUN_FAILED,
    RUN_STARTED,
    RUN_WORKFLOW,
    RUN_WORKFLOW_VERSION_V1,
    RUN_WORKFLOW_VERSION_V2,
    STATE_TRANSITION_RECORDED,
    TRACE_RECORDED,
    RunOutcome,
    RunProtocolIntegrityError,
    RunProtocolVersion,
    run_stream_id,
)

_TERMINALS = frozenset({RUN_COMPLETED, RUN_FAILED})


@dataclass(frozen=True, slots=True)
class RunGrammar:
    run_id: str
    protocol_version: RunProtocolVersion
    terminal: bool
    failed: bool


def validate_run_grammar(
    events: Sequence[EventEnvelope],
    *,
    run_id: str | None = None,
) -> RunGrammar:
    """Validate causal structure without decoding feature-owned artifacts.

    Artifact ownership and cross-artifact identity remain adapter responsibilities.
    This pure validator is safe for historical replay and supports incomplete prefixes.
    """

    if not events:
        raise RunProtocolIntegrityError("run history must not be empty")
    start = events[0]
    if start.event_type != RUN_STARTED:
        raise RunProtocolIntegrityError("run history must start with run.started")
    resolved_run_id = run_id or _text(start.payload, "run_id")
    if not resolved_run_id.strip():
        raise RunProtocolIntegrityError("run_id must not be empty")
    version = _protocol_version(start)
    expected_schema = version.event_schema_version
    expected_stream = run_stream_id(resolved_run_id)
    terminal = False
    for index, event in enumerate(events, start=1):
        if event.stream_id != expected_stream:
            raise RunProtocolIntegrityError("run event belongs to a different stream")
        if event.stream_sequence != index:
            raise RunProtocolIntegrityError("run event sequences are not contiguous")
        if event.correlation_id != resolved_run_id:
            raise RunProtocolIntegrityError("run event correlation does not match run_id")
        if event.schema_version != expected_schema:
            raise RunProtocolIntegrityError("run history mixes or uses unsupported event schemas")
        if _text(event.payload, "run_id") != resolved_run_id:
            raise RunProtocolIntegrityError("run event payload does not match run_id")
        expected_cause = None if index == 1 else events[index - 2].event_id
        if event.causation_id != expected_cause:
            raise RunProtocolIntegrityError(
                "run events must form one immediate-predecessor causation chain"
            )
        if terminal:
            raise RunProtocolIntegrityError("events cannot follow a terminal run event")
        terminal = event.event_type in _TERMINALS
    if version is RunProtocolVersion.V1:
        _validate_v1(events)
    else:
        _validate_v2(events)
    final = events[-1]
    return RunGrammar(
        resolved_run_id,
        version,
        final.event_type in _TERMINALS,
        final.event_type == RUN_FAILED,
    )


def _protocol_version(start: EventEnvelope) -> RunProtocolVersion:
    if _text(start.payload, "workflow") != RUN_WORKFLOW:
        raise RunProtocolIntegrityError("run start workflow is unsupported")
    value = _text(start.payload, "workflow_version")
    try:
        version = RunProtocolVersion(value)
    except ValueError as error:
        raise RunProtocolIntegrityError(f"run workflow version {value!r} is unsupported") from error
    expected = {
        RUN_WORKFLOW_VERSION_V1: RUN_EVENT_SCHEMA_VERSION_V1,
        RUN_WORKFLOW_VERSION_V2: RUN_EVENT_SCHEMA_VERSION_V2,
    }[version.value]
    if start.schema_version != expected:
        raise RunProtocolIntegrityError("run workflow and event schema versions disagree")
    return version


def _validate_v1(events: Sequence[EventEnvelope]) -> None:
    state = "start"
    authorization: str | None = None
    execution: str | None = None
    for event in events[1:]:
        if event.event_type == TRACE_RECORDED:
            if state == "trace":
                raise RunProtocolIntegrityError("v1 trace event is duplicated")
            outcome = _outcome(event)
            if outcome is not RunOutcome.FAILED:
                if state not in {"authorization", "execution"}:
                    raise RunProtocolIntegrityError("v1 successful trace is premature")
                _validate_v1_outcome(outcome, authorization, execution)
            state = "trace"
            continue
        if event.event_type in _TERMINALS:
            _validate_terminal(event, state, events)
            state = "terminal"
            continue
        expected = {
            "start": CONTEXT_RECORDED,
            "context": PROPOSAL_RECORDED,
            "proposal": CONSTRAINTS_EVALUATED,
            "constraints": AUTHORIZATION_DECIDED,
            "authorization": EXECUTION_RECORDED,
        }.get(state)
        if event.event_type != expected:
            raise RunProtocolIntegrityError(f"v1 event {event.event_type!r} is out of order")
        state = {
            CONTEXT_RECORDED: "context",
            PROPOSAL_RECORDED: "proposal",
            CONSTRAINTS_EVALUATED: "constraints",
            AUTHORIZATION_DECIDED: "authorization",
            EXECUTION_RECORDED: "execution",
        }[event.event_type]
        if event.event_type == AUTHORIZATION_DECIDED:
            authorization = _text(event.payload, "outcome")
        elif event.event_type == EXECUTION_RECORDED:
            if authorization != "allow":
                raise RunProtocolIntegrityError("v1 execution requires allowed authorization")
            execution = _text(event.payload, "status")


def _validate_v1_outcome(
    outcome: RunOutcome,
    authorization: str | None,
    execution: str | None,
) -> None:
    expected = {
        RunOutcome.EXECUTED: ("allow", "succeeded"),
        RunOutcome.DENIED: ("deny", None),
        RunOutcome.APPROVAL_REQUIRED: ("require-approval", None),
        RunOutcome.EXECUTION_FAILED: ("allow", "failed"),
        RunOutcome.REQUIRES_RECONCILIATION: ("allow", "unknown"),
    }.get(outcome)
    if expected is None or (authorization, execution) != expected:
        raise RunProtocolIntegrityError("v1 trace outcome does not match its material prefix")


def _validate_v2(events: Sequence[EventEnvelope]) -> None:
    state = "start"
    attempts = 0
    authorization: str | None = None
    execution: str | None = None
    outcome_state_seen = False
    for event in events[1:]:
        if event.event_type == TRACE_RECORDED:
            if state == "trace":
                raise RunProtocolIntegrityError("v2 trace event is duplicated")
            outcome = _outcome(event)
            if outcome is not RunOutcome.FAILED:
                if state not in {"evaluation", "transition"}:
                    raise RunProtocolIntegrityError("v2 successful trace requires evaluation")
                _validate_v2_outcome(outcome, authorization, execution)
            state = "trace"
            continue
        if event.event_type in _TERMINALS:
            _validate_terminal(event, state, events)
            state = "terminal"
            continue
        if state == "start":
            _require(event, EVALUATION_SPECIFIED)
            state = "evaluation-spec"
        elif state == "evaluation-spec":
            _require(event, INITIAL_STATE_RECORDED)
            state = "initial-state"
        elif state == "initial-state":
            _require(event, CONTEXT_RECORDED)
            state = "context"
        elif state == "context":
            _require(event, MODEL_REQUESTED)
            state = "model-request"
        elif state == "model-request":
            if event.event_type == MODEL_ATTEMPT_RECORDED:
                attempts = 1
                state = "model-attempt"
            elif event.event_type == MODEL_FAILED:
                state = "model-failed"
            else:
                _unexpected_v2(event)
        elif state == "model-attempt":
            if event.event_type == MODEL_ATTEMPT_RECORDED:
                attempts += 1
            elif event.event_type == MODEL_RESPONDED:
                if attempts < 1:  # pragma: no cover - state establishes this
                    raise RunProtocolIntegrityError("model response requires an attempt")
                state = "model-response"
            elif event.event_type == MODEL_FAILED:
                state = "model-failed"
            else:
                _unexpected_v2(event)
        elif state == "model-response":
            _require(event, PROPOSAL_RECORDED)
            state = "proposal"
        elif state == "proposal":
            _require(event, CONSTRAINTS_EVALUATED)
            state = "constraints"
        elif state == "constraints":
            _require(event, AUTHORIZATION_DECIDED)
            authorization = _text(event.payload, "outcome")
            if authorization not in {"allow", "deny", "require-approval"}:
                raise RunProtocolIntegrityError("v2 authorization outcome is unsupported")
            state = "authorization"
        elif state == "authorization":
            if authorization == "allow":
                _require(event, EXECUTION_RECORDED)
                execution = _text(event.payload, "status")
                state = "execution"
            else:
                _require(event, EVALUATION_RECORDED)
                state = "evaluation"
        elif state == "execution":
            if execution == "unknown":
                _require(event, EVALUATION_RECORDED)
                state = "evaluation"
            elif execution in {"succeeded", "failed"}:
                _require(event, OUTCOME_OBSERVED)
                state = "outcome"
            else:
                raise RunProtocolIntegrityError("v2 execution status is unsupported")
        elif state == "outcome":
            _require(event, OUTCOME_STATE_RECORDED)
            outcome_state_seen = True
            state = "outcome-state"
        elif state == "outcome-state":
            _require(event, EVALUATION_RECORDED)
            state = "evaluation"
        elif state == "evaluation":
            if event.event_type != STATE_TRANSITION_RECORDED or not outcome_state_seen:
                _unexpected_v2(event)
            state = "transition"
        elif state in {"model-failed", "transition", "trace", "terminal"}:
            _unexpected_v2(event)
        else:  # pragma: no cover - exhaustive internal state
            raise AssertionError(f"unknown v2 state {state!r}")


def _validate_terminal(
    event: EventEnvelope,
    state: str,
    events: Sequence[EventEnvelope],
) -> None:
    if state != "trace":
        raise RunProtocolIntegrityError("terminal event requires the final trace")
    if event is not events[-1]:
        raise RunProtocolIntegrityError("terminal event must end the run stream")
    trace = events[-2]
    trace_outcome = _outcome(trace)
    terminal_outcome = _outcome(event)
    if trace_outcome is not terminal_outcome:
        raise RunProtocolIntegrityError("terminal outcome does not match the recorded trace")
    if event.event_type == RUN_FAILED and terminal_outcome is not RunOutcome.FAILED:
        raise RunProtocolIntegrityError("run.failed requires failed outcome")
    if event.event_type == RUN_COMPLETED and terminal_outcome is RunOutcome.FAILED:
        raise RunProtocolIntegrityError("run.completed cannot have failed outcome")


def _validate_v2_outcome(
    outcome: RunOutcome,
    authorization: str | None,
    execution: str | None,
) -> None:
    expected = {
        RunOutcome.EXECUTED: ("allow", "succeeded"),
        RunOutcome.DENIED: ("deny", None),
        RunOutcome.APPROVAL_REQUIRED: ("require-approval", None),
        RunOutcome.EXECUTION_FAILED: ("allow", "failed"),
        RunOutcome.REQUIRES_RECONCILIATION: ("allow", "unknown"),
    }.get(outcome)
    if expected is None or (authorization, execution) != expected:
        raise RunProtocolIntegrityError("v2 trace outcome does not match its material prefix")


def _require(event: EventEnvelope, expected: str) -> None:
    if event.event_type != expected:
        raise RunProtocolIntegrityError(
            f"v2 event {event.event_type!r} is out of order; expected {expected!r}"
        )


def _unexpected_v2(event: EventEnvelope) -> None:
    raise RunProtocolIntegrityError(f"v2 event {event.event_type!r} is out of order")


def _outcome(event: EventEnvelope) -> RunOutcome:
    value = _text(event.payload, "outcome")
    try:
        return RunOutcome(value)
    except ValueError as error:
        raise RunProtocolIntegrityError(f"run outcome {value!r} is not recognized") from error


def _text(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RunProtocolIntegrityError(f"run event {name!r} must be a non-empty string")
    return value
