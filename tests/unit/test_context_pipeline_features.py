from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from blackcell.features.build_context import BuildContext, ContextBudgetError, ContextFrameBuilder
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
    assert frame.omitted_evidence_count == 1
    assert frame.frame_id.startswith("sha256:")


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
    first_only = replace(
        selection,
        candidates=selection.candidates[:1],
        omitted_count=selection.omitted_count + 1,
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
