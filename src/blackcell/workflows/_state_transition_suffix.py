from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, cast

from blackcell.features.accept_state_transition import (
    ACCEPTED_STATE_TRANSITION_MEDIA_TYPE,
    ACCEPTED_STATE_TRANSITION_SCHEMA_VERSION,
    AcceptStateTransition,
    TransitionAcceptance,
    decode_accepted_state_transition,
)
from blackcell.kernel import EventEnvelope, JsonInput
from blackcell.kernel._json import canonical_json_bytes
from blackcell.workflows.run_protocol import (
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_FAILURE_MEDIA_TYPE,
    RUN_FAILURE_SCHEMA_VERSION,
    RUN_TRACE_MEDIA_TYPE,
    RUN_TRACE_SCHEMA_VERSION_V2,
    STATE_TRANSITION_RECORDED,
    TRACE_RECORDED,
    RunOutcome,
    run_stream_id,
)

from ._state_transition_errors import StateTransitionBindingError
from ._state_transition_integrity import _Artifact, _artifact, _matches, _text

if TYPE_CHECKING:
    from .state_transition import StateTransitionArtifacts


def _verify_recorded_suffix(
    run_id: str,
    events: tuple[EventEnvelope, ...],
    *,
    command: AcceptStateTransition,
    acceptance: TransitionAcceptance,
    artifacts: StateTransitionArtifacts,
) -> None:
    by_type = {event.event_type: event for event in events}
    transition_event = by_type.get(STATE_TRANSITION_RECORDED)
    if transition_event is not None:
        expected = acceptance.transition
        if expected is None:
            raise StateTransitionBindingError(
                "recorded transition has no derived accepted transition"
            )
        link = _artifact(
            transition_event,
            artifacts=artifacts,
            media_type=ACCEPTED_STATE_TRANSITION_MEDIA_TYPE,
            schema_version=ACCEPTED_STATE_TRANSITION_SCHEMA_VERSION,
        )
        recorded = decode_accepted_state_transition(link.data)
        _matches(
            transition_event,
            {
                "transition_id": recorded.transition_id,
                "initial_snapshot_digest": recorded.initial_state.snapshot_digest,
                "outcome_snapshot_digest": recorded.outcome_state.snapshot_digest,
                "evaluation_id": recorded.evaluation.evaluation_id,
                "accepted_claim_ids": recorded.accepted_claim_ids,
                "accepted_source_event_ids": recorded.accepted_source_event_ids,
            },
        )
        if link.logical_id != recorded.transition_id or recorded != expected:
            raise StateTransitionBindingError(
                "recorded transition differs from derived accepted evidence"
            )

    trace_event = by_type.get(TRACE_RECORDED)
    trace_link: _Artifact | None = None
    if trace_event is not None:
        trace_outcome = _text(trace_event.payload, "outcome")
        if (
            trace_outcome != RunOutcome.FAILED.value
            and acceptance.transition is not None
            and transition_event is None
        ):
            raise StateTransitionBindingError(
                "completed trace omits its derived accepted transition"
            )
        trace_link = _artifact(
            trace_event,
            artifacts=artifacts,
            media_type=RUN_TRACE_MEDIA_TYPE,
            schema_version=RUN_TRACE_SCHEMA_VERSION_V2,
        )
        prior = tuple(
            event for event in events if event.stream_sequence < trace_event.stream_sequence
        )
        expected_trace = {
            "schema_version": RUN_TRACE_SCHEMA_VERSION_V2,
            "run_id": run_id,
            "run_stream_id": run_stream_id(run_id),
            "outcome": trace_outcome,
            "entries": _trace_entries(prior),
        }
        _matches(
            trace_event,
            {
                "run_id": run_id,
                "entry_count": len(prior),
            },
        )
        if trace_link.logical_id != f"trace:{run_id}" or trace_link.data != canonical_json_bytes(
            expected_trace
        ):
            raise StateTransitionBindingError("recorded trace differs from its exact run prefix")

    terminal = next(
        (event for event in events if event.event_type in {RUN_COMPLETED, RUN_FAILED}),
        None,
    )
    if terminal is None:
        return
    if trace_event is None or trace_link is None:
        raise StateTransitionBindingError("terminal run lacks its verified trace")
    trace_outcome = _text(trace_event.payload, "outcome")
    if terminal.event_type == RUN_COMPLETED:
        material_outcome = _material_outcome(command)
        _matches(
            terminal,
            {
                "run_id": run_id,
                "outcome": material_outcome.value,
                "authorization_outcome": command.authorization.outcome.value,
                "execution_status": (
                    None if command.execution is None else command.execution.status.value
                ),
                "trace_artifact_digest": trace_link.digest,
            },
        )
        if trace_outcome != material_outcome.value:
            raise StateTransitionBindingError(
                "completed trace outcome differs from derived run evidence"
            )
        return

    _matches(
        terminal,
        {
            "run_id": run_id,
            "outcome": RunOutcome.FAILED.value,
            "trace_artifact_digest": trace_link.digest,
        },
    )
    if trace_outcome != RunOutcome.FAILED.value:
        raise StateTransitionBindingError("failed terminal requires a failed trace")
    failure_link = _artifact(
        terminal,
        artifacts=artifacts,
        media_type=RUN_FAILURE_MEDIA_TYPE,
        schema_version=RUN_FAILURE_SCHEMA_VERSION,
    )
    phase = _text(terminal.payload, "phase")
    error_type = _text(terminal.payload, "error_type")
    expected_failure = canonical_json_bytes(
        {
            "schema_version": RUN_FAILURE_SCHEMA_VERSION,
            "run_id": run_id,
            "phase": phase,
            "error_type": error_type,
        }
    )
    if failure_link.logical_id != f"failure:{run_id}" or failure_link.data != expected_failure:
        raise StateTransitionBindingError("run failure artifact differs from its terminal event")


def _material_outcome(command: AcceptStateTransition) -> RunOutcome:
    execution_status = None if command.execution is None else command.execution.status.value
    outcomes: Mapping[tuple[str, str | None], RunOutcome] = {
        ("deny", None): RunOutcome.DENIED,
        ("require-approval", None): RunOutcome.APPROVAL_REQUIRED,
        ("allow", "succeeded"): RunOutcome.EXECUTED,
        ("allow", "failed"): RunOutcome.EXECUTION_FAILED,
        ("allow", "unknown"): RunOutcome.REQUIRES_RECONCILIATION,
    }
    try:
        return outcomes[(command.authorization.outcome.value, execution_status)]
    except KeyError as error:
        raise StateTransitionBindingError(
            "authorization/execution cannot produce a completed run outcome"
        ) from error


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
            "artifact_links": [
                {
                    "field": field,
                    "digest": _text(cast("Mapping[str, object]", value), "digest"),
                }
                for field, value in sorted(event.payload.items())
                if (field == "artifact" or field.endswith("_artifact"))
                and isinstance(value, Mapping)
            ],
        }
        for event in events
    ]
