from datetime import UTC, datetime, timedelta
from pathlib import Path

from blackcell.features.derive_signal_packet import DeriveSignalPacket, SignalPacketProjector
from blackcell.features.ingest_observation import (
    EvidencePointer,
    IngestObservation,
    IngestObservationHandler,
    ObservationInput,
    ObservedClaim,
)
from blackcell.features.project_operational_state import OperationalStateProjector
from blackcell.kernel import EventStore

NOW = datetime(2026, 7, 10, 16, tzinfo=UTC)


def test_signal_packet_summarizes_freshness_conflicts_and_provenance(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    observations = (
        _observation("obs:1", "ready", 0.9, NOW - timedelta(hours=2)),
        _observation("obs:2", "blocked", 0.5, NOW - timedelta(hours=2)),
    )
    IngestObservationHandler(store, clock=lambda: NOW).handle(
        IngestObservation("observations:1", 0, "operator", "fixture", "run:1", observations)
    )
    state = OperationalStateProjector().replay(store.read_all())

    packet = SignalPacketProjector().handle(
        DeriveSignalPacket("daily", NOW, stale_after_seconds=3_600), state
    )

    assert packet.schema_version == "signal-packet/v1"
    assert packet.state_position == 2
    assert packet.stale_claim_count == 2
    assert packet.mean_confidence == 0.7
    assert len(packet.conflicts) == 1
    expected_provenance = tuple(sorted(event.event_id for event in store.read_all()))
    assert packet.provenance_event_ids == expected_provenance
    assert packet.packet_id.startswith("sha256:")


def test_signal_packet_is_deterministic_and_empty_state_is_explicit(tmp_path: Path) -> None:
    state = OperationalStateProjector().replay(())
    command = DeriveSignalPacket("daily", NOW)
    projector = SignalPacketProjector()

    first = projector.handle(command, state)
    second = projector.handle(command, state)

    assert first == second
    assert first.claims == first.conflicts == first.provenance_event_ids == ()
    assert first.mean_confidence == 0.0
    assert first.state_position == 0


def _observation(
    identifier: str,
    value: str,
    confidence: float,
    effective_at: datetime,
) -> ObservationInput:
    return ObservationInput(
        identifier,
        effective_at,
        (ObservedClaim(f"claim:{identifier}", "project:blackcell", "status", value, confidence),),
        (EvidencePointer(locator=f"fixture://{identifier}"),),
    )
