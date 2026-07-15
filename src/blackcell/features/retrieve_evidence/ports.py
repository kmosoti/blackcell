from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from blackcell.features.retrieve_evidence.command import RetrieveEvidence
from blackcell.features.retrieve_evidence.models import (
    EvidenceObjectiveMatch,
    EvidenceSelection,
)
from blackcell.kernel import JsonScalar


class SignalClaimLike(Protocol):
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
    def freshness_seconds(self) -> int: ...

    @property
    def stale(self) -> bool: ...

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

    @property
    def epistemic_status(self) -> str: ...

    @property
    def unknown_reason(self) -> str | None: ...

    @property
    def expires_at(self) -> datetime | None: ...


class SignalConflictLike(Protocol):
    @property
    def subject(self) -> str: ...

    @property
    def predicate(self) -> str: ...


class SignalPacketLike(Protocol):
    @property
    def packet_id(self) -> str: ...

    @property
    def purpose(self) -> str: ...

    @property
    def state_domain(self) -> str: ...

    @property
    def state_stream_id(self) -> str | None: ...

    @property
    def state_global_position(self) -> int: ...

    @property
    def state_stream_position(self) -> int: ...

    @property
    def state_effective_time(self) -> datetime | None: ...

    @property
    def claims(self) -> Sequence[SignalClaimLike]: ...

    @property
    def conflicts(self) -> Sequence[SignalConflictLike]: ...


class EvidenceObjectiveMatcher(Protocol):
    """Rank objective-relevant observed claims without owning evidence policy."""

    def match(
        self,
        objective: str,
        claims: Sequence[SignalClaimLike],
    ) -> Sequence[EvidenceObjectiveMatch]: ...


class EvidenceRetriever(Protocol):
    def handle(
        self,
        query: RetrieveEvidence,
        packet: SignalPacketLike,
    ) -> EvidenceSelection: ...
