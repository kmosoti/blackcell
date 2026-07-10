from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from blackcell.context.models import content_digest
from blackcell.domains.repository import Claim, OperationalStateEstimate, Scalar


@dataclass(frozen=True, slots=True)
class SignalMeasurement:
    name: str
    value: Scalar
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("signal measurement name must be non-empty")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("signal evidence ids must be unique")


@dataclass(frozen=True, slots=True)
class SignalPacket:
    """A correlated telemetry summary; it is neither state nor model context."""

    scope: str
    state_id: str
    as_of_sequence: int
    as_of_time: datetime
    measurements: tuple[SignalMeasurement, ...]
    claim_ids: tuple[str, ...]
    source_counts: tuple[tuple[str, int], ...]
    schema_version: str = "signal-packet/v1"
    packet_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.scope.strip():
            raise ValueError("signal packet scope must be non-empty")
        if self.as_of_sequence < 0:
            raise ValueError("signal packet sequence must be non-negative")
        if self.as_of_time.tzinfo is None or self.as_of_time.utcoffset() is None:
            raise ValueError("signal packet time must be timezone-aware")
        if len(self.claim_ids) != len(set(self.claim_ids)):
            raise ValueError("signal packet claim ids must be unique")
        payload = {
            "scope": self.scope,
            "state_id": self.state_id,
            "as_of_sequence": self.as_of_sequence,
            "as_of_time": self.as_of_time,
            "measurements": self.measurements,
            "claim_ids": self.claim_ids,
            "source_counts": self.source_counts,
            "schema_version": self.schema_version,
        }
        object.__setattr__(self, "packet_id", f"signal:{content_digest(payload)}")


class SignalPacketProjector:
    def project(
        self,
        state: OperationalStateEstimate,
        *,
        scope: str = "repository",
    ) -> SignalPacket:
        current = state.current_claims
        expired = tuple(claim for claim in state.claims if claim not in current)
        current_unknowns = tuple(
            claim for claim in state.unknowns if not claim.is_expired(state.as_of_time)
        )
        conflict_claims = tuple(claim for conflict in state.conflicts for claim in conflict.claims)
        source_counts: dict[str, int] = {}
        for claim in current:
            for evidence in claim.evidence:
                source_counts[evidence.source] = source_counts.get(evidence.source, 0) + 1
        return SignalPacket(
            scope=scope,
            state_id=state.state_id,
            as_of_sequence=state.as_of_sequence,
            as_of_time=state.as_of_time,
            measurements=(
                SignalMeasurement("claims.current", len(current), _evidence_ids(current)),
                SignalMeasurement("claims.expired", len(expired), _evidence_ids(expired)),
                SignalMeasurement(
                    "conflicts.current",
                    len(state.conflicts),
                    _evidence_ids(conflict_claims),
                ),
                SignalMeasurement(
                    "unknowns.current",
                    len(current_unknowns),
                    _evidence_ids(current_unknowns),
                ),
            ),
            claim_ids=tuple(claim.claim_id for claim in current),
            source_counts=tuple(sorted(source_counts.items())),
        )


def _evidence_ids(claims: tuple[Claim, ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(evidence.event_id for claim in claims for evidence in claim.evidence)
    )
