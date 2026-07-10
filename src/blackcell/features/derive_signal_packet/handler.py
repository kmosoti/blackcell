from __future__ import annotations

from blackcell.features.derive_signal_packet.command import DeriveSignalPacket
from blackcell.features.derive_signal_packet.models import (
    SignalClaim,
    SignalConflict,
    SignalPacket,
)
from blackcell.features.derive_signal_packet.ports import BeliefClaimLike, BeliefStateLike


class SignalPacketProjector:
    def handle(self, command: DeriveSignalPacket, state: BeliefStateLike) -> SignalPacket:
        claims = tuple(
            sorted(
                (_signal_claim(command, claim) for claim in state.claims),
                key=lambda claim: (claim.subject, claim.predicate, claim.source_event_id),
            )
        )
        conflicts = tuple(
            sorted(
                (
                    SignalConflict(
                        conflict.subject,
                        conflict.predicate,
                        tuple(sorted(conflict.source_event_ids)),
                        conflict.values,
                    )
                    for conflict in state.conflicts
                ),
                key=lambda conflict: (conflict.subject, conflict.predicate),
            )
        )
        provenance = tuple(sorted({claim.source_event_id for claim in claims}))
        mean_confidence = sum(claim.confidence for claim in claims) / len(claims) if claims else 0.0
        return SignalPacket(
            scope=command.scope,
            generated_at=command.generated_at,
            state_position=state.last_global_position,
            claims=claims,
            conflicts=conflicts,
            provenance_event_ids=provenance,
            mean_confidence=mean_confidence,
            stale_claim_count=sum(claim.stale for claim in claims),
        )


def _signal_claim(command: DeriveSignalPacket, claim: BeliefClaimLike) -> SignalClaim:
    effective_at = claim.effective_at
    age = max(0, int((command.generated_at - effective_at).total_seconds()))
    return SignalClaim(
        subject=claim.subject,
        predicate=claim.predicate,
        value=claim.value,
        confidence=claim.confidence,
        effective_at=effective_at,
        freshness_seconds=age,
        stale=age > command.stale_after_seconds,
        source_event_id=claim.source_event_id,
    )
