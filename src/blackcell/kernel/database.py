from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from blackcell.kernel.errors import SchemaVersionError

SCHEMA_VERSION = 1

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
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as connection:
        current = int(connection.execute("pragma user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"kernel database schema {current} is newer than supported schema {SCHEMA_VERSION}"
            )
        if current < 1:
            connection.executescript(
                "begin immediate;\n"
                f"{_SCHEMA_V1}\n"
                "insert into kernel_schema_migrations(version, applied_at) "
                "values (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));\n"
                "pragma user_version = 1;\n"
                "commit;"
            )


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("pragma foreign_keys = on")
    connection.execute("pragma busy_timeout = 30000")
    connection.execute("pragma journal_mode = wal")
    connection.execute("pragma synchronous = normal")
    try:
        yield connection
    finally:
        connection.close()
