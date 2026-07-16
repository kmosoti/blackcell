from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
type JsonObject = dict[str, JsonValue]


class SpanStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SpanEvent:
    name: str
    timestamp: str
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SpanRecord:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    started_at: str
    ended_at: str
    duration_ms: float
    status: SpanStatus
    correlation_ids: Mapping[str, str] = field(default_factory=dict)
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)
    events: tuple[SpanEvent, ...] = ()


@runtime_checkable
class SpanExporter(Protocol):
    """Minimal exporter boundary; adapters may map records to OTel later."""

    def export(self, record: SpanRecord) -> None: ...
