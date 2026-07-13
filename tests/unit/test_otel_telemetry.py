from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from blackcell.adapters.telemetry import (
    OpenTelemetryMappingError,
    OpenTelemetrySpanExporter,
)
from blackcell.telemetry import (
    ContentPolicy,
    SpanNames,
    SpanRecord,
    SpanStatus,
    TraceRecorder,
)

TOKEN = "Runtime-v1_otel-token.0123456789-ABCDEFG"


class FailingProcessor(SpanProcessor):
    def on_end(self, span: ReadableSpan) -> None:
        del span
        raise RuntimeError("provider detail must remain isolated")


def test_otel_adapter_preserves_stable_trace_parentage_and_redacted_metadata() -> None:
    memory = InMemorySpanExporter()
    adapter = OpenTelemetrySpanExporter(
        SimpleSpanProcessor(memory),
        resource=Resource({"service.name": "blackcell-test"}),
    )
    recorder = TraceRecorder(
        content_policy=ContentPolicy(sensitive_values=(TOKEN,)),
        exporters=(adapter,),
    )

    with (
        recorder.span(
            SpanNames.BUILD_CONTEXT,
            trace_id="run:otel-1",
            correlation_ids={"run_id": "run:otel-1"},
            attributes={"prompt": TOKEN, "counts": {"selected": 3}},
        ),
        recorder.span(SpanNames.MODEL_DECIDE) as child,
    ):
        child.add_event("model.completed", {"authorization": TOKEN, "tokens": 7})

    exported = {span.name: span for span in memory.get_finished_spans()}
    parent = exported[SpanNames.BUILD_CONTEXT]
    child = exported[SpanNames.MODEL_DECIDE]
    assert parent.context is not None
    assert child.context is not None
    assert child.parent is not None
    assert parent.attributes is not None
    assert child.events[0].attributes is not None
    assert child.context.trace_id == parent.context.trace_id
    assert child.parent.span_id == parent.context.span_id
    assert parent.attributes["blackcell.trace.id"] == "run:otel-1"
    assert parent.attributes["blackcell.correlation.run_id"] == "run:otel-1"
    assert parent.attributes["blackcell.attribute.prompt"] == "[REDACTED]"
    assert parent.attributes["blackcell.attribute.counts"] == '{"selected":3}'
    assert child.events[0].attributes["authorization"] == "[REDACTED]"
    assert child.events[0].attributes["tokens"] == 7
    assert TOKEN not in repr(exported)
    adapter.shutdown()


def test_otel_adapter_maps_error_status_without_exception_content() -> None:
    memory = InMemorySpanExporter()
    adapter = OpenTelemetrySpanExporter(
        SimpleSpanProcessor(memory),
        resource=Resource({"service.name": "blackcell-test"}),
    )
    recorder = TraceRecorder(exporters=(adapter,))

    with (
        pytest.raises(RuntimeError, match="sensitive provider detail"),
        recorder.span(SpanNames.MODEL_DECIDE),
    ):
        raise RuntimeError("sensitive provider detail")

    span = memory.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description is None
    assert span.attributes["blackcell.attribute.error.message"] == "[REDACTED]"
    assert "sensitive provider detail" not in repr(span)
    adapter.shutdown()


def test_otel_mapping_is_deterministic_and_rejects_invalid_timestamps_content_free() -> None:
    memory = InMemorySpanExporter()
    adapter = OpenTelemetrySpanExporter(
        SimpleSpanProcessor(memory),
        resource=Resource({"service.name": "blackcell-test"}),
    )
    record = _record()

    adapter.export(record)
    adapter.export(record)

    first, second = memory.get_finished_spans()
    assert first.context is not None and second.context is not None
    assert first.context.trace_id == second.context.trace_id
    assert first.context.span_id == second.context.span_id
    with pytest.raises(OpenTelemetryMappingError) as caught:
        adapter.export(replace(record, started_at="customer-secret-invalid-time"))
    assert str(caught.value) == "invalid-span-timestamp"
    assert "customer-secret" not in str(caught.value)
    adapter.shutdown()


def test_otel_processor_failure_is_recorded_without_failing_the_controlled_span() -> None:
    adapter = OpenTelemetrySpanExporter(
        FailingProcessor(),
        resource=Resource({"service.name": "blackcell-test"}),
    )
    recorder = TraceRecorder(exporters=(adapter,))

    with recorder.span(SpanNames.OBSERVE):
        pass

    assert recorder.export_errors() == ("RuntimeError",)
    assert len(recorder.records()) == 1
    adapter.shutdown()


def _record() -> SpanRecord:
    now = datetime(2026, 7, 13, 12, tzinfo=UTC).isoformat()
    return SpanRecord(
        trace_id="run:deterministic",
        span_id="span:deterministic",
        parent_span_id=None,
        name=SpanNames.OBSERVE,
        started_at=now,
        ended_at=now,
        duration_ms=0,
        status=SpanStatus.OK,
    )
