from typing import Protocol

from blackcell.features.observe_outcome.command import ObserveOutcome
from blackcell.features.observe_outcome.models import OutcomeObservation


class OutcomeObserver(Protocol):
    @property
    def observer_id(self) -> str: ...

    @property
    def contract_version(self) -> str: ...

    def observe(self, command: ObserveOutcome) -> OutcomeObservation: ...
