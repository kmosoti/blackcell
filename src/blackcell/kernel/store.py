from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
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

        return self.append_many(
            (event,),
            expected_sequences={event.stream_id: expected_sequence},
        )[0]

    def append_many(
        self,
        events: Sequence[EventEnvelope],
        *,
        expected_sequences: Mapping[str, int],
    ) -> tuple[EventEnvelope, ...]:
        """Atomically append an ordered batch spanning one or more streams.

        ``expected_sequences`` contains the caller's last observed sequence for
        every stream in the batch. Events for a stream must appear in declared
        stream-sequence order, although events from different streams may be
        interleaved. A complete exact idempotent retry returns the original
        occurrences before applying optimistic concurrency checks, matching
        :meth:`append` semantics.

        Exact idempotent occurrences may also form a committed prefix for a
        stream. This permits a caller to append the remaining suffix without
        weakening concurrency checks for unrelated intervening events.
        """

        batch = tuple(events)
        if not batch:
            return ()
        with connect(self.path) as connection:
            connection.execute("begin immediate")
            try:
                appended = self.append_many_in_transaction(
                    connection,
                    batch,
                    expected_sequences=expected_sequences,
                )
                connection.commit()
                return appended
            except Exception:
                connection.rollback()
                raise

    def append_many_in_transaction(
        self,
        connection: sqlite3.Connection,
        events: Sequence[EventEnvelope],
        *,
        expected_sequences: Mapping[str, int],
    ) -> tuple[EventEnvelope, ...]:
        """Append a batch inside the caller's active transaction.

        The connection must target this store's main database, have foreign-key
        enforcement enabled, and already hold an explicit transaction. This
        method never commits or rolls back; the caller owns the complete atomic
        unit, including any adapter state written through the same connection.
        """

        self._validate_transaction_connection(connection)
        batch = tuple(events)
        if not batch:
            return ()
        stream_ids = self._validate_append_request(batch, expected_sequences)

        existing = tuple(self._idempotent_event(connection, event) for event in batch)
        if all(event is not None for event in existing):
            self._validate_existing_batch_order(existing)
            return tuple(event for event in existing if event is not None)

        grouped: dict[str, list[tuple[EventEnvelope, EventEnvelope | None]]] = {
            stream_id: [] for stream_id in stream_ids
        }
        for candidate, stored in zip(batch, existing, strict=True):
            grouped[candidate.stream_id].append((candidate, stored))

        current_sequences: dict[str, int] = {}
        committed_prefixes: dict[str, int] = {}
        for stream_id, stream_events in grouped.items():
            expected = expected_sequences[stream_id]
            actual = self._current_sequence(connection, stream_id)
            current_sequences[stream_id] = actual
            prefix = self._validate_batch_stream(
                stream_id,
                stream_events,
                expected_sequence=expected,
                actual_sequence=actual,
            )
            committed_prefixes[stream_id] = prefix
            if actual == 0:
                connection.execute(
                    "insert into event_streams(stream_id, current_sequence) values (?, 0)",
                    (stream_id,),
                )

        appended: list[EventEnvelope] = []
        for candidate, stored in zip(batch, existing, strict=True):
            if stored is not None:
                appended.append(stored)
                continue
            appended.append(self._insert_event(connection, candidate))

        for stream_id, stream_events in grouped.items():
            new_count = len(stream_events) - committed_prefixes[stream_id]
            if new_count == 0:
                continue
            previous = current_sequences[stream_id]
            required = previous + new_count
            changed = connection.execute(
                """
                update event_streams set current_sequence = ?
                where stream_id = ? and current_sequence = ?
                """,
                (required, stream_id, previous),
            ).rowcount
            if changed != 1:  # guarded by begin immediate; retained as an invariant check
                fresh = self._current_sequence(connection, stream_id)
                raise ConcurrencyError(stream_id, previous, fresh)

        return tuple(appended)

    @staticmethod
    def _validate_append_request(
        events: Sequence[EventEnvelope],
        expected_sequences: Mapping[str, int],
    ) -> set[str]:
        stream_ids = {event.stream_id for event in events}
        missing = stream_ids.difference(expected_sequences)
        if missing:
            names = ", ".join(repr(name) for name in sorted(missing))
            raise ValueError(f"expected_sequences is missing streams: {names}")
        for stream_id in stream_ids:
            if expected_sequences[stream_id] < 0:
                raise ValueError(f"expected sequence for stream {stream_id!r} must be non-negative")
        return stream_ids

    def _validate_transaction_connection(self, connection: sqlite3.Connection) -> None:
        if not connection.in_transaction:
            raise RuntimeError("kernel event append requires an active transaction")
        foreign_keys = connection.execute("pragma foreign_keys").fetchone()
        if foreign_keys is None or int(foreign_keys[0]) != 1:
            raise RuntimeError("kernel event append requires SQLite foreign keys")
        database_rows = connection.execute("pragma database_list").fetchall()
        main_path: str | None = None
        for row in database_rows:
            name = str(row[1])
            if name == "main":
                main_path = str(row[2])
                break
        if main_path is None or Path(main_path).resolve() != self.path.resolve():
            raise ValueError("transaction connection does not target this event store")

    @staticmethod
    def _validate_existing_batch_order(events: Sequence[EventEnvelope | None]) -> None:
        """Ensure an idempotent retry preserves per-stream append order."""

        last_sequence: dict[str, int] = {}
        for event in events:
            if event is None:  # pragma: no cover - guarded by the all() fast path
                raise ValueError("idempotent batch requires stored events")
            previous = last_sequence.get(event.stream_id)
            if previous is not None and event.stream_sequence != previous + 1:
                raise EventSequenceError(
                    f"idempotent batch is not ordered for stream {event.stream_id!r}: "
                    f"{event.stream_sequence} follows {previous}"
                )
            last_sequence[event.stream_id] = event.stream_sequence

    @staticmethod
    def _validate_batch_stream(
        stream_id: str,
        events: Sequence[tuple[EventEnvelope, EventEnvelope | None]],
        *,
        expected_sequence: int,
        actual_sequence: int,
    ) -> int:
        committed_prefix = 0
        encountered_new = False
        for _, stored in events:
            if stored is not None:
                if encountered_new:
                    raise ConcurrencyError(stream_id, expected_sequence, actual_sequence)
                committed_prefix += 1
            else:
                encountered_new = True

        required_actual = expected_sequence + committed_prefix
        if actual_sequence != required_actual:
            raise ConcurrencyError(stream_id, required_actual, actual_sequence)

        for offset, (candidate, stored) in enumerate(events, start=1):
            required_sequence = expected_sequence + offset
            if stored is not None:
                if stored.stream_sequence != required_sequence:
                    raise ConcurrencyError(stream_id, expected_sequence, actual_sequence)
            elif candidate.stream_sequence != required_sequence:
                raise EventSequenceError(
                    f"event {candidate.event_id} declares sequence {candidate.stream_sequence}; "
                    f"batch sequence for stream {stream_id!r} is {required_sequence}"
                )
        return committed_prefix

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        event: EventEnvelope,
    ) -> EventEnvelope:
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
        global_position = cursor.lastrowid
        if global_position is None:  # pragma: no cover - SQLite INSERT invariant
            raise EventConflictError("SQLite did not assign an event position")
        return replace(event, global_position=global_position)

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
