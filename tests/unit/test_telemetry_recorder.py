from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from blackcell.telemetry import (
    ContentMode,
    ContentPolicy,
    SpanNames,
    SpanRecord,
    SpanStatus,
    TraceRecorder,
)


class CollectingExporter:
    def __init__(self) -> None:
        self.records: list[SpanRecord] = []

    def export(self, record: SpanRecord) -> None:
        self.records.append(record)


def test_trace_recorder_correlates_nested_spans_and_exports() -> None:
    exporter = CollectingExporter()
    recorder = TraceRecorder(exporters=(exporter,))

    with (
        recorder.span(
            SpanNames.BUILD_CONTEXT,
            trace_id="trace-1",
            correlation_ids={"run_id": "run-1"},
        ) as parent,
        recorder.span(SpanNames.MODEL_DECIDE) as child,
    ):
        child.set_attribute("model", "fixture")

    records = recorder.records(trace_id="trace-1")
    assert len(records) == 2
    child_record, parent_record = records
    assert child_record.parent_span_id == parent.span_id
    assert child_record.trace_id == parent_record.trace_id == "trace-1"
    assert len(exporter.records) == 2


def test_content_policy_redacts_content_and_secrets_before_storage() -> None:
    recorder = TraceRecorder()

    with recorder.span(
        SpanNames.MODEL_DECIDE,
        attributes={
            "model": "fixture",
            "prompt": "private prompt",
            "nested": {"api_token": "ghp_secret", "count": 3},
        },
    ):
        pass

    attributes = recorder.records()[0].attributes
    assert attributes["model"] == "fixture"
    assert attributes["prompt"] == "[REDACTED]"
    assert attributes["nested"] == {"api_token": "[REDACTED]", "count": 3}


def test_redact_sensitive_mode_detects_bearer_values_and_truncates() -> None:
    policy = ContentPolicy(mode=ContentMode.REDACT_SENSITIVE, max_string_chars=4)

    sanitized = policy.sanitize({"header": "Bearer abc", "label": "abcdefgh"})

    assert sanitized == {"header": "[REDACTED]", "label": "abcd"}


def test_content_policy_redacts_exact_runtime_secret_and_credential_shapes() -> None:
    token = "Runtime-v1_opaque-token.0123456789-ABCDEFG"
    policy = ContentPolicy(
        mode=ContentMode.REDACT_SENSITIVE,
        sensitive_values=(token,),
    )

    sanitized = policy.sanitize(
        {
            "detail": f"request failed with {token}",
            "authorization_header": "not-even-a-token",
            "nested": {
                "connection": "https://runtime:credential@example.test/path",
                "provider": "github_pat_abcdefghijklmnopqrstuvwxyz",
                token: "secret-in-key",
            },
        }
    )

    assert sanitized == {
        "detail": "[REDACTED]",
        "authorization_header": "[REDACTED]",
        "nested": {
            "connection": "[REDACTED]",
            "provider": "[REDACTED]",
            "[REDACTED]": "[REDACTED]",
        },
    }
    assert token not in repr(policy)


def test_configured_secret_is_redacted_from_exception_before_export() -> None:
    token = "Runtime-v1_opaque-token.0123456789-ABCDEFG"
    exporter = CollectingExporter()
    recorder = TraceRecorder(
        content_policy=ContentPolicy(sensitive_values=(token,)),
        exporters=(exporter,),
    )

    with (
        pytest.raises(RuntimeError, match="request failed"),
        recorder.span(
            SpanNames.MODEL_DECIDE,
            trace_id=token,
            parent_span_id=token,
            correlation_ids={"request_id": token, "api_token": "opaque"},
        ),
    ):
        raise RuntimeError(f"request failed with {token}")

    assert recorder.records()[0].attributes["error.message"] == "[REDACTED]"
    assert recorder.records()[0].trace_id == "[REDACTED]"
    assert recorder.records()[0].parent_span_id == "[REDACTED]"
    assert recorder.records()[0].correlation_ids == {
        "request_id": "[REDACTED]",
        "api_token": "[REDACTED]",
    }
    assert exporter.records[0].attributes["error.message"] == "[REDACTED]"
    assert token not in repr(exporter.records[0])


def test_error_span_redacts_exception_message_and_preserves_failure() -> None:
    recorder = TraceRecorder()

    with (
        pytest.raises(RuntimeError, match="sensitive detail"),
        recorder.span(SpanNames.AFFORDANCE_EXECUTE),
    ):
        raise RuntimeError("sensitive detail")

    record = recorder.records()[0]
    assert record.status is SpanStatus.ERROR
    assert record.attributes["error.type"] == "RuntimeError"
    assert record.attributes["error.message"] == "[REDACTED]"


def test_span_names_must_use_blackcell_namespace() -> None:
    recorder = TraceRecorder()

    with pytest.raises(ValueError, match=r"blackcell\.\*"), recorder.span("provider.model.call"):
        pass


def test_duration_can_be_measured_with_injected_clocks() -> None:
    wall_values = iter(
        (
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 1, tzinfo=UTC) + timedelta(milliseconds=25),
        )
    )
    ticks = iter((1.0, 1.025))
    recorder = TraceRecorder(
        wall_clock=lambda: next(wall_values), monotonic_clock=lambda: next(ticks)
    )

    with recorder.span(SpanNames.OBSERVE):
        pass

    assert recorder.records()[0].duration_ms == pytest.approx(25)
