from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from blackcell.kernel import (
    CheckpointStore,
    EventEnvelope,
    EventStore,
    JsonInput,
    ProjectionConflict,
    ProjectionRunner,
)

NOW = datetime(2026, 7, 9, 12, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class CountState:
    total: int
    event_ids: tuple[str, ...]


class CountProjection:
    name = "observation-count"
    version = 1

    def initial_state(self) -> CountState:
        return CountState(0, ())

    def apply(self, state: CountState, event: EventEnvelope) -> CountState:
        amount = event.payload["amount"]
        assert isinstance(amount, int)
        return CountState(state.total + amount, (*state.event_ids, event.event_id))

    def dump_state(self, state: CountState) -> dict[str, JsonInput]:
        return {"event_ids": list(state.event_ids), "total": state.total}

    def load_state(self, value: object) -> CountState:
        assert isinstance(value, dict)
        mapping = cast("dict[str, object]", value)
        ids = mapping["event_ids"]
        assert isinstance(ids, list)
        total = mapping["total"]
        assert isinstance(total, int)
        return CountState(total, tuple(str(item) for item in ids))


def append_amount(store: EventStore, sequence: int, amount: int) -> EventEnvelope:
    return store.append(
        EventEnvelope.create(
            stream_id="counter:1",
            stream_sequence=sequence,
            event_type="AmountObserved",
            actor="test",
            source="fixture",
            payload={"amount": amount},
            recorded_at=NOW,
            correlation_id="run:1",
        ),
        expected_sequence=sequence - 1,
    )


def test_historical_replay_is_deterministic_and_uses_only_recorded_events(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "kernel.sqlite3")
    events = (append_amount(store, 1, 2), append_amount(store, 2, 3))
    runner = ProjectionRunner()
    projection = CountProjection()

    first = runner.replay(projection, events)
    second = runner.replay(projection, store.read_all())

    assert first == second
    assert first.state == CountState(5, tuple(item.event_id for item in events))
    assert first.processed_events == 2
    assert first.state_hash == second.state_hash


def test_checkpoint_persists_and_resumes_without_refolding_history(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    event_store = EventStore(path)
    append_amount(event_store, 1, 2)
    append_amount(event_store, 2, 3)
    runner = ProjectionRunner()
    projection = CountProjection()
    initial = runner.rebuild(event_store, projection, stream_id="counter:1")
    checkpoint = initial.checkpoint(projection, stream_id="counter:1")
    checkpoints = CheckpointStore(path)

    assert checkpoints.save(checkpoint, expected_position=0) == checkpoint
    loaded = CheckpointStore(path).load(projection.name, projection.version, stream_id="counter:1")
    assert loaded == checkpoint
    duplicate = checkpoints.save(checkpoint, expected_position=checkpoint.last_global_position)
    assert duplicate == checkpoint

    third = append_amount(event_store, 3, 7)
    resumed = runner.rebuild(
        event_store,
        projection,
        stream_id="counter:1",
        checkpoint=loaded,
    )
    assert resumed.state.total == 12
    assert resumed.processed_events == 1
    assert resumed.state.event_ids[-1] == third.event_id

    updated = resumed.checkpoint(projection, stream_id="counter:1")
    checkpoints.save(updated, expected_position=checkpoint.last_global_position)
    assert checkpoints.load(projection.name, projection.version, stream_id="counter:1") == updated


def test_checkpoint_optimistic_concurrency_and_regression_guards(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    store = EventStore(path)
    append_amount(store, 1, 1)
    projection = CountProjection()
    result = ProjectionRunner().rebuild(store, projection, stream_id="counter:1")
    checkpoint = result.checkpoint(projection, stream_id="counter:1")
    checkpoints = CheckpointStore(path)
    checkpoints.save(checkpoint, expected_position=0)

    with pytest.raises(ProjectionConflict, match="expected position"):
        checkpoints.save(checkpoint, expected_position=99)


def test_replay_rejects_unstored_or_unordered_input(tmp_path: Path) -> None:
    unstored = EventEnvelope.create(
        stream_id="counter:1",
        stream_sequence=1,
        event_type="AmountObserved",
        actor="test",
        source="fixture",
        payload={"amount": 1},
    )
    with pytest.raises(ValueError, match="EventStore"):
        ProjectionRunner().replay(CountProjection(), (unstored,))

    store = EventStore(tmp_path / "kernel.sqlite3")
    first = append_amount(store, 1, 1)
    second = append_amount(store, 2, 1)
    with pytest.raises(ValueError, match="ordered"):
        ProjectionRunner().replay(CountProjection(), (second, first))
