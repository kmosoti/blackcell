from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from blackcell.adapters.telemetry import (
    OpenTelemetryMappingError,
    OpenTelemetrySpanExporter,
    RuntimeTelemetry,
)
from blackcell.config import (
    API_TOKEN_ENV,
    DATA_DIR_ENV,
    OTEL_ENABLED_ENV,
    OTEL_ENDPOINT_ENV,
    REPOSITORY_ROOT_ENV,
    RuntimeProcessConfig,
)
from blackcell.telemetry import (
    ContentPolicy,
    SpanNames,
    SpanRecord,
    SpanStatus,
    TraceRecorder,
)

TOKEN = "runtime_otel-token.0123456789-ABCDEFG"


class FailingProcessor(SpanProcessor):
    def on_end(self, span: ReadableSpan) -> None:
        del span
        raise RuntimeError("provider detail must remain isolated")


class FailingLifecycleExporter:
    def __init__(self) -> None:
        self.flush_timeouts: list[int] = []
        self.shutdown_calls = 0

    def export(self, record: object) -> None:
        del record

    def force_flush(self, timeout_millis: int) -> None:
        self.flush_timeouts.append(timeout_millis)
        raise RuntimeError("flush detail must remain isolated")

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        raise RuntimeError("shutdown detail must remain isolated")


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


def test_runtime_telemetry_composes_explicit_export_and_suppresses_shutdown_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    config = RuntimeProcessConfig.from_environment(
        {
            DATA_DIR_ENV: str(tmp_path / "data"),
            API_TOKEN_ENV: TOKEN,
            REPOSITORY_ROOT_ENV: str(repository),
            OTEL_ENABLED_ENV: "1",
            OTEL_ENDPOINT_ENV: "http://127.0.0.1:4318/v1/traces",
        }
    )
    exporter = FailingLifecycleExporter()
    captured: dict[str, object] = {}

    def build_exporter(**kwargs: object) -> FailingLifecycleExporter:
        captured.update(kwargs)
        return exporter

    monkeypatch.setattr(OpenTelemetrySpanExporter, "otlp_http", build_exporter)

    runtime = RuntimeTelemetry.from_config(config)
    runtime.shutdown()

    assert runtime.recorder is not None
    assert captured["endpoint"] == "http://127.0.0.1:4318/v1/traces"
    assert captured["timeout_seconds"] == 10
    resource = captured["resource"]
    assert isinstance(resource, Resource)
    assert resource.attributes["service.name"] == "blackcell-runtime"
    assert resource.attributes["service.instance.id"] == "service:runtime"
    assert exporter.flush_timeouts == [10_000]
    assert exporter.shutdown_calls == 1


def test_runtime_telemetry_disabled_shutdown_is_a_noop(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    config = RuntimeProcessConfig.from_environment(
        {
            DATA_DIR_ENV: str(tmp_path / "data"),
            API_TOKEN_ENV: TOKEN,
            REPOSITORY_ROOT_ENV: str(repository),
        }
    )

    runtime = RuntimeTelemetry.from_config(config)
    runtime.shutdown()

    assert runtime.recorder is None


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
