"""Append-only SQLite chronicle with database-enforced immutability."""

import fcntl
import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from platformdirs import user_state_path
from pydantic import BaseModel, ConfigDict

from blackcell.contracts.errors import NotFoundFailure, PolicyFailure
from blackcell.contracts.plan import validate_plan_id


class EventType(StrEnum):
    DIRECTIVE_VALIDATED = "directive_validated"
    OPERATION_PROPOSED = "operation_proposed"
    OPERATION_LOCATED = "operation_located"
    OPERATION_WORKFLOW_RECONCILED = "operation_workflow_reconciled"
    OPERATION_PRESENTATION_RECONCILED = "operation_presentation_reconciled"
    OPERATION_VERIFIED = "operation_verified"
    ASSIGNMENT_CREATED = "assignment_created"
    ASSIGNMENT_LOCATED = "assignment_located"
    ASSIGNMENT_REPAIRED = "assignment_repaired"
    PARENT_RELATION_CREATED = "parent_relation_created"
    PARENT_RELATION_VERIFIED = "parent_relation_verified"
    BLOCKING_RELATION_CREATED = "blocking_relation_created"
    BLOCKING_RELATION_VERIFIED = "blocking_relation_verified"
    BLOCKING_RELATION_PENDING = "blocking_relation_pending"
    ECHO_VERIFIED = "echo_verified"
    ECHO_PENDING = "echo_pending"
    WORKFLOW_STEP_COMPLETED = "workflow_step_completed"
    WORKFLOW_STEP_PENDING = "workflow_step_pending"
    WORKFLOW_STEP_FAILED = "workflow_step_failed"
    ANOMALY_DETECTED = "anomaly_detected"
    ANOMALY_RESOLVED = "anomaly_resolved"
    MATERIALIZATION_COMPLETED = "materialization_completed"


class ChronicleEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    occurred_at: str
    event_type: str
    plan_id: str
    item_key: str | None
    payload: dict[str, Any]


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    item_key TEXT,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
);
CREATE INDEX IF NOT EXISTS events_plan_id_id ON events(plan_id, id);
CREATE TRIGGER IF NOT EXISTS events_reject_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'chronicle events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS events_reject_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'chronicle events are append-only');
END;
"""

FORBIDDEN_KEYS = frozenset(
    {"authorization", "linear_api_key", "github_token", "gh_token", "access_token"}
)


def _assert_secret_free(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = str(key).lower()
            if normalized_key in FORBIDDEN_KEYS:
                raise PolicyFailure(
                    "Chronicle payload contains a forbidden credential field.",
                    details={"field": ".".join((*path, str(key)))},
                )
            _assert_secret_free(item, (*path, str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_secret_free(item, (*path, str(index)))
    elif isinstance(value, str):
        for variable in ("LINEAR_API_KEY", "GITHUB_TOKEN", "GH_TOKEN"):
            secret = os.environ.get(variable)
            if secret and secret in value:
                raise PolicyFailure("Chronicle payload contains credential material.")


class Chronicle:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or user_state_path("blackcell") / "blackcell.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_root = self.path.parent / "locks"
        self.lock_root.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def append(
        self,
        event_type: str | EventType,
        plan_id: str,
        payload: Mapping[str, Any] | None = None,
        item_key: str | None = None,
    ) -> int:
        safe_payload = dict(payload or {})
        _assert_secret_free(safe_payload)
        encoded = json.dumps(safe_payload, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO events(occurred_at, event_type, plan_id, item_key, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(UTC).isoformat(),
                    str(event_type),
                    plan_id,
                    item_key,
                    encoded,
                ),
            )
            connection.commit()
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return a chronicle event ID.")
            return cursor.lastrowid

    def events(self, plan_id: str | None = None) -> list[ChronicleEvent]:
        query = "SELECT * FROM events"
        parameters: tuple[str, ...] = ()
        if plan_id is not None:
            query += " WHERE plan_id = ?"
            parameters = (plan_id,)
        query += " ORDER BY id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            ChronicleEvent(
                id=row["id"],
                occurred_at=row["occurred_at"],
                event_type=row["event_type"],
                plan_id=row["plan_id"],
                item_key=row["item_key"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def unresolved_anomalies(self, plan_id: str | None = None) -> list[ChronicleEvent]:
        events = self.events(plan_id)
        resolved_ids = {
            event.payload.get("anomaly_id")
            for event in events
            if event.event_type == EventType.ANOMALY_RESOLVED
        }
        return [
            event
            for event in events
            if event.event_type == EventType.ANOMALY_DETECTED and event.id not in resolved_ids
        ]

    def resolve_anomaly(self, anomaly_id: int, note: str) -> int:
        anomaly = next(
            (
                event
                for event in self.events()
                if event.id == anomaly_id and event.event_type == EventType.ANOMALY_DETECTED
            ),
            None,
        )
        if anomaly is None:
            raise NotFoundFailure(f"Anomaly event {anomaly_id} was not found.")
        if anomaly not in self.unresolved_anomalies(anomaly.plan_id):
            raise PolicyFailure(f"Anomaly event {anomaly_id} is already resolved.")
        cleaned_note = note.strip()
        if not cleaned_note:
            raise PolicyFailure("Anomaly resolution note must not be blank.")
        return self.append(
            EventType.ANOMALY_RESOLVED,
            anomaly.plan_id,
            {"anomaly_id": anomaly_id, "note": cleaned_note},
            anomaly.item_key,
        )

    @contextmanager
    def plan_lock(self, plan_id: str) -> Iterator[None]:
        validate_plan_id(plan_id)
        lock_path = self.lock_root / f"{plan_id}.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
