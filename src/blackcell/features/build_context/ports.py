from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from blackcell.kernel import JsonScalar


class EvidenceCandidateLike(Protocol):
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
    def score(self) -> int: ...

    @property
    def reasons(self) -> tuple[str, ...]: ...

    @property
    def conflicted(self) -> bool: ...


class EvidenceOmissionLike(EvidenceCandidateLike, Protocol):
    @property
    def reason(self) -> str: ...

    @property
    def omission_id(self) -> str: ...

    @property
    def schema_version(self) -> str: ...


class EvidenceClaimIdentityLike(Protocol):
    @property
    def source_event_id(self) -> str: ...

    @property
    def claim_id(self) -> str: ...


class EvidenceSelectionLike(Protocol):
    @property
    def objective(self) -> str: ...

    @property
    def selection_id(self) -> str: ...

    @property
    def source_packet_id(self) -> str: ...

    @property
    def source_packet_purpose(self) -> str: ...

    @property
    def state_domain(self) -> str: ...

    @property
    def state_stream_id(self) -> str | None: ...

    @property
    def state_global_position(self) -> int: ...

    @property
    def state_stream_position(self) -> int: ...

    @property
    def source_claim_identities(self) -> Sequence[EvidenceClaimIdentityLike]: ...

    @property
    def candidates(self) -> Sequence[EvidenceCandidateLike]: ...

    @property
    def omissions(self) -> Sequence[EvidenceOmissionLike]: ...
