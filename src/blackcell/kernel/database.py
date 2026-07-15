from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic, sleep

from blackcell.kernel.errors import SchemaVersionError

SCHEMA_VERSION = 1
_BUSY_TIMEOUT_MILLISECONDS = 30_000
_WAL_RETRY_INTERVAL_SECONDS = 0.01

_SCHEMA_V1 = """
create table if not exists kernel_schema_migrations (
    version integer primary key,
    applied_at text not null
);

create table if not exists event_streams (
    stream_id text primary key check(length(stream_id) > 0),
    current_sequence integer not null default 0 check(current_sequence >= 0)
);

create table if not exists kernel_events (
    global_position integer primary key autoincrement,
    event_id text not null unique check(length(event_id) > 0),
    stream_id text not null,
    stream_sequence integer not null check(stream_sequence >= 1),
    event_type text not null check(length(event_type) > 0),
    schema_version integer not null check(schema_version >= 1),
    recorded_at text not null,
    effective_at text not null,
    correlation_id text not null check(length(correlation_id) > 0),
    causation_id text,
    actor text not null check(length(actor) > 0),
    source text not null check(length(source) > 0),
    payload_json text not null,
    payload_hash text not null,
    idempotency_key text,
    idempotency_hash text,
    unique(stream_id, stream_sequence),
    unique(stream_id, idempotency_key),
    foreign key(stream_id) references event_streams(stream_id),
    foreign key(causation_id) references kernel_events(event_id),
    check((idempotency_key is null) = (idempotency_hash is null))
);

create index if not exists idx_kernel_events_type
    on kernel_events(event_type, global_position);
create index if not exists idx_kernel_events_correlation
    on kernel_events(correlation_id, global_position);

create table if not exists kernel_artifacts (
    digest text primary key check(length(digest) > 0),
    algorithm text not null,
    size_bytes integer not null check(size_bytes >= 0),
    media_type text not null check(length(media_type) > 0),
    encoding text,
    relative_path text not null unique check(length(relative_path) > 0),
    created_at text not null
);

create table if not exists projection_checkpoints (
    projection_name text not null check(length(projection_name) > 0),
    projection_version integer not null check(projection_version >= 1),
    scope text not null,
    last_global_position integer not null check(last_global_position >= 0),
    last_stream_sequence integer,
    state_json text not null,
    state_hash text not null,
    updated_at text not null,
    primary key(projection_name, projection_version, scope),
    check(last_stream_sequence is null or last_stream_sequence >= 0)
);
"""


def initialize_database(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with connect(path) as connection:
        connection.execute("begin immediate")
        try:
            # Read the version only after taking the write lock. Concurrent
            # first-open callers may all observe a new database before this
            # point; the lock holder initializes it and later callers then see
            # the committed version instead of replaying migration 1.
            current = int(connection.execute("pragma user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"kernel database schema {current} is newer than supported schema "
                    f"{SCHEMA_VERSION}"
                )
            if current < 1:
                _execute_schema(connection, _SCHEMA_V1)
                connection.execute(
                    "insert into kernel_schema_migrations(version, applied_at) "
                    "values (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                )
                connection.execute("pragma user_version = 1")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    _owner_only_file(path)


def _execute_schema(connection: sqlite3.Connection, script: str) -> None:
    """Execute a schema script without leaving the caller's transaction."""

    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            connection.execute(pending)
            pending = ""
    if pending.strip():  # pragma: no cover - static schema authoring invariant
        raise RuntimeError("kernel schema contains an incomplete SQL statement")


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(
        path,
        timeout=_BUSY_TIMEOUT_MILLISECONDS / 1_000,
        isolation_level=None,
    )
    try:
        _owner_only_file(path)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma foreign_keys = on")
        connection.execute(f"pragma busy_timeout = {_BUSY_TIMEOUT_MILLISECONDS}")
        _enable_wal(connection)
        connection.execute("pragma synchronous = normal")
        for suffix in ("-wal", "-shm"):
            auxiliary = Path(f"{path}{suffix}")
            if auxiliary.exists():
                _owner_only_file(auxiliary)
        yield connection
    finally:
        connection.close()
        for suffix in ("-wal", "-shm"):
            auxiliary = Path(f"{path}{suffix}")
            if auxiliary.exists():
                _owner_only_file(auxiliary)


def _enable_wal(connection: sqlite3.Connection) -> None:
    """Negotiate persistent WAL mode across concurrent first-open callers."""

    deadline = monotonic() + (_BUSY_TIMEOUT_MILLISECONDS / 1_000)
    while True:
        try:
            row = connection.execute("pragma journal_mode = wal").fetchone()
            if row is None or str(row[0]).casefold() != "wal":
                raise RuntimeError("kernel database did not enter WAL journal mode")
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).casefold() or monotonic() >= deadline:
                raise
            sleep(_WAL_RETRY_INTERVAL_SECONDS)


def _owner_only_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except FileNotFoundError:
        return
