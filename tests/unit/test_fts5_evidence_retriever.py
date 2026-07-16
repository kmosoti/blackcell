from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Never

import pytest

import blackcell.adapters.retrieval.fts5.adapter as fts5_adapter
from blackcell.adapters.retrieval import Fts5EvidenceRetriever, Fts5RetrievalError
from blackcell.features.build_context import BuildContext, build_context_frame
from blackcell.features.derive_signal_packet import (
    SignalClaim,
    SignalConflict,
    SignalEpistemicStatus,
    SignalPacket,
    SignalUnknownReason,
)
from blackcell.features.retrieve_evidence import (
    DeterministicEvidenceRetriever,
    EvidenceClaimIdentity,
    EvidenceKey,
    EvidenceObjectiveMatch,
    EvidenceOmissionReason,
    EvidenceSelection,
    MissingRequiredEvidenceError,
    RankedEvidenceRetriever,
    RetrieveEvidence,
)
from blackcell.features.retrieve_evidence.ports import SignalClaimLike

NOW = datetime(2026, 7, 14, 12, tzinfo=UTC)


def test_fts5_matches_term_baseline_at_the_same_result_and_context_budgets() -> None:
    packet = _packet(
        _claim(1, "owner", "kennedy"),
        _claim(2, "status", "blocked"),
        _claim(3, "priority", "high"),
    )
    query = RetrieveEvidence("resolve project owner", max_results=1)

    term_selection = DeterministicEvidenceRetriever().handle(query, packet)
    fts5_selection = Fts5EvidenceRetriever().handle(query, packet)

    assert (
        _candidate_identities(term_selection)
        == _candidate_identities(fts5_selection)
        == (EvidenceClaimIdentity("event:1", "claim:1"),)
    )
    assert term_selection.source_claim_identities == fts5_selection.source_claim_identities
    assert term_selection.candidates[0].reasons == ("objective-overlap",)
    assert fts5_selection.candidates[0].reasons == ("fts5-objective-match",)
    unconstrained = tuple(
        build_context_frame(BuildContext("task:matched", query.objective, NOW), selection)
        for selection in (term_selection, fts5_selection)
    )
    matched_budget = max(frame.model_payload_characters for frame in unconstrained)
    matched = tuple(
        build_context_frame(
            BuildContext(
                "task:matched",
                query.objective,
                NOW,
                max_characters=matched_budget,
            ),
            selection,
        )
        for selection in (term_selection, fts5_selection)
    )

    assert all(frame.model_payload_characters <= matched_budget for frame in matched)
    assert tuple(
        (item.source_event_id, item.claim_id, item.subject, item.predicate, item.value)
        for item in matched[0].evidence
    ) == tuple(
        (item.source_event_id, item.claim_id, item.subject, item.predicate, item.value)
        for item in matched[1].evidence
    )


def test_fts5_preserves_feature_owned_evidence_policy() -> None:
    expires_at = NOW + timedelta(hours=1)
    claims = (
        _claim(1, "status", "blocked"),
        _claim(2, "status", "ready"),
        _claim(3, "owner", "kennedy"),
        _claim(
            4,
            "priority",
            None,
            confidence=0.0,
            stale=True,
            epistemic_status=SignalEpistemicStatus.UNKNOWN,
            unknown_reason=SignalUnknownReason.EXPIRED,
            expires_at=expires_at,
        ),
    )
    conflict = SignalConflict(
        "project:blackcell",
        "status",
        ("event:1", "event:2"),
        ("claim:1", "claim:2"),
        ("blocked", "ready"),
    )
    packet = _packet(*claims, conflicts=(conflict,), state_effective_time=expires_at)

    selection = Fts5EvidenceRetriever().handle(
        RetrieveEvidence(
            "resolve owner",
            required_keys=(EvidenceKey("project:blackcell", "status"),),
            max_results=1,
        ),
        packet,
    )

    assert tuple(item.claim_id for item in selection.candidates) == ("claim:1", "claim:2")
    assert all(item.reasons == ("required", "conflict") for item in selection.candidates)
    assert all(item.conflicted for item in selection.candidates)
    assert selection.required_match_count == 2
    assert tuple((item.claim_id, item.reason) for item in selection.omissions) == (
        ("claim:3", EvidenceOmissionReason.RESULT_LIMIT),
        ("claim:4", EvidenceOmissionReason.UNKNOWN),
    )
    assert selection.omissions[0].reasons == ("fts5-objective-match",)
    assert selection.source_claim_identities == tuple(
        EvidenceClaimIdentity(f"event:{index}", f"claim:{index}") for index in range(1, 5)
    )
    with pytest.raises(MissingRequiredEvidenceError) as error:
        Fts5EvidenceRetriever().handle(
            RetrieveEvidence(
                "resolve priority",
                required_keys=(EvidenceKey("project:blackcell", "priority"),),
            ),
            packet,
        )
    assert error.value.gaps[0].unknown_supports[0].claim_id == "claim:4"


def test_fts5_uses_feature_owned_fallback_when_no_objective_term_is_available() -> None:
    selection = Fts5EvidenceRetriever().handle(
        RetrieveEvidence("***"),
        _packet(
            _claim(1, "owner", "kennedy"),
            _claim(2, "status", "blocked"),
        ),
    )

    assert tuple(item.claim_id for item in selection.candidates) == ("claim:1",)
    assert selection.candidates[0].reasons == ("state-fallback",)
    assert tuple((item.claim_id, item.reason) for item in selection.omissions) == (
        ("claim:2", EvidenceOmissionReason.IRRELEVANT),
    )


def test_fts5_compiles_untrusted_objectives_and_ranks_deterministically() -> None:
    packet = _packet(
        _claim(1, "owner", "kennedy"),
        _claim(2, "status", "blocked"),
        _claim(3, "summary", "owner status"),
    )
    query = RetrieveEvidence('owner OR "status") café -*', max_results=3)
    retriever = Fts5EvidenceRetriever()

    first = retriever.handle(query, packet)
    second = retriever.handle(query, packet)

    assert first == second
    assert first.selection_id == second.selection_id
    assert tuple(item.claim_id for item in first.candidates) == (
        "claim:3",
        "claim:1",
        "claim:2",
    )


def test_fts5_fails_closed_without_exposing_sqlite_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_to_connect(_database: str) -> Never:
        raise sqlite3.OperationalError("secret evidence content")

    monkeypatch.setattr(fts5_adapter.sqlite3, "connect", fail_to_connect)

    with pytest.raises(Fts5RetrievalError) as error:
        Fts5EvidenceRetriever().handle(
            RetrieveEvidence("owner"),
            _packet(_claim(1, "owner", "kennedy")),
        )

    assert str(error.value) == "SQLite FTS5 evidence retrieval failed"
    assert "secret" not in str(error.value)


def test_ranked_retriever_rejects_matches_outside_the_signal_packet() -> None:
    class ForeignMatcher:
        def match(
            self,
            objective: str,
            claims: Sequence[SignalClaimLike],
        ) -> tuple[EvidenceObjectiveMatch, ...]:
            return (
                EvidenceObjectiveMatch(
                    EvidenceClaimIdentity("event:foreign", "claim:foreign"),
                    100,
                    "foreign-match",
                ),
            )

    with pytest.raises(ValueError, match="matcher returned invalid results"):
        RankedEvidenceRetriever(ForeignMatcher()).handle(
            RetrieveEvidence("owner"),
            _packet(_claim(1, "owner", "kennedy")),
        )


def _candidate_identities(selection: EvidenceSelection) -> tuple[EvidenceClaimIdentity, ...]:
    return tuple(
        EvidenceClaimIdentity(item.source_event_id, item.claim_id) for item in selection.candidates
    )


def _claim(
    index: int,
    predicate: str,
    value: str | None,
    *,
    confidence: float = 0.9,
    stale: bool = False,
    epistemic_status: SignalEpistemicStatus = SignalEpistemicStatus.OBSERVED,
    unknown_reason: SignalUnknownReason | None = None,
    expires_at: datetime | None = None,
) -> SignalClaim:
    return SignalClaim(
        claim_id=f"claim:{index}",
        subject="project:blackcell",
        predicate=predicate,
        value=value,
        confidence=confidence,
        effective_at=NOW,
        freshness_seconds=index,
        stale=stale,
        source_event_id=f"event:{index}",
        domain="repository",
        stream_id="repository:1",
        stream_sequence=index,
        global_position=index,
        epistemic_status=epistemic_status,
        unknown_reason=unknown_reason,
        expires_at=expires_at,
    )


def _packet(
    *claims: SignalClaim,
    conflicts: tuple[SignalConflict, ...] = (),
    state_effective_time: datetime | None = None,
) -> SignalPacket:
    return SignalPacket(
        purpose="daily",
        state_domain="repository",
        state_stream_id="repository:1",
        generated_at=NOW,
        state_global_position=max((claim.global_position for claim in claims), default=0),
        state_stream_position=max((claim.stream_sequence for claim in claims), default=0),
        claims=tuple(claims),
        conflicts=conflicts,
        provenance_event_ids=tuple(sorted({claim.source_event_id for claim in claims})),
        mean_confidence=(
            sum(claim.confidence for claim in claims) / len(claims) if claims else 0.0
        ),
        stale_claim_count=sum(claim.stale for claim in claims),
        schema_version=(
            "signal-packet/v3" if state_effective_time is not None else "signal-packet/v2"
        ),
        state_effective_time=state_effective_time,
    )
