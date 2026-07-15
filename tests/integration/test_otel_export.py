from __future__ import annotations

import queue
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from blackcell.adapters.telemetry import RuntimeTelemetry
from blackcell.config import (
    API_TOKEN_ENV,
    DATA_DIR_ENV,
    OTEL_ENABLED_ENV,
    OTEL_ENDPOINT_ENV,
    OTEL_MAX_EXPORT_BATCH_SIZE_ENV,
    OTEL_MAX_QUEUE_SIZE_ENV,
    OTEL_SCHEDULE_DELAY_MILLISECONDS_ENV,
    REPOSITORY_ROOT_ENV,
    RuntimeProcessConfig,
)
from blackcell.operator import RepositoryOperator
from blackcell.workflows import WorkflowSpanName

TOKEN = "Runtime-v1_otel-integration.0123456789-ABCDEFG"
AMBIENT_HEADER_SECRET = "ambient-otel-header-secret"


class CaptureHandler(BaseHTTPRequestHandler):
    requests: queue.Queue[tuple[str, dict[str, str], bytes]] = queue.Queue()

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        self.requests.put(
            (
                self.path,
                {key.casefold(): value for key, value in self.headers.items()},
                self.rfile.read(length),
            )
        )
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def test_runtime_telemetry_exports_redacted_otlp_http_protobuf_without_ambient_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/traces"
    config = RuntimeProcessConfig.from_environment(
        {
            DATA_DIR_ENV: str(tmp_path / "data"),
            API_TOKEN_ENV: TOKEN,
            REPOSITORY_ROOT_ENV: str(repository),
            OTEL_ENABLED_ENV: "1",
            OTEL_ENDPOINT_ENV: endpoint,
            OTEL_MAX_QUEUE_SIZE_ENV: "32",
            OTEL_MAX_EXPORT_BATCH_SIZE_ENV: "16",
            OTEL_SCHEDULE_DELAY_MILLISECONDS_ENV: "60000",
        },
        hostname="test-host",
        process_id=42,
    )
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS", f"authorization={AMBIENT_HEADER_SECRET}"
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:1/wrong")
    telemetry = RuntimeTelemetry.from_config(config)

    try:
        database = config.security.paths.ensure_database_file()
        result = RepositoryOperator(
            repository,
            database_path=database,
            artifact_root=config.security.paths.artifact_root,
            workflow_telemetry=telemetry.workflow,
        ).run(objective=f"Inspect runtime readiness without exposing {TOKEN}.")
        telemetry.shutdown()
        path, headers, body = CaptureHandler.requests.get(timeout=5)
    finally:
        telemetry.shutdown()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert path == "/v1/traces"
    assert headers["content-type"] == "application/x-protobuf"
    assert "authorization" not in headers
    assert body
    assert TOKEN.encode() not in body
    assert AMBIENT_HEADER_SECRET.encode() not in body
    assert result.run_id.encode() in body
    request = ExportTraceServiceRequest.FromString(body)
    spans = tuple(
        span
        for resource in request.resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
    )
    assert tuple(span.name for span in spans) == tuple(item.value for item in WorkflowSpanName)
    assert len({span.trace_id for span in spans}) == 1
    assert all(len(span.trace_id) == 16 and len(span.span_id) == 8 for span in spans)
