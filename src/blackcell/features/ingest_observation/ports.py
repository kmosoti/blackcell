from collections.abc import Mapping, Sequence
from typing import Protocol

from blackcell.kernel import EventEnvelope


class EventLedger(Protocol):
    def append_many(
        self,
        events: Sequence[EventEnvelope],
        *,
        expected_sequences: Mapping[str, int],
    ) -> tuple[EventEnvelope, ...]: ...
