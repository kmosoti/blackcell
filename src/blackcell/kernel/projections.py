from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, TypeVar, runtime_checkable

from blackcell.kernel._json import (
    JsonInput,
    JsonValue,
    canonical_json,
    freeze_json,
    json_digest,
    thaw_json,
)
from blackcell.kernel.database import connect, initialize_database
from blackcell.kernel.errors import ProjectionConflict
from blackcell.kernel.events import EventEnvelope, utc_now
from blackcell.kernel.store import EventStore

StateT = TypeVar("StateT")


@runtime_checkable
class Projection(Protocol[StateT]):
    """Pure fold contract for deriving state from recorded events."""

    name: str
    version: int

    def initial_state(self) -> StateT: ...

    def apply(self, state: StateT, event: EventEnvelope) -> StateT: ...

    def dump_state(self, state: StateT) -> JsonInput: ...

    def load_state(self, value: object) -> StateT: ...


@dataclass(frozen=True, slots=True)
class ProjectionCheckpoint:
    projection_name: str
    projection_version: int
    stream_id: str | None
    last_global_position: int
    last_stream_sequence: int | None
    state: JsonValue
    state_hash: str
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.projection_name.strip():
            raise ValueError("projection_name must not be empty")
        if self.projection_version < 1:
            raise ValueError("projection_version must be at least 1")
        if self.last_global_position < 0:
            raise ValueError("last_global_position must be non-negative")
        if self.last_stream_sequence is not None and self.last_stream_sequence < 0:
            raise ValueError("last_stream_sequence must be non-negative")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")
        frozen = freeze_json(self.state, path="$.state")
        object.__setattr__(self, "state", frozen)
        calculated = json_digest(frozen)
        if calculated != self.state_hash:
            raise ValueError(
                f"projection checkpoint state hash mismatch: "
                f"expected {self.state_hash}, calculated {calculated}"
            )

    @classmethod
    def create(
        cls,
        *,
        projection_name: str,
        projection_version: int,
        state: JsonInput,
        last_global_position: int,
        stream_id: str | None = None,
        last_stream_sequence: int | None = None,
        updated_at: datetime | None = None,
    ) -> ProjectionCheckpoint:
        frozen = freeze_json(state, path="$.state")
        return cls(
            projection_name=projection_name,
            projection_version=projection_version,
            stream_id=stream_id,
            last_global_position=last_global_position,
            last_stream_sequence=last_stream_sequence,
            state=frozen,
            state_hash=json_digest(frozen),
            updated_at=updated_at or utc_now(),
        )


@dataclass(frozen=True, slots=True)
class ReplayResult[StateT]:
    state: StateT
    state_hash: str
    processed_events: int
    last_global_position: int
    last_stream_sequence: int | None

    def checkpoint(
        self, projection: Projection[StateT], *, stream_id: str | None = None
    ) -> ProjectionCheckpoint:
        return ProjectionCheckpoint.create(
            projection_name=projection.name,
            projection_version=projection.version,
            stream_id=stream_id,
            last_global_position=self.last_global_position,
            last_stream_sequence=self.last_stream_sequence if stream_id is not None else None,
            state=projection.dump_state(self.state),
        )


class CheckpointStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        initialize_database(self.path)

    def load(
        self,
        projection_name: str,
        projection_version: int,
        *,
        stream_id: str | None = None,
    ) -> ProjectionCheckpoint | None:
        scope = _scope(stream_id)
        with connect(self.path) as connection:
            row = connection.execute(
                """
                select projection_name, projection_version, scope, last_global_position,
                       last_stream_sequence, state_json, state_hash, updated_at
                from projection_checkpoints
                where projection_name = ? and projection_version = ? and scope = ?
                """,
                (projection_name, projection_version, scope),
            ).fetchone()
        if row is None:
            return None
        return _checkpoint_from_row(row)

    def save(
        self,
        checkpoint: ProjectionCheckpoint,
        *,
        expected_position: int | None = None,
    ) -> ProjectionCheckpoint:
        scope = _scope(checkpoint.stream_id)
        with connect(self.path) as connection:
            connection.execute("begin immediate")
            try:
                row = connection.execute(
                    """
                    select projection_name, projection_version, scope, last_global_position,
                           last_stream_sequence, state_json, state_hash, updated_at
                    from projection_checkpoints
                    where projection_name = ? and projection_version = ? and scope = ?
                    """,
                    (checkpoint.projection_name, checkpoint.projection_version, scope),
                ).fetchone()
                current_position = int(row["last_global_position"]) if row is not None else 0
                if expected_position is not None and expected_position != current_position:
                    raise ProjectionConflict(
                        f"checkpoint {checkpoint.projection_name!r} expected position "
                        f"{expected_position}, current position is {current_position}"
                    )
                if checkpoint.last_global_position < current_position:
                    raise ProjectionConflict(
                        f"checkpoint {checkpoint.projection_name!r} would regress from "
                        f"position {current_position} to {checkpoint.last_global_position}"
                    )
                if row is not None and checkpoint.last_global_position == current_position:
                    same = (
                        row["state_hash"] == checkpoint.state_hash
                        and row["last_stream_sequence"] == checkpoint.last_stream_sequence
                    )
                    if same:
                        existing = _checkpoint_from_row(row)
                        connection.commit()
                        return existing
                    raise ProjectionConflict(
                        f"checkpoint {checkpoint.projection_name!r} has divergent state "
                        f"at position {current_position}"
                    )

                connection.execute(
                    """
                    insert into projection_checkpoints(
                        projection_name, projection_version, scope, last_global_position,
                        last_stream_sequence, state_json, state_hash, updated_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(projection_name, projection_version, scope) do update set
                        last_global_position = excluded.last_global_position,
                        last_stream_sequence = excluded.last_stream_sequence,
                        state_json = excluded.state_json,
                        state_hash = excluded.state_hash,
                        updated_at = excluded.updated_at
                    """,
                    (
                        checkpoint.projection_name,
                        checkpoint.projection_version,
                        scope,
                        checkpoint.last_global_position,
                        checkpoint.last_stream_sequence,
                        canonical_json(checkpoint.state),
                        checkpoint.state_hash,
                        checkpoint.updated_at.isoformat(),
                    ),
                )
                connection.commit()
                return checkpoint
            except Exception:
                connection.rollback()
                raise


class ProjectionRunner:
    """Rebuild projections exclusively from immutable, already-recorded events."""

    def replay(
        self,
        projection: Projection[StateT],
        events: tuple[EventEnvelope, ...],
        *,
        checkpoint: ProjectionCheckpoint | None = None,
    ) -> ReplayResult[StateT]:
        if projection.version < 1:
            raise ValueError("projection version must be at least 1")
        if checkpoint is not None:
            self._validate_checkpoint(projection, checkpoint)
            state = projection.load_state(thaw_json(checkpoint.state))
            last_position = checkpoint.last_global_position
            last_sequence = checkpoint.last_stream_sequence
        else:
            state = projection.initial_state()
            last_position = 0
            last_sequence = None

        processed = 0
        previous_input_position = 0
        for event in events:
            if event.global_position is None:
                raise ValueError("historical replay requires events read from an EventStore")
            if event.global_position <= previous_input_position:
                raise ValueError("replay events must be ordered by global position")
            previous_input_position = event.global_position
            if event.global_position <= last_position:
                continue
            if (
                checkpoint is not None
                and checkpoint.stream_id is not None
                and event.stream_id != checkpoint.stream_id
            ):
                raise ValueError("stream checkpoint cannot consume a different event stream")
            state = projection.apply(state, event)
            last_position = event.global_position
            last_sequence = event.stream_sequence
            processed += 1

        serialized = projection.dump_state(state)
        return ReplayResult(
            state=state,
            state_hash=json_digest(serialized),
            processed_events=processed,
            last_global_position=last_position,
            last_stream_sequence=last_sequence,
        )

    def rebuild(
        self,
        store: EventStore,
        projection: Projection[StateT],
        *,
        stream_id: str | None = None,
        checkpoint: ProjectionCheckpoint | None = None,
    ) -> ReplayResult[StateT]:
        after_position = checkpoint.last_global_position if checkpoint is not None else 0
        if stream_id is None:
            events = store.read_all(after_position=after_position)
        else:
            after_sequence = (
                checkpoint.last_stream_sequence
                if checkpoint is not None and checkpoint.last_stream_sequence is not None
                else 0
            )
            events = store.read_stream(stream_id, after_sequence=after_sequence)
        return self.replay(projection, events, checkpoint=checkpoint)

    @staticmethod
    def _validate_checkpoint(
        projection: Projection[StateT], checkpoint: ProjectionCheckpoint
    ) -> None:
        if checkpoint.projection_name != projection.name:
            raise ValueError("checkpoint belongs to a different projection")
        if checkpoint.projection_version != projection.version:
            raise ValueError("checkpoint belongs to a different projection version")


def _scope(stream_id: str | None) -> str:
    if stream_id == "*":
        raise ValueError("stream_id '*' is reserved for the global projection scope")
    if stream_id is not None and not stream_id.strip():
        raise ValueError("stream_id must not be empty")
    return "*" if stream_id is None else stream_id


def _checkpoint_from_row(row: sqlite3.Row) -> ProjectionCheckpoint:
    state = json.loads(str(row["state_json"]))
    scope = str(row["scope"])
    return ProjectionCheckpoint(
        projection_name=str(row["projection_name"]),
        projection_version=int(row["projection_version"]),
        stream_id=None if scope == "*" else scope,
        last_global_position=int(row["last_global_position"]),
        last_stream_sequence=(
            None if row["last_stream_sequence"] is None else int(row["last_stream_sequence"])
        ),
        state=freeze_json(state),
        state_hash=str(row["state_hash"]),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )
