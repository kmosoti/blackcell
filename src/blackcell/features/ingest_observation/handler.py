from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from blackcell.features.ingest_observation.command import IngestObservation
from blackcell.features.ingest_observation.events import observation_events
from blackcell.features.ingest_observation.ports import EventLedger
from blackcell.kernel import EventEnvelope, utc_now


class IngestObservationHandler:
    def __init__(self, ledger: EventLedger, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._ledger = ledger
        self._clock = clock

    def handle(self, command: IngestObservation) -> tuple[EventEnvelope, ...]:
        events = observation_events(command, recorded_at=self._clock())
        return self._ledger.append_many(
            events,
            expected_sequences={command.stream_id: command.expected_sequence},
        )
