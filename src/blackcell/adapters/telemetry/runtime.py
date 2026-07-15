from __future__ import annotations

from contextlib import suppress

from opentelemetry.sdk.resources import Resource

from blackcell.adapters.telemetry.otel import OpenTelemetrySpanExporter
from blackcell.adapters.telemetry.workflow import TraceWorkflowTelemetry
from blackcell.config import RuntimeProcessConfig
from blackcell.telemetry import ContentPolicy, TraceRecorder
from blackcell.workflows.telemetry import NullWorkflowTelemetry, WorkflowTelemetry


class RuntimeTelemetry:
    """Own the opt-in exporter and its process lifecycle."""

    def __init__(
        self,
        workflow: WorkflowTelemetry,
        *,
        exporter: OpenTelemetrySpanExporter | None = None,
        flush_timeout_millis: int = 10_000,
    ) -> None:
        self.workflow = workflow
        self._exporter = exporter
        self._flush_timeout_millis = flush_timeout_millis

    @classmethod
    def from_config(
        cls,
        config: RuntimeProcessConfig,
        *,
        content_policy: ContentPolicy | None = None,
    ) -> RuntimeTelemetry:
        telemetry = config.telemetry
        if not telemetry.enabled:
            return cls(NullWorkflowTelemetry())
        if telemetry.endpoint is None:  # pragma: no cover - configuration invariant
            raise ValueError("enabled telemetry requires an endpoint")
        policy = content_policy or config.security.telemetry_policy()
        resource = Resource(
            {
                "service.name": "blackcell-runtime",
                "service.version": "0.2.0",
                "service.instance.id": policy.sanitize_text(config.worker_id),
            }
        )
        exporter = OpenTelemetrySpanExporter.otlp_http(
            endpoint=telemetry.endpoint,
            timeout_seconds=telemetry.timeout_seconds,
            max_queue_size=telemetry.max_queue_size,
            max_export_batch_size=telemetry.max_export_batch_size,
            schedule_delay_millis=telemetry.schedule_delay_milliseconds,
            resource=resource,
        )
        recorder = TraceRecorder(
            content_policy=policy,
            exporters=(exporter,),
            max_records=0,
        )
        return cls(
            TraceWorkflowTelemetry(recorder),
            exporter=exporter,
            flush_timeout_millis=telemetry.timeout_seconds * 1_000,
        )

    def shutdown(self) -> None:
        if self._exporter is None:
            return
        with suppress(Exception):
            self._exporter.force_flush(self._flush_timeout_millis)
        with suppress(Exception):
            self._exporter.shutdown()


__all__ = ["RuntimeTelemetry"]
