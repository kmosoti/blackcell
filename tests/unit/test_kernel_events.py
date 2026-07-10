from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from blackcell.kernel import (
    ConcurrencyError,
    EventConflictError,
    EventEnvelope,
    EventIntegrityError,
    EventSequenceError,
    EventStore,
    IdempotencyConflict,
    JsonInput,
    SchemaVersionError,
)

NOW = datetime(2026, 7, 9, 12, tzinfo=UTC)


def event(
    sequence: int,
    *,
    stream_id: str = "task:1",
    payload: Mapping[str, JsonInput] | None = None,
    idempotency_key: str | None = None,
    correlation_id: str = "run:1",
    causation_id: str | None = None,
) -> EventEnvelope:
    return EventEnvelope.create(
        stream_id=stream_id,
        stream_sequence=sequence,
        event_type="ObservationRecorded",
        actor="test",
        source="fixture",
        payload=payload or {"sequence": sequence},
        recorded_at=NOW,
        effective_at=NOW,
        correlation_id=correlation_id,
        causation_id=causation_id,
        idempotency_key=idempotency_key,
    )


def test_envelope_is_deeply_immutable_and_exposes_compatibility_aliases() -> None:
    source: dict[str, JsonInput] = {"nested": {"values": [1, 2]}}
    envelope = event(1, payload=source)
    source["nested"] = "changed"

    assert envelope.payload["nested"] == {"values": (1, 2)}
    assert envelope.sequence == envelope.stream_sequence == 1
    assert envelope.kind == envelope.event_type
    assert envelope.occurred_at == envelope.recorded_at
    actor_field = "actor"
    with pytest.raises(FrozenInstanceError):
        setattr(envelope, actor_field, "changed")
    payload_setitem = getattr(envelope.payload, "__setitem__", None)
    assert payload_setitem is None
    mutable_payload: Any = envelope.payload
    with pytest.raises(TypeError):
        mutable_payload["new"] = True


def test_append_preserves_occurrences_and_stream_order(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    first = store.append(event(1, payload={"same": True}), expected_sequence=0)
    second = store.append(event(2, payload={"same": True}), expected_sequence=1)

    assert first.event_id != second.event_id
    assert (first.global_position, second.global_position) == (1, 2)
    assert store.current_sequence("task:1") == 2
    assert store.read_stream("task:1") == (first, second)
    assert store.read_all(limit=1) == (first,)
    assert store.read_all(after_position=1) == (second,)
    assert store.get(first.event_id) == first
    assert len(store) == 2


def test_expected_sequence_and_declared_sequence_are_independent_guards(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    store.append(event(1), expected_sequence=0)

    with pytest.raises(ConcurrencyError) as stale:
        store.append(event(2), expected_sequence=0)
    assert (stale.value.expected, stale.value.actual) == (0, 1)

    with pytest.raises(EventSequenceError):
        store.append(event(3), expected_sequence=1)
    assert store.current_sequence("task:1") == 1


def test_idempotency_returns_original_occurrence_and_rejects_divergent_reuse(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    original = event(1, payload={"value": 1}, idempotency_key="observe-once")
    stored = store.append(original, expected_sequence=0)
    retry = event(99, payload={"value": 1}, idempotency_key="observe-once")

    assert store.append(retry, expected_sequence=77) == stored
    assert len(store) == 1

    divergent = event(2, payload={"value": 2}, idempotency_key="observe-once")
    with pytest.raises(IdempotencyConflict):
        store.append(divergent, expected_sequence=1)
    assert len(store) == 1


def test_causation_requires_an_existing_event(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    with pytest.raises(Exception, match="ledger integrity"):
        store.append(event(1, causation_id="missing-event"), expected_sequence=0)

    cause = store.append(event(1), expected_sequence=0)
    effect = event(2, causation_id=cause.event_id)
    assert store.append(effect, expected_sequence=1).causation_id == cause.event_id


def test_append_many_is_atomic_across_ordered_streams(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    first_a = event(1, stream_id="task:a")
    first_b = event(1, stream_id="task:b")
    second_a = event(2, stream_id="task:a")

    stored = store.append_many(
        (first_a, first_b, second_a),
        expected_sequences={"task:a": 0, "task:b": 0},
    )

    assert tuple(item.global_position for item in stored) == (1, 2, 3)
    assert store.read_stream("task:a") == (stored[0], stored[2])
    assert store.read_stream("task:b") == (stored[1],)
    assert store.current_sequence("task:a") == 2
    assert store.current_sequence("task:b") == 1


def test_append_many_rolls_back_every_stream_when_one_event_fails(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    valid = event(1, stream_id="task:a")
    invalid = event(1, stream_id="task:b", causation_id="missing")

    with pytest.raises(EventConflictError, match="ledger integrity"):
        store.append_many(
            (valid, invalid),
            expected_sequences={"task:a": 0, "task:b": 0},
        )

    assert store.read_all() == ()
    assert store.current_sequence("task:a") == 0
    assert store.current_sequence("task:b") == 0


def test_append_many_accepts_causation_from_an_earlier_batch_event(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    cause = event(1, stream_id="task:a")
    effect = event(1, stream_id="task:b", causation_id=cause.event_id)

    stored = store.append_many(
        (cause, effect),
        expected_sequences={"task:a": 0, "task:b": 0},
    )

    assert stored[1].causation_id == stored[0].event_id


def test_append_many_exact_retry_returns_original_occurrences(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    original = (
        event(1, idempotency_key="one", payload={"value": 1}),
        event(2, idempotency_key="two", payload={"value": 2}),
    )
    stored = store.append_many(original, expected_sequences={"task:1": 0})
    retry = (
        event(90, idempotency_key="one", payload={"value": 1}),
        event(91, idempotency_key="two", payload={"value": 2}),
    )

    assert store.append_many(retry, expected_sequences={"task:1": 89}) == stored
    assert len(store) == 2


def test_append_many_rejects_out_of_order_all_idempotent_retry(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    first = event(1, idempotency_key="one", payload={"value": 1})
    second = event(2, idempotency_key="two", payload={"value": 2})
    store.append_many((first, second), expected_sequences={"task:1": 0})

    retry_second = event(99, idempotency_key="two", payload={"value": 2})
    retry_first = event(98, idempotency_key="one", payload={"value": 1})
    with pytest.raises(EventSequenceError, match="not ordered"):
        store.append_many((retry_second, retry_first), expected_sequences={"task:1": 97})

    divergent = event(3, idempotency_key="two", payload={"value": "changed"})
    with pytest.raises(IdempotencyConflict):
        store.append_many((divergent,), expected_sequences={"task:1": 2})
    assert len(store) == 2


def test_append_many_rejects_stale_stream_and_rolls_back_other_streams(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    store.append(event(1, stream_id="task:b"), expected_sequence=0)

    with pytest.raises(ConcurrencyError):
        store.append_many(
            (event(1, stream_id="task:a"), event(2, stream_id="task:b")),
            expected_sequences={"task:a": 0, "task:b": 0},
        )

    assert store.read_stream("task:a") == ()
    assert store.current_sequence("task:a") == 0
    assert store.current_sequence("task:b") == 1


def test_append_many_can_resume_after_an_idempotent_committed_prefix(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    first = event(1, idempotency_key="one", payload={"value": 1})
    stored_first = store.append(first, expected_sequence=0)
    retry = event(50, idempotency_key="one", payload={"value": 1})
    second = event(2, idempotency_key="two")

    stored = store.append_many((retry, second), expected_sequences={"task:1": 0})

    assert stored[0] == stored_first
    assert stored[1].stream_sequence == 2
    assert store.current_sequence("task:1") == 2


def test_database_reopens_with_wal_and_persisted_events(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    first_store = EventStore(path)
    stored = first_store.append(event(1), expected_sequence=0)
    second_store = EventStore(path)

    assert second_store.read_stream("task:1") == (stored,)
    with sqlite3.connect(path) as connection:
        assert connection.execute("pragma user_version").fetchone()[0] == 1
        assert connection.execute("pragma journal_mode").fetchone()[0] == "wal"


def test_read_detects_payload_tampering(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    store = EventStore(path)
    stored = store.append(event(1), expected_sequence=0)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "update kernel_events set payload_json = ? where event_id = ?",
            ('{"sequence":999}', stored.event_id),
        )

    with pytest.raises(EventIntegrityError, match="payload hash mismatch"):
        store.get(stored.event_id)


def test_newer_database_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("pragma user_version = 999")

    with pytest.raises(SchemaVersionError, match="newer"):
        EventStore(path)


@settings(max_examples=35, deadline=None)
@given(values=st.lists(st.text(min_size=0, max_size=30), min_size=1, max_size=15))
def test_stream_round_trip_property(values: list[str]) -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = EventStore(Path(directory) / "kernel.sqlite3")
        appended = tuple(
            store.append(event(index, payload={"value": value}), expected_sequence=index - 1)
            for index, value in enumerate(values, start=1)
        )

        loaded = store.read_stream("task:1")
        assert loaded == appended
        assert [item.payload["value"] for item in loaded] == values
        assert len({item.event_id for item in loaded}) == len(values)
