from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from blackcell.kernel import JsonScalar
from blackcell.kernel._json import json_digest


class SignalEpistemicStatus(StrEnum):
    OBSERVED = "observed"
    UNKNOWN = "unknown"


class SignalUnknownReason(StrEnum):
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class SignalClaim:
    claim_id: str
    subject: str
    predicate: str
    value: JsonScalar
    confidence: float
    effective_at: datetime
    freshness_seconds: int
    stale: bool
    source_event_id: str
    domain: str
    stream_id: str
    stream_sequence: int
    global_position: int
    epistemic_status: SignalEpistemicStatus = SignalEpistemicStatus.OBSERVED
    unknown_reason: SignalUnknownReason | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.epistemic_status, SignalEpistemicStatus):
            raise TypeError("epistemic_status must be recognized")
        if self.unknown_reason is not None and not isinstance(
            self.unknown_reason, SignalUnknownReason
        ):
            raise TypeError("unknown_reason must be recognized")
        if self.expires_at is not None and (
            self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None
        ):
            raise ValueError("expires_at must be timezone-aware")
        if self.expires_at is not None and self.expires_at < self.effective_at:
            raise ValueError("expires_at cannot precede effective_at")
        if self.epistemic_status is SignalEpistemicStatus.OBSERVED:
            if self.unknown_reason is not None:
                raise ValueError("observed signal claims cannot have an unknown reason")
        elif (
            self.value is not None
            or self.confidence != 0.0
            or self.unknown_reason is not SignalUnknownReason.EXPIRED
            or self.expires_at is None
            or not self.stale
        ):
            raise ValueError("unknown signal claims require explicit expired semantics")


@dataclass(frozen=True, slots=True)
class SignalConflict:
    subject: str
    predicate: str
    source_event_ids: tuple[str, ...]
    claim_ids: tuple[str, ...]
    values: tuple[JsonScalar, ...]

    def __post_init__(self) -> None:
        if not self.claim_ids or not (
            len(self.source_event_ids) == len(self.claim_ids) == len(self.values)
        ):
            raise ValueError("signal conflict provenance arrays must be non-empty and aligned")


@dataclass(frozen=True, slots=True)
class SignalPacket:
    purpose: str
    state_domain: str
    state_stream_id: str | None
    generated_at: datetime
    state_global_position: int
    state_stream_position: int
    claims: tuple[SignalClaim, ...]
    conflicts: tuple[SignalConflict, ...]
    provenance_event_ids: tuple[str, ...]
    mean_confidence: float
    stale_claim_count: int
    schema_version: str = "signal-packet/v2"
    state_effective_time: datetime | None = None
    packet_id: str = field(init=False)

    @property
    def state_position(self) -> int:
        """Compatibility name for the complete ledger cutoff."""

        return self.state_global_position

    def __post_init__(self) -> None:
        if not self.purpose.strip() or not self.state_domain.strip():
            raise ValueError("packet purpose and state domain must not be empty")
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        if self.state_effective_time is not None and (
            self.state_effective_time.tzinfo is None
            or self.state_effective_time.utcoffset() is None
        ):
            raise ValueError("state_effective_time must be timezone-aware")
        if self.schema_version not in {"signal-packet/v2", "signal-packet/v3"}:
            raise ValueError("signal packet schema is unsupported")
        has_epistemic_extensions = self.state_effective_time is not None or any(
            claim.epistemic_status is not SignalEpistemicStatus.OBSERVED
            or claim.unknown_reason is not None
            or claim.expires_at is not None
            for claim in self.claims
        )
        if self.schema_version == "signal-packet/v2" and has_epistemic_extensions:
            raise ValueError("signal-packet/v2 cannot contain epistemic extensions")
        if self.schema_version == "signal-packet/v3" and not has_epistemic_extensions:
            raise ValueError("signal-packet/v3 requires epistemic extensions")
        if self.state_stream_id is not None and not self.state_stream_id.strip():
            raise ValueError("state_stream_id must not be blank")
        if self.state_global_position < 0 or self.state_stream_position < 0:
            raise ValueError("state positions must be non-negative")
        if self.state_stream_id is None and (
            self.claims or self.conflicts or self.state_stream_position
        ):
            raise ValueError("an unbound packet state must not contain evidence")
        outside_scope = tuple(
            claim
            for claim in self.claims
            if claim.domain != self.state_domain or claim.stream_id != self.state_stream_id
        )
        if outside_scope:
            raise ValueError("signal claims must belong to the packet state scope")
        if any(claim.global_position > self.state_global_position for claim in self.claims):
            raise ValueError("signal claims cannot exceed the packet ledger cutoff")
        if any(claim.stream_sequence > self.state_stream_position for claim in self.claims):
            raise ValueError("signal claims cannot exceed the packet stream position")
        claim_identities = {(claim.source_event_id, claim.claim_id) for claim in self.claims}
        if len(claim_identities) != len(self.claims):
            raise ValueError("signal event/claim identities must be unique")
        conflict_identities = (
            set(zip(conflict.source_event_ids, conflict.claim_ids, strict=True))
            for conflict in self.conflicts
        )
        if any(not identities <= claim_identities for identities in conflict_identities):
            raise ValueError("signal conflicts must reference claims in the packet")
        expected_provenance = tuple(sorted({claim.source_event_id for claim in self.claims}))
        if self.provenance_event_ids != expected_provenance:
            raise ValueError("packet provenance must match its claim event sources")
        expected_mean = (
            sum(claim.confidence for claim in self.claims) / len(self.claims)
            if self.claims
            else 0.0
        )
        if self.mean_confidence != expected_mean:
            raise ValueError("mean_confidence must equal the mean claim confidence")
        if self.stale_claim_count != sum(claim.stale for claim in self.claims):
            raise ValueError("stale_claim_count must equal the number of stale claims")
        object.__setattr__(self, "packet_id", json_digest(_packet_payload(self)))


def _packet_payload(packet: SignalPacket) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": packet.schema_version,
        "purpose": packet.purpose,
        "state_domain": packet.state_domain,
        "state_stream_id": packet.state_stream_id,
        "generated_at": packet.generated_at.isoformat(),
        "state_global_position": packet.state_global_position,
        "state_stream_position": packet.state_stream_position,
        "claims": [_claim_payload(claim, packet.schema_version) for claim in packet.claims],
        "conflicts": [
            {
                "subject": conflict.subject,
                "predicate": conflict.predicate,
                "source_event_ids": list(conflict.source_event_ids),
                "claim_ids": list(conflict.claim_ids),
                "values": list(conflict.values),
            }
            for conflict in packet.conflicts
        ],
        "provenance_event_ids": list(packet.provenance_event_ids),
        "mean_confidence": packet.mean_confidence,
        "stale_claim_count": packet.stale_claim_count,
    }
    if packet.schema_version == "signal-packet/v3":
        payload["state_effective_time"] = (
            packet.state_effective_time.isoformat()
            if packet.state_effective_time is not None
            else None
        )
    return payload


def _claim_payload(claim: SignalClaim, schema_version: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "claim_id": claim.claim_id,
        "subject": claim.subject,
        "predicate": claim.predicate,
        "value": claim.value,
        "confidence": claim.confidence,
        "effective_at": claim.effective_at.isoformat(),
        "freshness_seconds": claim.freshness_seconds,
        "stale": claim.stale,
        "source_event_id": claim.source_event_id,
        "domain": claim.domain,
        "stream_id": claim.stream_id,
        "stream_sequence": claim.stream_sequence,
        "global_position": claim.global_position,
    }
    if schema_version == "signal-packet/v3":
        payload.update(
            {
                "epistemic_status": claim.epistemic_status,
                "unknown_reason": claim.unknown_reason,
                "expires_at": (
                    claim.expires_at.isoformat() if claim.expires_at is not None else None
                ),
            }
        )
    return payload
