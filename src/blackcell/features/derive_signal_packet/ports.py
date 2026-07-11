from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from blackcell.kernel import JsonScalar


class BeliefClaimLike(Protocol):
    @property
    def claim_id(self) -> str: ...

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
    def source_event_id(self) -> str: ...

    @property
    def domain(self) -> str: ...

    @property
    def stream_id(self) -> str: ...

    @property
    def stream_sequence(self) -> int: ...

    @property
    def global_position(self) -> int: ...


class BeliefConflictLike(Protocol):
    @property
    def subject(self) -> str: ...

    @property
    def predicate(self) -> str: ...

    @property
    def source_event_ids(self) -> tuple[str, ...]: ...

    @property
    def claim_ids(self) -> tuple[str, ...]: ...

    @property
    def values(self) -> tuple[JsonScalar, ...]: ...


class BeliefStateLike(Protocol):
    @property
    def scope(self) -> OperationalStateScopeLike: ...

    @property
    def claims(self) -> Sequence[BeliefClaimLike]: ...

    @property
    def conflicts(self) -> Sequence[BeliefConflictLike]: ...

    @property
    def cutoff_global_position(self) -> int: ...

    @property
    def last_source_stream_sequence(self) -> int: ...


class OperationalStateScopeLike(Protocol):
    @property
    def domain(self) -> str: ...

    @property
    def stream_id(self) -> str | None: ...
