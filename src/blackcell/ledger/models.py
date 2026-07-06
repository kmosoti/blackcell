from dataclasses import dataclass
from pathlib import Path

Payload = dict[str, int | float | str | bool | None]


@dataclass(frozen=True, slots=True)
class LedgerRun:
    run_id: str
    kind: str
    status: str
    created_at: str
    payload: Payload


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    event_id: str
    run_id: str
    sequence: int
    kind: str
    source: str
    message: str
    payload: Payload


@dataclass(frozen=True, slots=True)
class LedgerSummary:
    path: Path
    schema_version: int
    run_count: int
    event_count: int


@dataclass(frozen=True, slots=True)
class LedgerRecordResult:
    path: Path
    run_id: str
    event_count: int
