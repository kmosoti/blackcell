from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from blackcell.features.project_operational_state import OperationalStateProjector
from blackcell.kernel import EventEnvelope, EventStore

NOW = datetime(2026, 7, 10, 12, tzinfo=UTC)


def observation(
    sequence: int,
    value: str,
    *,
    effective_at: datetime = NOW,
    confidence: float = 0.8,
) -> EventEnvelope:
    return EventEnvelope.create(
        stream_id="observations:daily",
        stream_sequence=sequence,
        event_type="observation.recorded",
        actor="operator",
        source="fixture",
        payload={
            "claims": [
                {
                    "subject": "project:blackcell",
                    "predicate": "status",
                    "value": value,
                    "confidence": confidence,
                }
            ]
        },
        recorded_at=NOW,
        effective_at=effective_at,
        correlation_id="run:daily",
    )


def test_projection_preserves_confidence_time_and_provenance(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    stored = store.append(observation(1, "ready", confidence=0.7), expected_sequence=0)

    state = OperationalStateProjector().replay(store.read_all())

    assert state.last_global_position == 1
    assert state.conflicts == ()
    assert state.claims_for("project:blackcell", "status") == state.claims
    claim = state.claims[0]
    assert (claim.value, claim.confidence) == ("ready", 0.7)
    assert (claim.source_event_id, claim.source, claim.actor) == (
        stored.event_id,
        "fixture",
        "operator",
    )


def test_equal_time_disagreement_remains_an_explicit_conflict(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    store.append_many(
        (observation(1, "ready"), observation(2, "blocked")),
        expected_sequences={"observations:daily": 0},
    )

    state = OperationalStateProjector().replay(store.read_all())

    assert {claim.value for claim in state.claims} == {"ready", "blocked"}
    assert len(state.conflicts) == 1
    assert set(state.conflicts[0].values) == {"ready", "blocked"}


def test_newer_evidence_supersedes_candidates_and_older_evidence_is_ignored(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    store.append_many(
        (
            observation(1, "ready"),
            observation(2, "blocked", effective_at=NOW + timedelta(minutes=1)),
            observation(3, "stale", effective_at=NOW - timedelta(minutes=1)),
        ),
        expected_sequences={"observations:daily": 0},
    )

    state = OperationalStateProjector().replay(store.read_all())

    assert tuple(claim.value for claim in state.claims) == ("blocked",)
    assert state.conflicts == ()


def test_projection_requires_stored_globally_ordered_events(tmp_path: Path) -> None:
    unstored = observation(1, "ready")
    with pytest.raises(ValueError, match="stored"):
        OperationalStateProjector().replay((unstored,))

    store = EventStore(tmp_path / "kernel.sqlite3")
    first, second = store.append_many(
        (observation(1, "ready"), observation(2, "blocked")),
        expected_sequences={"observations:daily": 0},
    )
    with pytest.raises(ValueError, match="ordered"):
        OperationalStateProjector().replay((second, first))
