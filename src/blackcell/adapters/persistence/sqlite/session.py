from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from blackcell.kernel import EventEnvelope, EventStore
from blackcell.kernel.database import connect

_ALLOWED_SQL_OPERATIONS = frozenset({"delete", "insert", "select", "update"})


class SQLiteTransactionError(RuntimeError):
    """The shared kernel transaction is absent, closed, or misused."""


class SQLiteKernelTransaction:
    """One active local transaction shared by adapter rows and kernel events."""

    def __init__(self, connection: sqlite3.Connection, events: EventStore) -> None:
        self._connection = connection
        self._events = events
        self._active = True

    def execute(
        self,
        statement: str,
        parameters: tuple[object, ...] = (),
    ) -> sqlite3.Cursor:
        """Execute one bounded data statement in the shared transaction."""

        self._require_active()
        tokens = statement.lstrip().split(maxsplit=1)
        if not tokens or tokens[0].casefold() not in _ALLOWED_SQL_OPERATIONS:
            raise SQLiteTransactionError(
                "shared transactions permit only select, insert, update, and delete statements"
            )
        return self._connection.execute(statement, parameters)

    def append_events(
        self,
        events: Sequence[EventEnvelope],
        *,
        expected_sequences: Mapping[str, int],
    ) -> tuple[EventEnvelope, ...]:
        """Append kernel events without ending the surrounding transaction."""

        self._require_active()
        return self._events.append_many_in_transaction(
            self._connection,
            events,
            expected_sequences=expected_sequences,
        )

    def append_event(
        self,
        event: EventEnvelope,
        *,
        expected_sequence: int,
    ) -> EventEnvelope:
        return self.append_events(
            (event,),
            expected_sequences={event.stream_id: expected_sequence},
        )[0]

    def current_sequence(self, stream_id: str) -> int:
        self._require_active()
        row = self._connection.execute(
            "select current_sequence from event_streams where stream_id = ?",
            (stream_id,),
        ).fetchone()
        return 0 if row is None else int(row["current_sequence"])

    def _require_active(self) -> None:
        if not self._active or not self._connection.in_transaction:
            raise SQLiteTransactionError("shared kernel transaction is not active")

    def _close(self) -> None:
        self._active = False


class SQLiteKernelSession:
    """Create explicit atomic units over one initialized kernel database."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._events = EventStore(self.path)

    @contextmanager
    def transaction(self) -> Iterator[SQLiteKernelTransaction]:
        with connect(self.path) as connection:
            connection.execute("begin immediate")
            transaction = SQLiteKernelTransaction(connection, self._events)
            try:
                yield transaction
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                transaction._close()


__all__ = [
    "SQLiteKernelSession",
    "SQLiteKernelTransaction",
    "SQLiteTransactionError",
]
