from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from blackcell.features.derive_signal_packet import DeriveSignalPacket, project_signal_packet
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

    packet = project_signal_packet(
        DeriveSignalPacket("daily", NOW, stale_after_seconds=3_600), state
    )

    assert packet.schema_version == "signal-packet/v2"
    assert packet.purpose == "daily"
    assert (packet.state_domain, packet.state_stream_id) == ("repository", "observations:1")
    assert packet.state_position == 2
    assert packet.state_global_position == 2
    assert packet.state_stream_position == 2
    assert packet.stale_claim_count == 2
    assert packet.mean_confidence == 0.7
    assert len(packet.conflicts) == 1
    assert packet.conflicts[0].claim_ids == tuple(claim.claim_id for claim in packet.claims)
    assert {
        (
            claim.claim_id,
            claim.domain,
            claim.stream_id,
            claim.stream_sequence,
            claim.global_position,
        )
        for claim in packet.claims
    } == {
        ("claim:obs:1", "repository", "observations:1", 1, 1),
        ("claim:obs:2", "repository", "observations:1", 2, 2),
    }
    expected_provenance = tuple(sorted(event.event_id for event in store.read_all()))
    assert packet.provenance_event_ids == expected_provenance
    assert packet.packet_id.startswith("sha256:")

    repurposed = replace(packet, purpose="incident-review")
    assert repurposed.state_domain == packet.state_domain
    assert repurposed.packet_id != packet.packet_id
    with pytest.raises(ValueError, match="state scope"):
        replace(packet, state_domain="personal-planning")
    with pytest.raises(ValueError, match="mean claim confidence"):
        replace(packet, mean_confidence=0.0)
    with pytest.raises(ValueError, match="number of stale claims"):
        replace(packet, stale_claim_count=0)
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(packet, generated_at=NOW.replace(tzinfo=None))


def test_signal_packet_is_deterministic_and_empty_state_is_explicit(tmp_path: Path) -> None:
    state = OperationalStateProjector().replay(())
    command = DeriveSignalPacket("daily", NOW)

    first = project_signal_packet(command, state)
    second = project_signal_packet(command, state)

    assert first == second
    assert first.claims == first.conflicts == first.provenance_event_ids == ()
    assert first.mean_confidence == 0.0
    assert first.state_position == 0


def test_signal_claim_identity_is_event_and_claim_composite(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    observation = ObservationInput(
        "obs:shared",
        NOW,
        (
            ObservedClaim("claim:status", "project:blackcell", "status", "ready"),
            ObservedClaim("claim:owner", "project:blackcell", "owner", "kennedy"),
        ),
        (EvidencePointer(locator="fixture://shared"),),
    )
    IngestObservationHandler(store, clock=lambda: NOW).handle(
        IngestObservation("observations:1", 0, "operator", "fixture", "run:1", (observation,))
    )

    packet = project_signal_packet(
        DeriveSignalPacket("daily", NOW),
        OperationalStateProjector().replay(store.read_all()),
    )

    assert {claim.claim_id for claim in packet.claims} == {"claim:status", "claim:owner"}
    assert len({claim.source_event_id for claim in packet.claims}) == 1
    assert len({(claim.source_event_id, claim.claim_id) for claim in packet.claims}) == 2


def _observation(
    identifier: str,
    value: str,
    confidence: float,
    effective_at: datetime,
) -> ObservationInput:
    return ObservationInput(
        identifier,
        effective_at,
        (
            ObservedClaim(
                f"claim:{identifier}",
                "project:blackcell",
                "status",
                value,
                confidence,
            ),
        ),
        (EvidencePointer(locator=f"fixture://{identifier}"),),
    )
