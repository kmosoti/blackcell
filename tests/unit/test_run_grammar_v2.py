from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from blackcell.adapters.persistence.sqlite.run_records import KernelRunRecorder
from blackcell.kernel import ArtifactStore, EventEnvelope, EventStore, JsonInput
from blackcell.kernel._json import json_digest
from blackcell.workflows.run_grammar import validate_run_grammar
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
    RunProtocolIntegrityError,
    RunProtocolVersion,
    RunStart,
)

NOW = datetime(2026, 7, 11, 21, tzinfo=UTC)


def test_v1_history_remains_strictly_readable() -> None:
    history = _history(
        RunProtocolVersion.V1,
        (
            RUN_STARTED,
            CONTEXT_RECORDED,
            PROPOSAL_RECORDED,
            CONSTRAINTS_EVALUATED,
            AUTHORIZATION_DECIDED,
            EXECUTION_RECORDED,
            TRACE_RECORDED,
            RUN_COMPLETED,
        ),
        authorization="allow",
        execution="succeeded",
        outcome=RunOutcome.EXECUTED,
    )

    grammar = validate_run_grammar(history)

    assert grammar.protocol_version is RunProtocolVersion.V1
    assert grammar.terminal
    assert not grammar.failed


@pytest.mark.parametrize(
    ("events", "authorization", "execution", "outcome"),
    (
        (
            (
                RUN_STARTED,
                EVALUATION_SPECIFIED,
                INITIAL_STATE_RECORDED,
                CONTEXT_RECORDED,
                MODEL_REQUESTED,
                MODEL_ATTEMPT_RECORDED,
                MODEL_RESPONDED,
                PROPOSAL_RECORDED,
                CONSTRAINTS_EVALUATED,
                AUTHORIZATION_DECIDED,
                EXECUTION_RECORDED,
                OUTCOME_OBSERVED,
                OUTCOME_STATE_RECORDED,
                EVALUATION_RECORDED,
                STATE_TRANSITION_RECORDED,
                TRACE_RECORDED,
                RUN_COMPLETED,
            ),
            "allow",
            "succeeded",
            RunOutcome.EXECUTED,
        ),
        (
            (
                RUN_STARTED,
                EVALUATION_SPECIFIED,
                INITIAL_STATE_RECORDED,
                CONTEXT_RECORDED,
                MODEL_REQUESTED,
                MODEL_ATTEMPT_RECORDED,
                MODEL_RESPONDED,
                PROPOSAL_RECORDED,
                CONSTRAINTS_EVALUATED,
                AUTHORIZATION_DECIDED,
                EVALUATION_RECORDED,
                TRACE_RECORDED,
                RUN_COMPLETED,
            ),
            "deny",
            None,
            RunOutcome.DENIED,
        ),
        (
            (
                RUN_STARTED,
                EVALUATION_SPECIFIED,
                INITIAL_STATE_RECORDED,
                CONTEXT_RECORDED,
                MODEL_REQUESTED,
                MODEL_ATTEMPT_RECORDED,
                MODEL_RESPONDED,
                PROPOSAL_RECORDED,
                CONSTRAINTS_EVALUATED,
                AUTHORIZATION_DECIDED,
                EXECUTION_RECORDED,
                EVALUATION_RECORDED,
                TRACE_RECORDED,
                RUN_COMPLETED,
            ),
            "allow",
            "unknown",
            RunOutcome.REQUIRES_RECONCILIATION,
        ),
    ),
)
def test_v2_completed_branches_are_valid(
    events: tuple[str, ...],
    authorization: str,
    execution: str | None,
    outcome: RunOutcome,
) -> None:
    grammar = validate_run_grammar(
        _history(
            RunProtocolVersion.V2,
            events,
            authorization=authorization,
            execution=execution,
            outcome=outcome,
        )
    )

    assert grammar.protocol_version is RunProtocolVersion.V2
    assert grammar.terminal
    assert not grammar.failed


@pytest.mark.parametrize("attempts", (0, 2))
def test_v2_model_failure_allows_zero_or_multiple_completed_attempts(attempts: int) -> None:
    events = (
        RUN_STARTED,
        EVALUATION_SPECIFIED,
        INITIAL_STATE_RECORDED,
        CONTEXT_RECORDED,
        MODEL_REQUESTED,
        *((MODEL_ATTEMPT_RECORDED,) * attempts),
        MODEL_FAILED,
        TRACE_RECORDED,
        RUN_FAILED,
    )

    grammar = validate_run_grammar(
        _history(
            RunProtocolVersion.V2,
            events,
            outcome=RunOutcome.FAILED,
        )
    )

    assert grammar.terminal
    assert grammar.failed


@pytest.mark.parametrize(
    ("events", "message", "authorization", "execution", "outcome"),
    (
        (
            (RUN_STARTED, INITIAL_STATE_RECORDED),
            "evaluation-specified",
            None,
            None,
            RunOutcome.FAILED,
        ),
        (
            (
                RUN_STARTED,
                EVALUATION_SPECIFIED,
                INITIAL_STATE_RECORDED,
                CONTEXT_RECORDED,
                MODEL_REQUESTED,
                MODEL_RESPONDED,
            ),
            "out of order",
            None,
            None,
            RunOutcome.FAILED,
        ),
        (
            (
                RUN_STARTED,
                EVALUATION_SPECIFIED,
                INITIAL_STATE_RECORDED,
                CONTEXT_RECORDED,
                MODEL_REQUESTED,
                MODEL_FAILED,
                PROPOSAL_RECORDED,
            ),
            "out of order",
            None,
            None,
            RunOutcome.FAILED,
        ),
        (
            (
                RUN_STARTED,
                EVALUATION_SPECIFIED,
                INITIAL_STATE_RECORDED,
                CONTEXT_RECORDED,
                MODEL_REQUESTED,
                MODEL_ATTEMPT_RECORDED,
                MODEL_RESPONDED,
                PROPOSAL_RECORDED,
                CONSTRAINTS_EVALUATED,
                AUTHORIZATION_DECIDED,
                EXECUTION_RECORDED,
                EVALUATION_RECORDED,
                STATE_TRANSITION_RECORDED,
            ),
            "out of order",
            "allow",
            "unknown",
            RunOutcome.REQUIRES_RECONCILIATION,
        ),
    ),
)
def test_v2_rejects_partial_or_semantically_invalid_order(
    events: tuple[str, ...],
    message: str,
    authorization: str | None,
    execution: str | None,
    outcome: RunOutcome,
) -> None:
    history = _history(
        RunProtocolVersion.V2,
        events,
        authorization=authorization,
        execution=execution,
        outcome=outcome,
    )

    with pytest.raises(RunProtocolIntegrityError, match=message):
        validate_run_grammar(history)


def test_v2_rejects_mixed_schema_and_causation_gap() -> None:
    history = _history(
        RunProtocolVersion.V2,
        (RUN_STARTED, EVALUATION_SPECIFIED, INITIAL_STATE_RECORDED),
        outcome=RunOutcome.FAILED,
    )

    with pytest.raises(RunProtocolIntegrityError, match="mixes"):
        validate_run_grammar((history[0], replace(history[1], schema_version=1), history[2]))
    with pytest.raises(RunProtocolIntegrityError, match="causation"):
        validate_run_grammar(
            (
                history[0],
                history[1],
                replace(history[2], causation_id="event:wrong"),
            )
        )


def test_v2_rejects_duplicate_trace() -> None:
    history = _history(
        RunProtocolVersion.V2,
        (
            RUN_STARTED,
            EVALUATION_SPECIFIED,
            INITIAL_STATE_RECORDED,
            CONTEXT_RECORDED,
            TRACE_RECORDED,
            TRACE_RECORDED,
            RUN_FAILED,
        ),
        outcome=RunOutcome.FAILED,
    )

    with pytest.raises(RunProtocolIntegrityError, match="duplicated"):
        validate_run_grammar(history)


def test_v2_writer_stays_inactive_until_full_composition(tmp_path: Path) -> None:
    database = tmp_path / "kernel.sqlite3"
    recorder = KernelRunRecorder(
        EventStore(database),
        ArtifactStore(tmp_path / "artifacts", database_path=database),
        clock=lambda: NOW,
    )

    with pytest.raises(RunProtocolIntegrityError, match="not active"):
        recorder.start(_start(RunProtocolVersion.V2))

    assert EventStore(database).read_stream("daily-operator-run:run:1") == ()


def test_run_start_requires_typed_protocol_version() -> None:
    with pytest.raises(TypeError, match="RunProtocolVersion"):
        replace(_start(RunProtocolVersion.V1), protocol_version="daily-operator/v2")


def _start(version: RunProtocolVersion) -> RunStart:
    return RunStart(
        "run:1",
        json_digest({"request": "one"}),
        "operator",
        "task:daily",
        "inspect status",
        "repository",
        "observations:daily",
        version,
    )


def _history(
    version: RunProtocolVersion,
    event_types: tuple[str, ...],
    *,
    authorization: str | None = None,
    execution: str | None = None,
    outcome: RunOutcome,
) -> tuple[EventEnvelope, ...]:
    events: list[EventEnvelope] = []
    for sequence, event_type in enumerate(event_types, start=1):
        payload: dict[str, JsonInput] = {"run_id": "run:1"}
        if event_type == RUN_STARTED:
            payload.update(
                {
                    "request_digest": json_digest({"request": "one"}),
                    "workflow": "daily-operator",
                    "workflow_version": version.value,
                }
            )
        elif event_type == AUTHORIZATION_DECIDED:
            payload["outcome"] = authorization
        elif event_type == EXECUTION_RECORDED:
            payload["status"] = execution
        elif event_type in {TRACE_RECORDED, RUN_COMPLETED, RUN_FAILED}:
            payload["outcome"] = outcome.value
        event = EventEnvelope.create(
            stream_id="daily-operator-run:run:1",
            stream_sequence=sequence,
            event_type=event_type,
            schema_version=version.event_schema_version,
            actor="operator",
            source="test.run-grammar",
            payload=payload,
            recorded_at=NOW,
            effective_at=NOW,
            correlation_id="run:1",
            causation_id=None if not events else events[-1].event_id,
        )
        events.append(event)
    return tuple(events)
