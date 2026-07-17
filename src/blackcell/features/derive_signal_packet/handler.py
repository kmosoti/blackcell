from __future__ import annotations

from blackcell.features.derive_signal_packet.command import DeriveSignalPacket
from blackcell.features.derive_signal_packet.models import (
    SignalClaim,
    SignalConflict,
    SignalEpistemicStatus,
    SignalPacket,
    SignalUnknownReason,
)
from blackcell.features.derive_signal_packet.ports import (
    BeliefClaimLike,
    BeliefConflictLike,
    BeliefStateLike,
)


def project_signal_packet(command: DeriveSignalPacket, state: BeliefStateLike) -> SignalPacket:
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
    state_effective_time = getattr(state, "effective_time_cutoff", None)
    schema_version = (
        "signal-packet/v3"
        if state_effective_time is not None
        or any(
            claim.epistemic_status is not SignalEpistemicStatus.OBSERVED
            or claim.expires_at is not None
            for claim in claims
        )
        else "signal-packet/v2"
    )
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
        schema_version=schema_version,
        state_effective_time=state_effective_time,
    )


def _signal_claim(command: DeriveSignalPacket, claim: BeliefClaimLike) -> SignalClaim:
    effective_at = claim.effective_at
    age = max(0, int((command.generated_at - effective_at).total_seconds()))
    status = SignalEpistemicStatus(getattr(claim, "epistemic_status", "observed"))
    source_unknown_reason = getattr(claim, "unknown_reason", None)
    unknown_reason = (
        None if source_unknown_reason is None else SignalUnknownReason(source_unknown_reason)
    )
    return SignalClaim(
        claim_id=claim.claim_id,
        subject=claim.subject,
        predicate=claim.predicate,
        value=claim.value,
        confidence=claim.confidence,
        effective_at=effective_at,
        freshness_seconds=age,
        stale=status is SignalEpistemicStatus.UNKNOWN or age > command.stale_after_seconds,
        source_event_id=claim.source_event_id,
        domain=claim.domain,
        stream_id=claim.stream_id,
        stream_sequence=claim.stream_sequence,
        global_position=claim.global_position,
        epistemic_status=status,
        unknown_reason=unknown_reason,
        expires_at=getattr(claim, "expires_at", None),
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
