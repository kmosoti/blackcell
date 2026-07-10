from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from blackcell.kernel import JsonScalar


class SignalClaimLike(Protocol):
    @property
    def subject(self) -> str: ...

    @property
    def predicate(self) -> str: ...

    @property
    def value(self) -> JsonScalar: ...

    @property
    def confidence(self) -> float: ...

    @property
    def effective_at(self) -> datetime: ...

    @property
    def freshness_seconds(self) -> int: ...

    @property
    def stale(self) -> bool: ...

    @property
    def source_event_id(self) -> str: ...


class SignalConflictLike(Protocol):
    @property
    def subject(self) -> str: ...

    @property
    def predicate(self) -> str: ...


class SignalPacketLike(Protocol):
    @property
    def packet_id(self) -> str: ...

    @property
    def state_position(self) -> int: ...

    @property
    def claims(self) -> Sequence[SignalClaimLike]: ...

    @property
    def conflicts(self) -> Sequence[SignalConflictLike]: ...
