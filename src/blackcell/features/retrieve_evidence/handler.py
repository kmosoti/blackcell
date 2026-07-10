from __future__ import annotations

import re

from blackcell.features.retrieve_evidence.command import RetrieveEvidence
from blackcell.features.retrieve_evidence.models import EvidenceCandidate, EvidenceSelection
from blackcell.features.retrieve_evidence.ports import SignalClaimLike, SignalPacketLike

_TOKEN = re.compile(r"[a-z0-9_./:-]+")


class DeterministicEvidenceRetriever:
    def handle(self, query: RetrieveEvidence, packet: SignalPacketLike) -> EvidenceSelection:
        required = {(key.subject, key.predicate) for key in query.required_keys}
        conflict_keys = {(item.subject, item.predicate) for item in packet.conflicts}
        objective_tokens = _tokens(query.objective)
        ranked = tuple(
            sorted(
                (
                    candidate
                    for claim in packet.claims
                    if (candidate := _candidate(claim, objective_tokens, required, conflict_keys))
                    is not None
                ),
                key=lambda item: (-item.score, item.freshness_seconds, item.source_event_id),
            )
        )
        if not ranked and packet.claims:
            fallback = min(
                packet.claims,
                key=lambda item: (item.freshness_seconds, item.source_event_id),
            )
            ranked = (_fallback(fallback, conflict_keys),)
        selected = ranked[: query.max_results]
        return EvidenceSelection(
            objective=query.objective,
            source_packet_id=packet.packet_id,
            state_position=packet.state_position,
            candidates=selected,
            omitted_count=max(0, len(packet.claims) - len(selected)),
        )


def _candidate(
    claim: SignalClaimLike,
    objective_tokens: frozenset[str],
    required: set[tuple[str, str]],
    conflict_keys: set[tuple[str, str]],
) -> EvidenceCandidate | None:
    key = (claim.subject, claim.predicate)
    claim_tokens = _tokens(f"{claim.subject} {claim.predicate} {claim.value}")
    overlap = objective_tokens & claim_tokens
    reasons: list[str] = []
    score = 0
    if key in required:
        reasons.append("required")
        score += 1_000
    if overlap:
        reasons.append("objective-overlap")
        score += 100 * len(overlap)
    if key in conflict_keys:
        reasons.append("conflict")
        score += 200
    if not reasons:
        return None
    score += round(claim.confidence * 10)
    if claim.stale:
        score -= 25
    return _copy_candidate(claim, score, tuple(reasons), key in conflict_keys)


def _fallback(
    claim: SignalClaimLike,
    conflict_keys: set[tuple[str, str]],
) -> EvidenceCandidate:
    key = (claim.subject, claim.predicate)
    return _copy_candidate(claim, 0, ("state-fallback",), key in conflict_keys)


def _copy_candidate(
    claim: SignalClaimLike,
    score: int,
    reasons: tuple[str, ...],
    conflicted: bool,
) -> EvidenceCandidate:
    return EvidenceCandidate(
        claim.subject,
        claim.predicate,
        claim.value,
        claim.confidence,
        claim.effective_at,
        claim.freshness_seconds,
        claim.stale,
        claim.source_event_id,
        score,
        reasons,
        conflicted,
    )


def _tokens(value: str) -> frozenset[str]:
    return frozenset(_TOKEN.findall(value.casefold()))
