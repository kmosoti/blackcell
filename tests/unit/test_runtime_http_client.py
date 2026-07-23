from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from urllib.error import URLError
from urllib.request import OpenerDirector

import msgspec
import pytest

import blackcell.adapters.runtime_http as runtime_http
from blackcell.adapters.runtime_http import (
    RuntimeClientError,
    RuntimeClientFailureCode,
    RuntimeHttpClient,
    RuntimeHttpResponse,
    RuntimeServiceStatus,
    UrllibRuntimeTransport,
)
from blackcell.bootstrap.alpha_runtime import AlphaRuntimeApiService
from blackcell.config import SecretValue
from blackcell.interfaces.http import (
    AlphaCancelRunRequest,
    AlphaEventPageResponse,
    AlphaEventResponse,
    ErrorResponse,
    HealthResponse,
    encode_contract,
)
from blackcell.interfaces.http.contracts import MAX_REQUEST_BODY_BYTES
from blackcell.kernel import EventStore
from tests.unit.test_alpha_runtime import _intent, _plan, _project, _repository, _run

_TOKEN = "Alpha-runtime-client-token.0123456789-ABCDEFG"


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None
    timeout_seconds: float


class FakeTransport:
    def __init__(self, *responses: RuntimeHttpResponse) -> None:
        self.responses = list(responses)
        self.requests: list[RecordedRequest] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> RuntimeHttpResponse:
        self.requests.append(RecordedRequest(method, url, dict(headers), body, timeout_seconds))
        return self.responses.pop(0)


def test_status_decodes_public_health_contracts_and_not_ready_state() -> None:
    transport = FakeTransport(
        _response(200, HealthResponse(status="live")),
        _response(503, HealthResponse(status="not-ready")),
    )

    status = RuntimeHttpClient(transport=transport).status()

    assert status == RuntimeServiceStatus(
        endpoint="http://127.0.0.1:8080",
        live=True,
        ready=False,
    )
    assert [(item.method, item.url) for item in transport.requests] == [
        ("GET", "http://127.0.0.1:8080/health/live"),
        ("GET", "http://127.0.0.1:8080/health/ready"),
    ]
    assert all(item.headers == {"accept": "application/json"} for item in transport.requests)
    assert all(item.timeout_seconds == 5.0 for item in transport.requests)


def test_client_rejects_malformed_or_remote_plaintext_endpoints() -> None:
    endpoints = (
        "",
        "http://runtime.example:8080",
        "http://user:password@127.0.0.1:8080",
        "http://127.0.0.1:8080/api",
        "http://127.0.0.1:0",
        "https://runtime.example/query?value=1",
        "ftp://127.0.0.1:8080",
        "http://127.0.0.1:8080\nignored",
    )
    for endpoint in endpoints:
        with pytest.raises(RuntimeClientError) as caught:
            RuntimeHttpClient(endpoint=endpoint, transport=FakeTransport())

        assert caught.value.code is RuntimeClientFailureCode.INVALID_ENDPOINT


def test_client_accepts_remote_https_and_loopback_http() -> None:
    assert (
        RuntimeHttpClient(
            endpoint="https://runtime.example:8443/",
            transport=FakeTransport(),
        ).endpoint
        == "https://runtime.example:8443"
    )
    assert (
        RuntimeHttpClient(
            endpoint="http://[::1]:8080/",
            transport=FakeTransport(),
        ).endpoint
        == "http://[::1]:8080"
    )


def test_client_validates_generic_and_replay_timeouts_independently() -> None:
    RuntimeHttpClient(transport=FakeTransport(), timeout_seconds=30.0)
    RuntimeHttpClient(transport=FakeTransport(), replay_timeout_seconds=3_600.0)

    for timeout in (30.01, 0, True, float("nan")):
        with pytest.raises(RuntimeClientError) as caught:
            RuntimeHttpClient(transport=FakeTransport(), timeout_seconds=timeout)
        assert caught.value.code is RuntimeClientFailureCode.INVALID_TIMEOUT

    for timeout in (3_600.01, 0, True, float("nan")):
        with pytest.raises(RuntimeClientError) as caught:
            RuntimeHttpClient(transport=FakeTransport(), replay_timeout_seconds=timeout)
        assert caught.value.code is RuntimeClientFailureCode.INVALID_TIMEOUT


def test_client_failures_are_typed_bounded_and_content_free() -> None:
    rejected = RuntimeHttpClient(
        transport=FakeTransport(_response(401, ErrorResponse(error="authentication-required"))),
    )
    with pytest.raises(RuntimeClientError) as denied:
        rejected.status()
    assert denied.value.code is RuntimeClientFailureCode.REQUEST_REJECTED
    assert denied.value.status_code == 401
    assert denied.value.service_error == "authentication-required"

    invalid = RuntimeHttpClient(
        transport=FakeTransport(RuntimeHttpResponse(200, "text/plain", b"credential leak"))
    )
    with pytest.raises(RuntimeClientError) as malformed:
        invalid.status()
    assert malformed.value.code is RuntimeClientFailureCode.INVALID_RESPONSE
    assert "credential leak" not in str(malformed.value)


def test_stdlib_transport_bounds_responses_and_connection_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_http, "MAX_RESPONSE_BODY_BYTES", 16)
    oversized = UrllibRuntimeTransport(
        _opener=cast(OpenerDirector, _FakeOpener(_FakeUrlResponse(b"x" * 17)))
    )
    with pytest.raises(RuntimeClientError) as too_large:
        oversized.request(
            "GET",
            "http://127.0.0.1:8080/health/live",
            headers={},
            body=None,
            timeout_seconds=1.0,
        )
    assert too_large.value.code is RuntimeClientFailureCode.RESPONSE_TOO_LARGE

    unavailable = UrllibRuntimeTransport(
        _opener=cast(OpenerDirector, _FakeOpener(URLError("sensitive host details")))
    )
    with pytest.raises(RuntimeClientError) as failed:
        unavailable.request(
            "GET",
            "http://127.0.0.1:8080/health/live",
            headers={},
            body=None,
            timeout_seconds=1.0,
        )
    assert failed.value.code is RuntimeClientFailureCode.CONNECTION_FAILED
    assert "sensitive host details" not in str(failed.value)


def test_alpha_client_decodes_valid_service_response_larger_than_request_limit() -> None:
    event = AlphaEventResponse(
        event_id="event-1",
        cursor=1,
        stream_id="alpha:plan:plan-1",
        stream_sequence=1,
        event_type="alpha.plan.accepted",
        event_schema_version=1,
        recorded_at="2026-07-23T12:00:00+00:00",
        correlation_id="correlation-1",
        causation_id=None,
        actor="operator",
        payload_digest="sha256:" + "a" * 64,
        payload={"accepted_plan": "x" * MAX_REQUEST_BODY_BYTES},
    )
    page = AlphaEventPageResponse(
        after_cursor=0,
        limit=1,
        scanned_events=1,
        events=(event,),
        next_cursor=1,
        has_more=False,
    )
    body = encode_contract(page)
    response = _FakeUrlResponse(body)
    transport = UrllibRuntimeTransport(
        _opener=cast(OpenerDirector, _FakeOpener(response)),
    )
    client = RuntimeHttpClient(transport=transport, token=SecretValue(_TOKEN))

    assert len(body) > MAX_REQUEST_BODY_BYTES
    assert client.list_alpha_events(limit=1) == page
    assert response.closed


def test_alpha_client_sends_strict_authenticated_requests_and_decodes_contracts(
    tmp_path,
) -> None:
    repository = _repository(tmp_path)
    service = AlphaRuntimeApiService(EventStore(tmp_path / "state.sqlite3"), repository)
    project_request = _project(repository)
    intent_request = _intent()
    plan_request = _plan()
    run_request = _run()
    cancel_request = AlphaCancelRunRequest(
        schema_version="alpha-cancel-run-request/v1",
        idempotency_key="cancel-run-1",
    )
    project = service.register_project(project_request, principal_id="client:test")
    intent = service.accept_intent(intent_request, principal_id="client:test")
    plan = service.accept_plan(plan_request, principal_id="client:test")
    run = service.submit_run(run_request, principal_id="client:test")
    events = service.list_events(after_cursor=0, limit=20)
    replay = service.replay_run("run-1")
    canceled = service.cancel_run("run-1", cancel_request, principal_id="client:test")
    transport = FakeTransport(
        _response(201, project),
        _response(201, intent),
        _response(201, plan),
        _response(202, run),
        _response(200, run),
        _response(200, events),
        _response(200, replay),
        _response(202, canceled),
    )
    client = RuntimeHttpClient(transport=transport, token=SecretValue(_TOKEN))

    assert client.register_alpha_project(project_request) == project
    assert client.accept_alpha_intent(intent_request) == intent
    assert client.accept_alpha_plan(plan_request) == plan
    assert client.submit_alpha_run(run_request) == run
    assert client.inspect_alpha_run("run-1") == run
    assert client.list_alpha_events(after_cursor=0, limit=20) == events
    assert client.replay_alpha_run("run-1") == replay
    assert client.cancel_alpha_run("run-1", cancel_request) == canceled

    assert [item.method for item in transport.requests] == [
        "POST",
        "POST",
        "POST",
        "POST",
        "GET",
        "GET",
        "GET",
        "POST",
    ]
    assert [item.url for item in transport.requests] == [
        "http://127.0.0.1:8080/api/alpha/v1/projects",
        "http://127.0.0.1:8080/api/alpha/v1/intents",
        "http://127.0.0.1:8080/api/alpha/v1/plans",
        "http://127.0.0.1:8080/api/alpha/v1/runs",
        "http://127.0.0.1:8080/api/alpha/v1/runs/run-1/status",
        "http://127.0.0.1:8080/api/alpha/v1/events?after=0&limit=20",
        "http://127.0.0.1:8080/api/alpha/v1/runs/run-1/replay",
        "http://127.0.0.1:8080/api/alpha/v1/runs/run-1/cancel",
    ]
    assert all(item.headers["authorization"] == f"Bearer {_TOKEN}" for item in transport.requests)
    assert transport.requests[0].body == encode_contract(project_request)
    assert transport.requests[4].body is None
    assert transport.requests[-1].body == encode_contract(cancel_request)
    assert [item.timeout_seconds for item in transport.requests] == [
        5.0,
        5.0,
        5.0,
        5.0,
        5.0,
        5.0,
        600.0,
        5.0,
    ]
    assert _TOKEN not in repr(client)


def test_alpha_client_bounds_identifiers_pagination_auth_and_failures() -> None:
    transport = FakeTransport()
    unauthenticated = RuntimeHttpClient(transport=transport)
    with pytest.raises(RuntimeClientError) as missing:
        unauthenticated.inspect_alpha_run("run-1")
    assert missing.value.code is RuntimeClientFailureCode.MISSING_AUTHENTICATION
    assert transport.requests == []

    client = RuntimeHttpClient(transport=transport, token=SecretValue(_TOKEN))
    for run_id in ("", "../run", "run/one", "run one", "x" * 121):
        with pytest.raises(RuntimeClientError) as invalid:
            client.inspect_alpha_run(run_id)
        assert invalid.value.code is RuntimeClientFailureCode.INVALID_REQUEST
    for after, limit in ((-1, 1), (True, 1), (0, 0), (0, 201)):
        with pytest.raises(RuntimeClientError) as invalid:
            client.list_alpha_events(after_cursor=after, limit=limit)
        assert invalid.value.code is RuntimeClientFailureCode.INVALID_REQUEST
    assert transport.requests == []

    denied_transport = FakeTransport(_response(401, ErrorResponse(error="authentication-required")))
    denied_client = RuntimeHttpClient(
        transport=denied_transport,
        token=SecretValue(_TOKEN),
    )
    with pytest.raises(RuntimeClientError) as denied:
        denied_client.inspect_alpha_run("run-1")
    assert denied.value.code is RuntimeClientFailureCode.REQUEST_REJECTED
    assert denied.value.cli_exit_code == 4
    assert _TOKEN not in str(denied.value)
    assert _TOKEN not in repr(denied.value)


class _FakeHeaders:
    def get(self, name: str, default: str = "") -> str:
        del name
        return default or "application/json"


class _FakeUrlResponse:
    status = 200
    headers = _FakeHeaders()

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.closed = False

    def read(self, limit: int) -> bytes:
        return self.body[:limit]

    def close(self) -> None:
        self.closed = True


class _FakeOpener:
    def __init__(self, result: _FakeUrlResponse | URLError) -> None:
        self.result = result

    def open(self, request: object, *, timeout: float) -> _FakeUrlResponse:
        del request, timeout
        if isinstance(self.result, URLError):
            raise self.result
        return self.result


def _response(
    status_code: int,
    contract: msgspec.Struct,
) -> RuntimeHttpResponse:
    return RuntimeHttpResponse(
        status_code=status_code,
        content_type="application/json; charset=utf-8",
        body=encode_contract(contract),
    )
