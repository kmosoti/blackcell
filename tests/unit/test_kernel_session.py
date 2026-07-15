from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from blackcell.adapters.persistence.sqlite import (
    SQLiteKernelSession,
    SQLiteTransactionError,
)
from blackcell.kernel import ConcurrencyError, EventEnvelope, EventStore
from blackcell.kernel.database import connect

NOW = datetime(2026, 7, 13, 12, tzinfo=UTC)


def test_session_commits_adapter_state_and_kernel_event_atomically(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    session = SQLiteKernelSession(path)
    _create_adapter_table(path)
    candidate = _event(1)

    with session.transaction() as transaction:
        transaction.execute(
            "insert into test_adapter_state(state_id, value) values (?, ?)",
            ("state:1", "ready"),
        )
        stored = transaction.append_event(candidate, expected_sequence=0)
        assert transaction.current_sequence(candidate.stream_id) == 1

    with connect(path) as connection:
        row = connection.execute(
            "select value from test_adapter_state where state_id = ?",
            ("state:1",),
        ).fetchone()
    assert row is not None and row["value"] == "ready"
    assert EventStore(path).read_stream(candidate.stream_id) == (stored,)


def test_session_rolls_back_adapter_state_and_event_on_caller_failure(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    session = SQLiteKernelSession(path)
    _create_adapter_table(path)

    with pytest.raises(LookupError, match="abort"), session.transaction() as transaction:
        transaction.execute(
            "insert into test_adapter_state(state_id, value) values (?, ?)",
            ("state:rollback", "pending"),
        )
        transaction.append_event(_event(1), expected_sequence=0)
        raise LookupError("abort")

    assert _adapter_rows(path) == ()
    assert EventStore(path).read_stream("orchestration:run:1") == ()


def test_event_conflict_rolls_back_adapter_state_in_same_transaction(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    session = SQLiteKernelSession(path)
    _create_adapter_table(path)
    EventStore(path).append(_event(1), expected_sequence=0)

    with pytest.raises(ConcurrencyError), session.transaction() as transaction:
        transaction.execute(
            "insert into test_adapter_state(state_id, value) values (?, ?)",
            ("state:conflict", "must-rollback"),
        )
        transaction.append_event(_event(2), expected_sequence=0)

    assert _adapter_rows(path) == ()
    assert EventStore(path).current_sequence("orchestration:run:1") == 1


def test_transaction_rejects_control_sql_and_use_after_scope(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    session = SQLiteKernelSession(path)
    _create_adapter_table(path)

    with session.transaction() as transaction:
        with pytest.raises(SQLiteTransactionError, match="permit only"):
            transaction.execute("commit")
        transaction.execute(
            "insert into test_adapter_state(state_id, value) values (?, ?)",
            ("state:bounded", "committed"),
        )

    with pytest.raises(SQLiteTransactionError, match="not active"):
        transaction.execute("select 1")
    assert _adapter_rows(path) == (("state:bounded", "committed"),)


def test_in_transaction_append_rejects_absent_or_foreign_transaction(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    foreign_path = tmp_path / "foreign.sqlite3"
    store = EventStore(path)
    EventStore(foreign_path)

    with connect(path) as connection, pytest.raises(RuntimeError, match="active transaction"):
        store.append_many_in_transaction(
            connection,
            (_event(1),),
            expected_sequences={"orchestration:run:1": 0},
        )

    with connect(foreign_path) as connection:
        connection.execute("begin immediate")
        with pytest.raises(ValueError, match="does not target"):
            store.append_many_in_transaction(
                connection,
                (_event(1),),
                expected_sequences={"orchestration:run:1": 0},
            )
        connection.rollback()


def _event(sequence: int) -> EventEnvelope:
    return EventEnvelope.create(
        stream_id="orchestration:run:1",
        stream_sequence=sequence,
        event_type="OrchestrationStateChanged",
        actor="scheduler:test",
        source="blackcell.orchestration",
        payload={"sequence": sequence},
        recorded_at=NOW,
        effective_at=NOW,
        correlation_id="run:1",
        idempotency_key=f"orchestration:run:1:{sequence}",
    )


def _create_adapter_table(path: Path) -> None:
    with connect(path) as connection:
        connection.execute(
            """
            create table test_adapter_state (
                state_id text primary key,
                value text not null
            )
            """
        )


def _adapter_rows(path: Path) -> tuple[tuple[str, str], ...]:
    with connect(path) as connection:
        rows = connection.execute(
            "select state_id, value from test_adapter_state order by state_id"
        ).fetchall()
    return tuple((str(row["state_id"]), str(row["value"])) for row in rows)
