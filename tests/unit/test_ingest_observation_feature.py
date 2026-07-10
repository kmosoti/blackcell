from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from blackcell.features.ingest_observation import (
    EvidencePointer,
    IngestObservation,
    IngestObservationHandler,
    ObservationInput,
    ObservedClaim,
)
from blackcell.features.project_operational_state import OperationalStateProjector
from blackcell.kernel import ConcurrencyError, EventStore, IdempotencyConflict

NOW = datetime(2026, 7, 10, 15, tzinfo=UTC)


def observation(identifier: str, value: str) -> ObservationInput:
    return ObservationInput(
        observation_id=identifier,
        effective_at=NOW,
        claims=(
            ObservedClaim(
                claim_id=f"claim:{identifier}",
                subject="project:blackcell",
                predicate="status",
                value=value,
                confidence=0.8,
            ),
        ),
        evidence=(EvidencePointer(locator=f"fixture://{identifier}"),),
    )


def command(*observations: ObservationInput, expected_sequence: int = 0) -> IngestObservation:
    return IngestObservation(
        stream_id="observations:daily",
        expected_sequence=expected_sequence,
        actor="operator",
        source="fixture",
        correlation_id="run:daily",
        observations=observations,
    )


def test_handler_records_an_atomic_provenance_rich_batch(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    stored = IngestObservationHandler(store, clock=lambda: NOW).handle(
        command(observation("obs:1", "ready"), observation("obs:2", "blocked"))
    )

    assert tuple(event.stream_sequence for event in stored) == (1, 2)
    assert tuple(event.global_position for event in stored) == (1, 2)
    assert stored[0].payload["observation_schema_version"] == "observation/v1"
    evidence = stored[0].payload["evidence"]
    assert isinstance(evidence, tuple)
    pointer = evidence[0]
    assert isinstance(pointer, Mapping)
    pointer_mapping = cast("Mapping[str, object]", pointer)
    assert pointer_mapping["locator"] == "fixture://obs:1"
    assert (stored[0].actor, stored[0].source, stored[0].correlation_id) == (
        "operator",
        "fixture",
        "run:daily",
    )


def test_ingested_events_project_directly_into_operational_state(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    IngestObservationHandler(store, clock=lambda: NOW).handle(
        command(observation("obs:1", "ready"))
    )

    state = OperationalStateProjector().replay(store.read_all())

    assert state.claims[0].value == "ready"
    assert state.claims[0].source_event_id == store.read_all()[0].event_id
    assert state.claims[0].confidence == 0.8


def test_exact_retry_returns_original_events_and_divergence_is_rejected(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    handler = IngestObservationHandler(store, clock=lambda: NOW)
    original = handler.handle(command(observation("obs:1", "ready")))

    assert handler.handle(command(observation("obs:1", "ready"))) == original
    with pytest.raises(IdempotencyConflict):
        handler.handle(command(observation("obs:1", "changed")))
    assert len(store) == 1


def test_stale_batch_does_not_partially_append(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    handler = IngestObservationHandler(store, clock=lambda: NOW)
    handler.handle(command(observation("obs:1", "ready")))

    with pytest.raises(ConcurrencyError):
        handler.handle(command(observation("obs:2", "blocked")))

    assert tuple(event.payload["observation_id"] for event in store.read_all()) == ("obs:1",)


def test_inputs_require_evidence_unique_ids_and_finite_confidence() -> None:
    claim = ObservedClaim("claim:1", "project", "status", "ready")
    with pytest.raises(ValueError, match="evidence"):
        ObservationInput("obs:1", NOW, (claim,), ())
    with pytest.raises(ValueError, match="unique"):
        ObservationInput(
            "obs:1",
            NOW,
            (claim, claim),
            (EvidencePointer(locator="fixture://one"),),
        )
    with pytest.raises(ValueError, match="finite"):
        ObservedClaim("claim:2", "project", "status", "ready", float("nan"))
