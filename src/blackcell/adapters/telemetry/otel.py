from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from threading import Lock

from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import (
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
    TraceState,
)
from opentelemetry.util.types import AttributeValue

from blackcell.telemetry import SpanRecord, SpanStatus

_INSTRUMENTATION_SCOPE = InstrumentationScope("blackcell.telemetry", "0.2.0")
_KEY_CHARACTER = re.compile(r"[^A-Za-z0-9_.-]")
_MAX_ATTRIBUTE_KEY_CHARS = 256
_MAX_NESTED_JSON_CHARS = 2_048


class OpenTelemetryMappingError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OpenTelemetrySpanExporter:
    """Map sanitized BlackCell records into a bounded OTel span processor."""

    def __init__(self, processor: SpanProcessor, *, resource: Resource) -> None:
        self._processor = processor
        self._resource = resource
        self._lock = Lock()
        self._closed = False

    def export(self, record: SpanRecord) -> None:
        with self._lock:
            if self._closed:
                raise OpenTelemetryMappingError("otel-exporter-closed")
        self._processor.on_end(_readable_span(record, resource=self._resource))

    def force_flush(self, timeout_millis: int) -> bool:
        with self._lock:
            if self._closed:
                return True
        return self._processor.force_flush(timeout_millis)

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._processor.shutdown()

    @classmethod
    def otlp_http(
        cls,
        *,
        endpoint: str,
        timeout_seconds: int,
        max_queue_size: int,
        max_export_batch_size: int,
        schedule_delay_millis: int,
        resource: Resource,
    ) -> OpenTelemetrySpanExporter:
        exporter = OTLPSpanExporter(
            endpoint=endpoint,
            headers={"x-blackcell-runtime": "0.2.0"},
            timeout=float(timeout_seconds),
            compression=Compression.NoCompression,
        )
        processor = BatchSpanProcessor(
            exporter,
            max_queue_size=max_queue_size,
            max_export_batch_size=max_export_batch_size,
            schedule_delay_millis=schedule_delay_millis,
            export_timeout_millis=timeout_seconds * 1_000,
        )
        return cls(processor, resource=resource)


def _readable_span(record: SpanRecord, *, resource: Resource) -> ReadableSpan:
    started_at = _timestamp_ns(record.started_at)
    ended_at = _timestamp_ns(record.ended_at)
    if ended_at < started_at:
        raise OpenTelemetryMappingError("invalid-span-time-range")
    trace_id = _identifier(record.trace_id, bytes_count=16, domain="trace")
    context = SpanContext(
        trace_id,
        _identifier(record.span_id, bytes_count=8, domain="span"),
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    parent = (
        None
        if record.parent_span_id is None
        else SpanContext(
            trace_id,
            _identifier(record.parent_span_id, bytes_count=8, domain="span"),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
            trace_state=TraceState(),
        )
    )
    attributes = {
        "blackcell.trace.id": record.trace_id,
        "blackcell.span.id": record.span_id,
        "blackcell.span.status": record.status.value,
        **{
            f"blackcell.correlation.{_key(key)}": value
            for key, value in record.correlation_ids.items()
        },
        **{
            f"blackcell.attribute.{_key(key)}": mapped
            for key, value in record.attributes.items()
            if (mapped := _attribute_value(value)) is not None
        },
    }
    events = tuple(
        Event(
            event.name,
            attributes={
                _key(key): mapped
                for key, value in event.attributes.items()
                if (mapped := _attribute_value(value)) is not None
            },
            timestamp=_timestamp_ns(event.timestamp),
        )
        for event in record.events
    )
    return ReadableSpan(
        record.name,
        context=context,
        parent=parent,
        resource=resource,
        attributes=attributes,
        events=events,
        kind=SpanKind.INTERNAL,
        status=Status(StatusCode.ERROR if record.status is SpanStatus.ERROR else StatusCode.OK),
        start_time=started_at,
        end_time=ended_at,
        instrumentation_scope=_INSTRUMENTATION_SCOPE,
    )


def _identifier(value: str, *, bytes_count: int, domain: str) -> int:
    digest = hashlib.sha256(f"blackcell:{domain}:{value}".encode()).digest()
    return int.from_bytes(digest[:bytes_count], "big") or 1


def _timestamp_ns(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise OpenTelemetryMappingError("invalid-span-timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OpenTelemetryMappingError("invalid-span-timestamp")
    resolved = parsed.astimezone(UTC)
    seconds = int(resolved.timestamp())
    return seconds * 1_000_000_000 + resolved.microsecond * 1_000


def _key(value: str) -> str:
    resolved = _KEY_CHARACTER.sub("_", value)[:_MAX_ATTRIBUTE_KEY_CHARS]
    return resolved or "unknown"


def _attribute_value(value: object) -> AttributeValue | None:
    if isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None:
        return None
    if isinstance(value, Mapping | Sequence) and not isinstance(value, bytes | bytearray | str):
        try:
            encoded = json.dumps(
                _plain_json(value),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as error:
            raise OpenTelemetryMappingError("invalid-span-attribute") from error
        return encoded[:_MAX_NESTED_JSON_CHARS]
    raise OpenTelemetryMappingError("invalid-span-attribute")


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
        return [_plain_json(item) for item in value]
    return value


__all__ = ["OpenTelemetryMappingError", "OpenTelemetrySpanExporter"]
