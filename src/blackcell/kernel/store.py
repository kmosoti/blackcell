from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from blackcell.kernel._json import canonical_json
from blackcell.kernel.database import connect, initialize_database
from blackcell.kernel.errors import (
    ConcurrencyError,
    EventConflictError,
    EventSequenceError,
    IdempotencyConflict,
)
from blackcell.kernel.events import EventEnvelope


class EventStore:
    """SQLite append-only event store with optimistic stream concurrency."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        initialize_database(self.path)

    def append(self, event: EventEnvelope, *, expected_sequence: int) -> EventEnvelope:
        """Append one occurrence and return the canonical stored envelope.

        ``expected_sequence`` is the caller's last observed stream sequence (zero
        for a new stream). An exact idempotent retry returns its original stored
        occurrence before applying the optimistic concurrency check.
        """

        if expected_sequence < 0:
            raise ValueError("expected_sequence must be non-negative")

        with connect(self.path) as connection:
            connection.execute("begin immediate")
            try:
                existing = self._idempotent_event(connection, event)
                if existing is not None:
                    connection.commit()
                    return existing

                row = connection.execute(
                    "select current_sequence from event_streams where stream_id = ?",
                    (event.stream_id,),
                ).fetchone()
                actual_sequence = int(row["current_sequence"]) if row is not None else 0
                if actual_sequence != expected_sequence:
                    raise ConcurrencyError(event.stream_id, expected_sequence, actual_sequence)
                required_sequence = expected_sequence + 1
                if event.stream_sequence != required_sequence:
                    raise EventSequenceError(
                        f"event {event.event_id} declares sequence {event.stream_sequence}; "
                        f"next sequence for stream {event.stream_id!r} is {required_sequence}"
                    )

                if row is None:
                    connection.execute(
                        "insert into event_streams(stream_id, current_sequence) values (?, 0)",
                        (event.stream_id,),
                    )

                try:
                    cursor = connection.execute(
                        """
                        insert into kernel_events(
                            event_id, stream_id, stream_sequence, event_type, schema_version,
                            recorded_at, effective_at, correlation_id, causation_id, actor,
                            source, payload_json, payload_hash, idempotency_key, idempotency_hash
                        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event.event_id,
                            event.stream_id,
                            event.stream_sequence,
                            event.event_type,
                            event.schema_version,
                            event.recorded_at.isoformat(),
                            event.effective_at.isoformat(),
                            event.correlation_id,
                            event.causation_id,
                            event.actor,
                            event.source,
                            canonical_json(event.payload),
                            event.payload_hash,
                            event.idempotency_key,
                            event.idempotency_hash if event.idempotency_key is not None else None,
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise EventConflictError(
                        f"event {event.event_id} violates event ledger integrity: {error}"
                    ) from error

                changed = connection.execute(
                    """
                    update event_streams set current_sequence = ?
                    where stream_id = ? and current_sequence = ?
                    """,
                    (required_sequence, event.stream_id, expected_sequence),
                ).rowcount
                if changed != 1:  # guarded by begin immediate, retained as an invariant check
                    fresh = self._current_sequence(connection, event.stream_id)
                    raise ConcurrencyError(event.stream_id, expected_sequence, fresh)
                global_position = cursor.lastrowid
                if global_position is None:  # pragma: no cover - SQLite INSERT invariant
                    raise EventConflictError("SQLite did not assign an event position")
                connection.commit()
                return replace(event, global_position=global_position)
            except Exception:
                connection.rollback()
                raise

    def get(self, event_id: str) -> EventEnvelope | None:
        with connect(self.path) as connection:
            row = connection.execute(f"{_EVENT_SELECT} where event_id = ?", (event_id,)).fetchone()
        return None if row is None else _event_from_row(row)

    def read_stream(
        self,
        stream_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> tuple[EventEnvelope, ...]:
        _validate_cursor(after_sequence, limit)
        query = (
            f"{_EVENT_SELECT} where stream_id = ? and stream_sequence > ? order by stream_sequence"
        )
        params: tuple[object, ...] = (stream_id, after_sequence)
        if limit is not None:
            query += " limit ?"
            params += (limit,)
        with connect(self.path) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(_event_from_row(row) for row in rows)

    def read_all(
        self,
        *,
        after_position: int = 0,
        limit: int | None = None,
    ) -> tuple[EventEnvelope, ...]:
        _validate_cursor(after_position, limit)
        query = f"{_EVENT_SELECT} where global_position > ? order by global_position"
        params: tuple[object, ...] = (after_position,)
        if limit is not None:
            query += " limit ?"
            params += (limit,)
        with connect(self.path) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(_event_from_row(row) for row in rows)

    def current_sequence(self, stream_id: str) -> int:
        with connect(self.path) as connection:
            return self._current_sequence(connection, stream_id)

    def __len__(self) -> int:
        with connect(self.path) as connection:
            return int(connection.execute("select count(*) from kernel_events").fetchone()[0])

    @staticmethod
    def _current_sequence(connection: sqlite3.Connection, stream_id: str) -> int:
        row = connection.execute(
            "select current_sequence from event_streams where stream_id = ?", (stream_id,)
        ).fetchone()
        return int(row["current_sequence"]) if row is not None else 0

    @staticmethod
    def _idempotent_event(
        connection: sqlite3.Connection, event: EventEnvelope
    ) -> EventEnvelope | None:
        if event.idempotency_key is None:
            return None
        row = connection.execute(
            f"select {_EVENT_COLUMNS}, idempotency_hash from kernel_events "
            "where stream_id = ? and idempotency_key = ?",
            (event.stream_id, event.idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["idempotency_hash"] != event.idempotency_hash:
            raise IdempotencyConflict(
                f"idempotency key {event.idempotency_key!r} on stream "
                f"{event.stream_id!r} was reused for different content"
            )
        return _event_from_row(row)


_EVENT_COLUMNS = """
global_position, event_id, stream_id, stream_sequence, event_type, schema_version,
       recorded_at, effective_at, correlation_id, causation_id, actor, source,
       payload_json, payload_hash, idempotency_key
""".strip()
_EVENT_SELECT = f"select {_EVENT_COLUMNS} from kernel_events"


def _event_from_row(row: sqlite3.Row) -> EventEnvelope:
    payload = json.loads(str(row["payload_json"]))
    if not isinstance(payload, dict):
        raise TypeError("stored event payload must be a JSON object")
    return EventEnvelope(
        event_id=str(row["event_id"]),
        stream_id=str(row["stream_id"]),
        stream_sequence=int(row["stream_sequence"]),
        event_type=str(row["event_type"]),
        schema_version=int(row["schema_version"]),
        recorded_at=datetime.fromisoformat(str(row["recorded_at"])),
        effective_at=datetime.fromisoformat(str(row["effective_at"])),
        correlation_id=str(row["correlation_id"]),
        causation_id=None if row["causation_id"] is None else str(row["causation_id"]),
        actor=str(row["actor"]),
        source=str(row["source"]),
        payload=payload,
        payload_hash=str(row["payload_hash"]),
        idempotency_key=(None if row["idempotency_key"] is None else str(row["idempotency_key"])),
        global_position=int(row["global_position"]),
    )


def _validate_cursor(cursor: int, limit: int | None) -> None:
    if cursor < 0:
        raise ValueError("cursor must be non-negative")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
