from __future__ import annotations

from blackcell.features.derive_signal_packet.command import DeriveSignalPacket
from blackcell.features.derive_signal_packet.models import (
    SignalClaim,
    SignalConflict,
    SignalPacket,
)
from blackcell.features.derive_signal_packet.ports import (
    BeliefClaimLike,
    BeliefConflictLike,
    BeliefStateLike,
)


class SignalPacketProjector:
    def handle(self, command: DeriveSignalPacket, state: BeliefStateLike) -> SignalPacket:
        claims = tuple(
            sorted(
                (_signal_claim(command, claim) for claim in state.claims),
                key=lambda claim: (
                    claim.subject,
                    claim.predicate,
                    claim.source_event_id,
                    claim.claim_id,
                ),
            )
        )
        conflicts = tuple(
            sorted(
                (_signal_conflict(conflict) for conflict in state.conflicts),
                key=lambda conflict: (conflict.subject, conflict.predicate),
            )
        )
        provenance = tuple(sorted({claim.source_event_id for claim in claims}))
        mean_confidence = sum(claim.confidence for claim in claims) / len(claims) if claims else 0.0
        return SignalPacket(
            purpose=command.purpose,
            state_domain=state.scope.domain,
            state_stream_id=state.scope.stream_id,
            generated_at=command.generated_at,
            state_global_position=state.cutoff_global_position,
            state_stream_position=state.last_source_stream_sequence,
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
        claim_id=claim.claim_id,
        subject=claim.subject,
        predicate=claim.predicate,
        value=claim.value,
        confidence=claim.confidence,
        effective_at=effective_at,
        freshness_seconds=age,
        stale=age > command.stale_after_seconds,
        source_event_id=claim.source_event_id,
        domain=claim.domain,
        stream_id=claim.stream_id,
        stream_sequence=claim.stream_sequence,
        global_position=claim.global_position,
    )


def _signal_conflict(conflict: BeliefConflictLike) -> SignalConflict:
    members = tuple(
        sorted(
            zip(
                conflict.source_event_ids,
                conflict.claim_ids,
                conflict.values,
                strict=True,
            ),
            key=lambda item: (item[0], item[1]),
        )
    )
    return SignalConflict(
        conflict.subject,
        conflict.predicate,
        tuple(item[0] for item in members),
        tuple(item[1] for item in members),
        tuple(item[2] for item in members),
    )
