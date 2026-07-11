from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from blackcell.features.ingest_observation.command import IngestCorrection, IngestObservation
from blackcell.features.ingest_observation.events import correction_events, observation_events
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


class IngestCorrectionHandler:
    def __init__(self, ledger: EventLedger, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._ledger = ledger
        self._clock = clock

    def handle(self, command: IngestCorrection) -> tuple[EventEnvelope, ...]:
        events = correction_events(command, recorded_at=self._clock())
        return self._ledger.append_many(
            events,
            expected_sequences={command.stream_id: command.expected_sequence},
        )
