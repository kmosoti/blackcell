from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from blackcell.kernel import JsonScalar


class BeliefClaimLike(Protocol):
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


class BeliefConflictLike(Protocol):
    @property
    def subject(self) -> str: ...

    @property
    def predicate(self) -> str: ...

    @property
    def source_event_ids(self) -> tuple[str, ...]: ...

    @property
    def values(self) -> tuple[JsonScalar, ...]: ...


class BeliefStateLike(Protocol):
    @property
    def claims(self) -> Sequence[BeliefClaimLike]: ...

    @property
    def conflicts(self) -> Sequence[BeliefConflictLike]: ...

    @property
    def last_global_position(self) -> int: ...
