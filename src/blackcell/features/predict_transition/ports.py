from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from blackcell.kernel import JsonScalar


class PredictionScopeLike(Protocol):
    @property
    def domain(self) -> str: ...

    @property
    def stream_id(self) -> str | None: ...


class PredictionClaimLike(Protocol):
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
    def source_event_id(self) -> str: ...

    @property
    def epistemic_status(self) -> object: ...


class PredictionConflictLike(Protocol):
    @property
    def subject(self) -> str: ...

    @property
    def predicate(self) -> str: ...

    @property
    def claim_ids(self) -> tuple[str, ...]: ...

    @property
    def source_event_ids(self) -> tuple[str, ...]: ...

    @property
    def key(self) -> tuple[str, str]: ...


class PredictionStateLike(Protocol):
    @property
    def scope(self) -> PredictionScopeLike: ...

    @property
    def claims(self) -> Sequence[PredictionClaimLike]: ...

    @property
    def conflicts(self) -> Sequence[PredictionConflictLike]: ...

    @property
    def cutoff_global_position(self) -> int: ...

    @property
    def last_source_stream_sequence(self) -> int: ...

    @property
    def effective_time_cutoff(self) -> datetime | None: ...

    def claims_for(self, subject: str, predicate: str) -> Sequence[PredictionClaimLike]: ...


__all__ = [
    "PredictionClaimLike",
    "PredictionConflictLike",
    "PredictionScopeLike",
    "PredictionStateLike",
]
