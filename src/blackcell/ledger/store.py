"""Chronicle storage interface."""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Any, Protocol

from blackcell.ledger.sqlite import ChronicleEvent, EventType


class ChronicleStore(Protocol):
    def append(
        self,
        event_type: str | EventType,
        plan_id: str,
        payload: Mapping[str, Any] | None = None,
        item_key: str | None = None,
    ) -> int: ...

    def events(self, plan_id: str | None = None) -> list[ChronicleEvent]: ...

    def plan_lock(self, plan_id: str) -> AbstractContextManager[None]: ...
