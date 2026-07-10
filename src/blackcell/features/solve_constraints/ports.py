from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from blackcell.kernel import JsonScalar


class ContextEvidenceLike(Protocol):
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

    @property
    def conflicted(self) -> bool: ...


class ContextFrameLike(Protocol):
    @property
    def frame_id(self) -> str: ...

    @property
    def evidence(self) -> Sequence[ContextEvidenceLike]: ...
