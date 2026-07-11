from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from blackcell.features.build_context import (
    BuildContext,
    ContextBudgetError,
    ContextFrameBuilder,
    ContextOmissionReason,
    ContextOmissionStage,
    ContextSelectionMismatchError,
)
from blackcell.features.derive_signal_packet import DeriveSignalPacket, SignalPacketProjector
from blackcell.features.ingest_observation import (
    EvidencePointer,
    IngestObservation,
    IngestObservationHandler,
    ObservationInput,
    ObservedClaim,
)
from blackcell.features.project_operational_state import OperationalStateProjector
from blackcell.features.retrieve_evidence import (
    DeterministicEvidenceRetriever,
    EvidenceKey,
    EvidenceOmission,
    EvidenceOmissionReason,
    MissingRequiredEvidenceError,
    RequiredEvidenceGapReason,
    RetrieveEvidence,
)
from blackcell.kernel import EventStore

NOW = datetime(2026, 7, 10, 17, tzinfo=UTC)


def test_retrieval_and_context_frame_preserve_task_relevance_and_citations(
    tmp_path: Path,
) -> None:
    packet = _packet(tmp_path, (("status", "blocked"), ("owner", "kennedy")))
    selection = DeterministicEvidenceRetriever().handle(
        RetrieveEvidence("resolve blocked project status", max_results=1), packet
    )
    frame = ContextFrameBuilder().handle(
        BuildContext("task:1", "resolve blocked project status", NOW), selection
    )

    assert tuple(item.predicate for item in selection.candidates) == ("status",)
    assert frame.evidence[0].value == "blocked"
    assert frame.provenance_event_ids == (frame.evidence[0].source_event_id,)
    assert frame.source_packet_id == packet.packet_id
    assert frame.source_packet_purpose == packet.purpose == "daily"
    assert (frame.state_domain, frame.state_stream_id) == (
        packet.state_domain,
        packet.state_stream_id,
    )
    assert tuple(
        (item.source_event_id, item.claim_id) for item in frame.source_claim_identities
    ) == tuple(sorted((item.source_event_id, item.claim_id) for item in packet.claims))
    assert frame.evidence[0].claim_id == selection.candidates[0].claim_id
    assert frame.evidence[0].global_position == selection.candidates[0].global_position
    assert frame.omitted_evidence_count == 1
    assert frame.omissions[0].reason is ContextOmissionReason.IRRELEVANT
    assert frame.omissions[0].stage is ContextOmissionStage.RETRIEVAL
    assert frame.omissions[0].source_omission_id == selection.omissions[0].omission_id
    assert frame.frame_id.startswith("sha256:")

    with pytest.raises(ValueError, match="selection state scope"):
        replace(selection, state_domain="personal-planning")
    with pytest.raises(ValueError, match="declared state scope"):
        replace(frame, state_domain="personal-planning")


def test_context_frame_rejects_evidence_selected_for_another_objective(
    tmp_path: Path,
) -> None:
    packet = _packet(tmp_path, (("status", "blocked"),))
    selection = DeterministicEvidenceRetriever().handle(
        RetrieveEvidence("resolve blocked project status"),
        packet,
    )

    with pytest.raises(ContextSelectionMismatchError):
        ContextFrameBuilder().handle(
            BuildContext("task:1", "audit dependencies", NOW),
            selection,
        )


def test_context_frame_is_deterministic_and_enforces_required_budget(tmp_path: Path) -> None:
    packet = _packet(tmp_path, (("status", "blocked"),))
    selection = DeterministicEvidenceRetriever().handle(RetrieveEvidence("project status"), packet)
    builder = ContextFrameBuilder()
    command = BuildContext("task:1", "project status", NOW)

    assert builder.handle(command, selection) == builder.handle(command, selection)

    required_selection = DeterministicEvidenceRetriever().handle(
        RetrieveEvidence("unrelated", required_keys=()), packet
    )
    tiny = BuildContext("task:1", "unrelated", NOW, max_characters=1)
    frame = builder.handle(tiny, required_selection)
    assert frame.evidence == ()
    assert frame.omitted_evidence_count == 1
    assert frame.omissions[0].reason is ContextOmissionReason.CHARACTER_BUDGET
    assert frame.omissions[0].serialized_characters is not None

    required = DeterministicEvidenceRetriever().handle(
        RetrieveEvidence("unrelated", required_keys=(EvidenceKey("project:blackcell", "status"),)),
        packet,
    )
    with pytest.raises(ContextBudgetError):
        builder.handle(tiny, required)


def test_context_frame_rejects_when_later_required_evidence_exceeds_budget(
    tmp_path: Path,
) -> None:
    packet = _packet(tmp_path, (("status", "blocked"), ("owner", "kennedy")))
    required_keys = (
        EvidenceKey("project:blackcell", "status"),
        EvidenceKey("project:blackcell", "owner"),
    )
    retriever = DeterministicEvidenceRetriever()
    selection = retriever.handle(RetrieveEvidence("unrelated", required_keys=required_keys), packet)
    assert len(selection.candidates) == 2
    first_only = retriever.handle(
        RetrieveEvidence("unrelated", required_keys=required_keys[:1]),
        packet,
    )

    builder = ContextFrameBuilder()
    first_size = builder.handle(
        BuildContext("task:1", "unrelated", NOW), first_only
    ).serialized_characters

    with pytest.raises(ContextBudgetError):
        builder.handle(
            BuildContext("task:1", "unrelated", NOW, max_characters=first_size),
            selection,
        )


def test_retrieval_preserves_required_matches_beyond_result_target(tmp_path: Path) -> None:
    packet = _packet(tmp_path, (("status", "blocked"), ("owner", "kennedy")))
    selection = DeterministicEvidenceRetriever().handle(
        RetrieveEvidence(
            "unrelated",
            required_keys=(
                EvidenceKey("project:blackcell", "status"),
                EvidenceKey("project:blackcell", "owner"),
            ),
            max_results=1,
        ),
        packet,
    )

    assert tuple(item.predicate for item in selection.candidates) == ("status", "owner")
    assert all("required" in item.reasons for item in selection.candidates)
    assert selection.required_match_count == 2
    assert selection.omitted_count == 0


def test_retrieval_fails_closed_when_a_required_key_is_missing(tmp_path: Path) -> None:
    packet = _packet(tmp_path, (("status", "blocked"),))
    missing_key = EvidenceKey("project:blackcell", "owner")

    with pytest.raises(MissingRequiredEvidenceError) as error:
        DeterministicEvidenceRetriever().handle(
            RetrieveEvidence("project status", required_keys=(missing_key,)),
            packet,
        )

    assert error.value.missing_keys == (missing_key,)
    assert error.value.gaps[0].key == missing_key
    assert error.value.gaps[0].reason is RequiredEvidenceGapReason.ABSENT
    assert error.value.gaps[0].gap_id.startswith("sha256:")
    assert error.value.gaps[0].source_packet_id == packet.packet_id
    assert error.value.gaps[0].state_domain == packet.state_domain
    assert error.value.gaps[0].state_stream_id == packet.state_stream_id
    assert error.value.gaps[0].state_global_position == packet.state_global_position
    assert error.value.gaps[0].state_stream_position == packet.state_stream_position
    assert (
        replace(
            error.value.gaps[0],
            state_global_position=packet.state_global_position + 1,
        ).gap_id
        != error.value.gaps[0].gap_id
    )


def test_retrieval_uses_remaining_capacity_for_ranked_optional_evidence(
    tmp_path: Path,
) -> None:
    packet = _packet(
        tmp_path,
        (("status", "blocked"), ("owner", "kennedy"), ("priority", "high")),
    )
    selection = DeterministicEvidenceRetriever().handle(
        RetrieveEvidence(
            "owner priority",
            required_keys=(EvidenceKey("project:blackcell", "status"),),
            max_results=2,
        ),
        packet,
    )

    assert tuple(item.predicate for item in selection.candidates) == ("status", "owner")
    assert selection.candidates[0].reasons == ("required",)
    assert selection.candidates[1].reasons == ("objective-overlap",)
    assert selection.omitted_count == 1
    assert selection.omissions[0].reason is EvidenceOmissionReason.RESULT_LIMIT
    assert selection.omissions[0].predicate == "priority"


def test_retrieval_preserves_duplicate_and_conflicting_required_matches(
    tmp_path: Path,
) -> None:
    packet = _packet(
        tmp_path,
        (
            ("status", "blocked"),
            ("status", "blocked"),
            ("status", "ready"),
            ("owner", "kennedy"),
        ),
    )
    selection = DeterministicEvidenceRetriever().handle(
        RetrieveEvidence(
            "unrelated",
            required_keys=(EvidenceKey("project:blackcell", "status"),),
            max_results=1,
        ),
        packet,
    )

    assert tuple(item.value for item in selection.candidates) == (
        "blocked",
        "blocked",
        "ready",
    )
    assert all(item.conflicted for item in selection.candidates)
    assert len({item.source_event_id for item in selection.candidates}) == 3
    assert selection.required_match_count == 3
    assert selection.omitted_count == 1


def test_evidence_selection_cannot_masquerade_as_required_complete(tmp_path: Path) -> None:
    packet = _packet(tmp_path, (("status", "blocked"), ("status", "ready")))
    selection = DeterministicEvidenceRetriever().handle(
        RetrieveEvidence(
            "unrelated",
            required_keys=(EvidenceKey("project:blackcell", "status"),),
        ),
        packet,
    )

    with pytest.raises(ValueError, match="exactly cover source packet claims"):
        replace(selection, candidates=selection.candidates[:1])

    with pytest.raises(ValueError, match="selected evidence identities must be unique"):
        replace(selection, candidates=(selection.candidates[0], selection.candidates[0]))

    candidate = selection.candidates[0]
    forged_omission = EvidenceOmission(
        claim_id=candidate.claim_id,
        subject=candidate.subject,
        predicate=candidate.predicate,
        value=candidate.value,
        confidence=candidate.confidence,
        effective_at=candidate.effective_at,
        freshness_seconds=candidate.freshness_seconds,
        stale=candidate.stale,
        source_event_id=candidate.source_event_id,
        domain=candidate.domain,
        stream_id=candidate.stream_id,
        stream_sequence=candidate.stream_sequence,
        global_position=candidate.global_position,
        score=0,
        reasons=(),
        conflicted=candidate.conflicted,
        reason=EvidenceOmissionReason.IRRELEVANT,
    )
    with pytest.raises(ValueError, match="every required matching disposition"):
        replace(
            selection,
            candidates=selection.candidates[1:],
            omissions=(forged_omission,),
            required_match_count=1,
        )


def test_retrieval_records_every_nonselected_claim_with_a_precise_reason(
    tmp_path: Path,
) -> None:
    packet = _packet(
        tmp_path,
        (
            ("status", "blocked"),
            ("status", "ready"),
            ("owner", "kennedy"),
            ("priority", "low"),
        ),
    )
    selection = DeterministicEvidenceRetriever().handle(
        RetrieveEvidence(
            "owner",
            required_keys=(EvidenceKey("project:blackcell", "status"),),
            max_results=2,
        ),
        packet,
    )

    assert tuple(item.value for item in selection.candidates) == ("blocked", "ready")
    assert all(item.conflicted for item in selection.candidates)
    assert tuple((item.predicate, item.reason) for item in selection.omissions) == (
        ("priority", EvidenceOmissionReason.IRRELEVANT),
        ("owner", EvidenceOmissionReason.RESULT_LIMIT),
    )
    assert len(selection.candidates) + selection.omitted_count == len(packet.claims)
    assert all(item.omission_id.startswith("sha256:") for item in selection.omissions)


def test_selection_and_frame_identities_include_typed_omission_content(
    tmp_path: Path,
) -> None:
    packet = _packet(tmp_path, (("status", "blocked"), ("owner", "kennedy")))
    selection = DeterministicEvidenceRetriever().handle(
        RetrieveEvidence("status", max_results=1), packet
    )
    changed_selection = replace(
        selection,
        omissions=(replace(selection.omissions[0], value="someone-else"),),
    )
    assert changed_selection.selection_id != selection.selection_id

    builder = ContextFrameBuilder()
    command = BuildContext("task:1", "status", NOW)
    frame = builder.handle(command, selection)
    with pytest.raises(ValueError, match="does not match source_omission_id"):
        replace(frame.omissions[0], value="someone-else")

    tiny = builder.handle(BuildContext("task:1", "status", NOW, max_characters=1), selection)
    projection = next(
        item for item in tiny.omissions if item.stage is ContextOmissionStage.CONTEXT_PROJECTION
    )
    changed_frame = replace(
        tiny,
        omissions=tuple(
            replace(item, value="someone-else") if item is projection else item
            for item in tiny.omissions
        ),
    )
    assert changed_frame.frame_id != tiny.frame_id


def test_context_character_budget_has_an_exact_inclusive_boundary(tmp_path: Path) -> None:
    packet = _packet(tmp_path, (("status", "blocked"), ("owner", "kennedy")))
    query = RetrieveEvidence(
        "owner",
        required_keys=(EvidenceKey("project:blackcell", "status"),),
        max_results=2,
    )
    selection = DeterministicEvidenceRetriever().handle(query, packet)
    builder = ContextFrameBuilder()
    unconstrained = builder.handle(BuildContext("task:1", "owner", NOW), selection)

    exact = builder.handle(
        BuildContext(
            "task:1",
            "owner",
            NOW,
            max_characters=unconstrained.serialized_characters,
        ),
        selection,
    )
    just_below = builder.handle(
        BuildContext(
            "task:1",
            "owner",
            NOW,
            max_characters=unconstrained.serialized_characters - 1,
        ),
        selection,
    )

    assert exact.evidence == unconstrained.evidence
    assert exact.omissions == ()
    assert exact.model_payload_characters == len(exact.model_payload)
    assert tuple(item.predicate for item in just_below.evidence) == ("status",)
    assert just_below.omitted_evidence_count == 1
    assert just_below.omissions[0].predicate == "owner"
    assert just_below.omissions[0].reason is ContextOmissionReason.CHARACTER_BUDGET
    assert just_below.omissions[0].stage is ContextOmissionStage.CONTEXT_PROJECTION
    assert just_below.omissions[0].serialized_characters == (
        unconstrained.serialized_characters - just_below.serialized_characters
    )
    assert "kennedy" not in just_below.model_payload
    assert all(item.value != "kennedy" for item in just_below.evidence)


def test_context_artifacts_reject_incoherent_omission_and_provenance_records(
    tmp_path: Path,
) -> None:
    packet = _packet(tmp_path, (("status", "blocked"), ("owner", "kennedy")))
    selection = DeterministicEvidenceRetriever().handle(
        RetrieveEvidence("status", max_results=1), packet
    )
    builder = ContextFrameBuilder()
    frame = builder.handle(BuildContext("task:1", "status", NOW), selection)
    retrieval_omission = frame.omissions[0]

    with pytest.raises(ValueError, match="cannot declare a model-payload size"):
        replace(retrieval_omission, model_payload_characters=1)
    with pytest.raises(ValueError, match="ordered evidence sources"):
        replace(frame, provenance_event_ids=())

    tiny = builder.handle(BuildContext("task:1", "status", NOW, max_characters=1), selection)
    projection_omission = next(
        item for item in tiny.omissions if item.stage is ContextOmissionStage.CONTEXT_PROJECTION
    )
    with pytest.raises(ValueError, match="cannot reference a source omission"):
        replace(projection_omission, source_omission_id="omission:upstream")


def _packet(tmp_path: Path, facts: tuple[tuple[str, str], ...]):
    store = EventStore(tmp_path / "kernel.sqlite3")
    observations = tuple(
        ObservationInput(
            f"obs:{index}",
            NOW,
            (ObservedClaim(f"claim:{index}", "project:blackcell", predicate, value, 0.9),),
            (EvidencePointer(locator=f"fixture://{index}"),),
        )
        for index, (predicate, value) in enumerate(facts, start=1)
    )
    IngestObservationHandler(store, clock=lambda: NOW).handle(
        IngestObservation("observations:1", 0, "operator", "fixture", "run:1", observations)
    )
    state = OperationalStateProjector().replay(store.read_all())
    return SignalPacketProjector().handle(DeriveSignalPacket("daily", NOW), state)
