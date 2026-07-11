from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from blackcell.kernel import JsonScalar


class EvidenceCandidateLike(Protocol):
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
    def score(self) -> int: ...

    @property
    def reasons(self) -> tuple[str, ...]: ...

    @property
    def conflicted(self) -> bool: ...


class EvidenceSelectionLike(Protocol):
    @property
    def objective(self) -> str: ...

    @property
    def selection_id(self) -> str: ...

    @property
    def source_packet_id(self) -> str: ...

    @property
    def state_position(self) -> int: ...

    @property
    def candidates(self) -> Sequence[EvidenceCandidateLike]: ...

    @property
    def omitted_count(self) -> int: ...
