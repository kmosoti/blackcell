import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from blackcell.latent.ids import stable_digest
from blackcell.ledger.models import (
    LedgerEvent,
    LedgerRecordResult,
    LedgerRun,
    LedgerSummary,
    Payload,
)

SCHEMA_VERSION = 1
DEFAULT_LEDGER_PATH = Path(".blackcell") / "ledger.sqlite3"


def init_ledger(path: Path = DEFAULT_LEDGER_PATH) -> LedgerSummary:
    with _connect(path) as connection:
        _ensure_schema(connection)
        return _summary(connection, path)


def summarize_ledger(path: Path = DEFAULT_LEDGER_PATH) -> LedgerSummary:
    if not path.exists():
        return LedgerSummary(path=path, schema_version=SCHEMA_VERSION, run_count=0, event_count=0)
    try:
        with _connect_readonly(path) as connection:
            return _summary(connection, path)
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return LedgerSummary(
                path=path,
                schema_version=SCHEMA_VERSION,
                run_count=0,
                event_count=0,
            )
        raise


def record_run(
    *,
    kind: str,
    status: str,
    created_at: str,
    events: Sequence[LedgerEvent] = (),
    payload: Payload | None = None,
    path: Path = DEFAULT_LEDGER_PATH,
    run_id: str | None = None,
) -> LedgerRecordResult:
    run_payload = payload or {}
    resolved_run_id = run_id or stable_digest(
        "ledger-run",
        {
            "kind": kind,
            "status": status,
            "created_at": created_at,
            "payload": run_payload,
        },
    )
    run = LedgerRun(
        run_id=resolved_run_id,
        kind=kind,
        status=status,
        created_at=created_at,
        payload=run_payload,
    )
    with _connect(path) as connection:
        _ensure_schema(connection)
        _insert_run(connection, run)
        for event in events:
            if event.run_id != run.run_id:
                raise ValueError("ledger event run_id must match the recorded run")
            _insert_event(connection, event)
    return LedgerRecordResult(path=path, run_id=run.run_id, event_count=len(events))


def list_runs(path: Path = DEFAULT_LEDGER_PATH) -> tuple[LedgerRun, ...]:
    if not path.exists():
        return ()
    try:
        with _connect_readonly(path) as connection:
            rows = connection.execute(
                "select run_id, kind, status, created_at, payload_json "
                "from runs order by created_at, run_id"
            ).fetchall()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return ()
        raise
    return tuple(_run_from_row(row) for row in rows)


def list_events(
    path: Path = DEFAULT_LEDGER_PATH, *, run_id: str | None = None
) -> tuple[LedgerEvent, ...]:
    if not path.exists():
        return ()
    query = "select event_id, run_id, sequence, kind, source, message, payload_json from events"
    params: tuple[str, ...] = ()
    if run_id is not None:
        query += " where run_id = ?"
        params = (run_id,)
    query += " order by run_id, sequence, event_id"
    try:
        with _connect_readonly(path) as connection:
            rows = connection.execute(query, params).fetchall()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return ()
        raise
    return tuple(_event_from_row(row) for row in rows)


def make_event(
    *,
    run_id: str,
    sequence: int,
    kind: str,
    source: str,
    message: str,
    payload: Payload | None = None,
) -> LedgerEvent:
    event_payload = payload or {}
    event_id = stable_digest(
        "ledger-event",
        {
            "run_id": run_id,
            "sequence": sequence,
            "kind": kind,
            "source": source,
            "message": message,
            "payload": event_payload,
        },
    )
    return LedgerEvent(
        event_id=event_id,
        run_id=run_id,
        sequence=sequence,
        kind=kind,
        source=source,
        message=message,
        payload=event_payload,
    )


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("pragma foreign_keys = on")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


@contextmanager
def _connect_readonly(path: Path) -> Iterator[sqlite3.Connection]:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("pragma foreign_keys = on")
    try:
        yield connection
    finally:
        connection.close()


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        create table if not exists ledger_meta (
            key text primary key,
            value text not null
        );
        create table if not exists runs (
            run_id text primary key,
            kind text not null,
            status text not null,
            created_at text not null,
            payload_json text not null
        );
        create table if not exists events (
            event_id text primary key,
            run_id text not null,
            sequence integer not null,
            kind text not null,
            source text not null,
            message text not null,
            payload_json text not null,
            unique(run_id, sequence),
            foreign key(run_id) references runs(run_id)
        );
        """
    )


def _insert_run(connection: sqlite3.Connection, run: LedgerRun) -> None:
    payload_json = _json(run.payload)
    existing = connection.execute(
        "select kind, status, created_at, payload_json from runs where run_id = ?",
        (run.run_id,),
    ).fetchone()
    expected = (run.kind, run.status, run.created_at, payload_json)
    if existing is not None:
        if tuple(existing) != expected:
            raise ValueError(f"ledger run conflict for {run.run_id}")
        return
    connection.execute(
        """
        insert into runs(run_id, kind, status, created_at, payload_json)
        values (?, ?, ?, ?, ?)
        """,
        (run.run_id, run.kind, run.status, run.created_at, payload_json),
    )


def _insert_event(connection: sqlite3.Connection, event: LedgerEvent) -> None:
    payload_json = _json(event.payload)
    existing = connection.execute(
        """
        select event_id, kind, source, message, payload_json
        from events where run_id = ? and sequence = ?
        """,
        (event.run_id, event.sequence),
    ).fetchone()
    expected = (event.event_id, event.kind, event.source, event.message, payload_json)
    if existing is not None:
        if tuple(existing) != expected:
            raise ValueError(f"ledger event conflict for {event.run_id}#{event.sequence}")
        return
    connection.execute(
        """
        insert into events(event_id, run_id, sequence, kind, source, message, payload_json)
        values (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.event_id,
            event.run_id,
            event.sequence,
            event.kind,
            event.source,
            event.message,
            payload_json,
        ),
    )
    connection.execute(
        "insert or replace into ledger_meta(key, value) values (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )


def _summary(connection: sqlite3.Connection, path: Path) -> LedgerSummary:
    return LedgerSummary(
        path=path,
        schema_version=SCHEMA_VERSION,
        run_count=_count(connection, "runs"),
        event_count=_count(connection, "events"),
    )


def _count(connection: sqlite3.Connection, table: str) -> int:
    row = connection.execute(f"select count(*) from {table}").fetchone()
    value = row[0]
    if not isinstance(value, int):
        raise TypeError(f"unexpected count value for {table}: {value!r}")
    return value


def _run_from_row(row: tuple[object, ...]) -> LedgerRun:
    return LedgerRun(
        run_id=_str_cell(row[0], "run_id"),
        kind=_str_cell(row[1], "kind"),
        status=_str_cell(row[2], "status"),
        created_at=_str_cell(row[3], "created_at"),
        payload=_loads_payload(_str_cell(row[4], "payload_json")),
    )


def _event_from_row(row: tuple[object, ...]) -> LedgerEvent:
    sequence = row[2]
    if not isinstance(sequence, int):
        raise TypeError(f"unexpected sequence value: {sequence!r}")
    return LedgerEvent(
        event_id=_str_cell(row[0], "event_id"),
        run_id=_str_cell(row[1], "run_id"),
        sequence=sequence,
        kind=_str_cell(row[3], "kind"),
        source=_str_cell(row[4], "source"),
        message=_str_cell(row[5], "message"),
        payload=_loads_payload(_str_cell(row[6], "payload_json")),
    )


def _loads_payload(value: str) -> Payload:
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise TypeError("ledger payload must be an object")
    result: Payload = {}
    for key, item in payload.items():
        if isinstance(item, int | float | str | bool) or item is None:
            result[str(key)] = item
        else:
            result[str(key)] = json.dumps(item, sort_keys=True)
    return result


def _str_cell(value: object, key: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"ledger column {key!r} must be a string")
    return value


def _json(value: object) -> str:
    return json.dumps(_jsonable(value), sort_keys=True)


def _jsonable(value: object) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value
