from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Protocol

from blackcell.features.build_context import ContextFrame, ContextFrameBuilder
from blackcell.features.derive_signal_packet import SignalPacketProjector
from blackcell.features.ingest_observation.events import observation_events
from blackcell.features.project_operational_state import OperationalBeliefState
from blackcell.features.retrieve_evidence import DeterministicEvidenceRetriever
from blackcell.kernel import EventEnvelope
from blackcell.workflows.daily_operator_v2_request import DailyOperatorV2Request


class DailyOperatorV2EvidenceError(ValueError):
    """Ledger evidence differs from the immutable Daily Operator request."""


class DailyOperatorV2EvidenceHistory(Protocol):
    def read_stream(
        self,
        stream_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> Sequence[EventEnvelope]: ...

    def read_all(
        self,
        *,
        after_position: int = 0,
        limit: int | None = None,
    ) -> Sequence[EventEnvelope]: ...

    def get(self, event_id: str) -> EventEnvelope | None: ...


def verify_requested_ingestion(
    request: DailyOperatorV2Request,
    start: EventEnvelope,
    state: OperationalBeliefState,
    history: DailyOperatorV2EvidenceHistory,
) -> None:
    """Prove that the initial snapshot includes exactly the requested new observations."""

    command = replace(request.ingestion, causation_id=start.event_id)
    count = len(command.observations)
    actual = tuple(
        history.read_stream(
            command.stream_id,
            after_sequence=command.expected_sequence,
            limit=count,
        )
    )
    expected_last = command.expected_sequence + count
    if len(actual) != count or state.last_source_stream_sequence != expected_last:
        raise DailyOperatorV2EvidenceError(
            "initial state does not end at the requested observation batch"
        )
    if not actual:
        raise DailyOperatorV2EvidenceError("request observation batch is absent")
    if start.global_position is None:
        raise DailyOperatorV2EvidenceError("run start is not a stored ledger occurrence")
    expected = observation_events(command, recorded_at=actual[0].recorded_at)
    for observed, candidate in zip(actual, expected, strict=True):
        if not _same_occurrence_semantics(observed, candidate):
            raise DailyOperatorV2EvidenceError(
                "stored observation differs from the immutable ingestion request"
            )
        if observed.global_position is None:
            raise DailyOperatorV2EvidenceError(
                "requested observation is not a stored ledger occurrence"
            )
        if not (start.global_position < observed.global_position <= state.cutoff_global_position):
            raise DailyOperatorV2EvidenceError(
                "requested observation must follow run start and precede the initial cutoff"
            )
        if history.get(observed.event_id) != observed:
            raise DailyOperatorV2EvidenceError(
                "requested observation lookup does not prove its occurrence"
            )
        slot = tuple(
            history.read_all(
                after_position=observed.global_position - 1,
                limit=1,
            )
        )
        if len(slot) != 1 or slot[0] != observed:
            raise DailyOperatorV2EvidenceError(
                "requested observation does not occupy its claimed ledger position"
            )


def rebuild_requested_context(
    request: DailyOperatorV2Request,
    state: OperationalBeliefState,
) -> ContextFrame:
    """Rebuild the deterministic state-to-context pipeline declared by the request."""

    packet = SignalPacketProjector().handle(request.signal, state)
    selection = DeterministicEvidenceRetriever().handle(request.retrieval, packet)
    return ContextFrameBuilder().handle(request.context, selection)


def _same_occurrence_semantics(actual: EventEnvelope, expected: EventEnvelope) -> bool:
    return (
        actual.stream_id == expected.stream_id
        and actual.stream_sequence == expected.stream_sequence
        and actual.event_type == expected.event_type
        and actual.schema_version == expected.schema_version
        and actual.recorded_at == expected.recorded_at
        and actual.effective_at == expected.effective_at
        and actual.correlation_id == expected.correlation_id
        and actual.causation_id == expected.causation_id
        and actual.actor == expected.actor
        and actual.source == expected.source
        and actual.payload == expected.payload
        and actual.idempotency_key == expected.idempotency_key
    )


__all__ = [
    "DailyOperatorV2EvidenceError",
    "DailyOperatorV2EvidenceHistory",
    "rebuild_requested_context",
    "verify_requested_ingestion",
]
