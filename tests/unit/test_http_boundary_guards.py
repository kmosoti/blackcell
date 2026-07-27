from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

import msgspec
import pytest
from litestar.exceptions import HTTPException

from blackcell.bootstrap.runtime_service import RuntimeService
from blackcell.config import SecretValue
from blackcell.interfaces import (
    BearerAuthenticator,
    ScopeAuthorizer,
    ServicePrincipal,
    ServiceScope,
)
from blackcell.interfaces.http import (
    MAX_REQUEST_BODY_BYTES,
    RUN_QUERY_MEDIA_TYPE,
    RUN_QUERY_RESULT_MEDIA_TYPE,
    RunQueryRequest,
    RunQueryResponse,
)
from blackcell.interfaces.http.app import (
    HttpBoundaryError,
    _authorize_query,
    _decode_query_json,
    _etag_matches,
    _exception_response,
    _invoke,
    _path_id,
    _query_integer,
    _query_response_acceptable,
    _QuerySyntaxError,
    _read_query_body,
    _supported_query_content_type,
    _websocket_query,
    create_http_app,
)
from blackcell.interfaces.http.ports import RuntimeApiError, RuntimeApiFailureCode
from blackcell.kernel import EventStore
from tests.unit.test_runtime_http_api import _TOKEN, _auth, _client, _HttpPort
from tests.unit.test_runtime_service import _repository


@pytest.mark.parametrize(
    ("value", "accepted"),
    (
        ("application/vnd.blackcell.run-query+json", True),
        ('application/vnd.blackcell.run-query+json; charset="utf-8"', True),
        ("application/json", False),
        ("application/vnd.blackcell.run-query+json; charset", False),
        ("application/vnd.blackcell.run-query+json; profile=utf-8", False),
        ("application/vnd.blackcell.run-query+json; charset=ascii", False),
    ),
)
def test_query_content_type_is_closed(value: str, accepted: bool) -> None:
    assert _supported_query_content_type(value) is accepted


@pytest.mark.parametrize(
    ("values", "accepted"),
    (
        ((), True),
        (("*/*",), True),
        (("application/*",), True),
        (("application/vnd.blackcell.run-query-result+json",), True),
        (("text/plain",), False),
        (("text/plain;q=0",), False),
        (("text/plain;level=1",), False),
        (("text/plain;q=invalid",), False),
        (("text/plain;q=2",), False),
        ((("x" * 8_193),), False),
    ),
)
def test_query_accept_negotiation_is_bounded(values: tuple[str, ...], accepted: bool) -> None:
    assert _query_response_acceptable(values) is accepted


def test_query_json_and_etag_parsers_reject_ambiguous_inputs() -> None:
    with pytest.raises(_QuerySyntaxError):
        _decode_query_json(b'{"value": NaN}')

    assert not _etag_matches((), '"digest"')
    assert _etag_matches(("*",), '"digest"')
    assert _etag_matches(('W/"digest"',), '"digest"')
    assert not _etag_matches(('"other"',), '"digest"')


@pytest.mark.parametrize(
    ("headers", "events", "expected", "status"),
    (
        (
            ((b"content-length", b"1"), (b"content-length", b"1")),
            ({"type": "http.request", "body": b"x"},),
            None,
            413,
        ),
        (
            ((b"content-length", str(MAX_REQUEST_BODY_BYTES + 1).encode("ascii")),),
            ({"type": "http.request", "body": b""},),
            None,
            413,
        ),
        ((), ({"type": "http.disconnect"},), None, 400),
        ((), ({"type": "http.request", "body": "not-bytes"},), None, 400),
        (
            (),
            ({"type": "http.request", "body": b"x" * (MAX_REQUEST_BODY_BYTES + 1)},),
            None,
            413,
        ),
        (
            (),
            (
                {"type": "http.request", "body": b"left", "more_body": True},
                {"type": "http.request", "body": b"-right", "more_body": False},
            ),
            b"left-right",
            None,
        ),
    ),
)
def test_query_body_reader_bounds_transport_ambiguity(
    headers: tuple[tuple[bytes, bytes], ...],
    events: tuple[dict[str, object], ...],
    expected: bytes | None,
    status: int | None,
) -> None:
    pending = list(events)

    async def receive() -> Any:
        return pending.pop(0)

    operation = _read_query_body(
        cast(Any, {"headers": headers}),
        cast(Callable[[], Awaitable[Any]], receive),
    )
    if status is None:
        assert asyncio.run(operation) == expected
        return
    with pytest.raises(HttpBoundaryError) as caught:
        asyncio.run(operation)
    assert caught.value.status_code == status


@pytest.mark.parametrize(
    ("query", "expected"),
    (
        (b"ticket=token&after=0", ("token", 0)),
        (b"", None),
        ("not-bytes", None),
        (b"ticket=token", None),
        (b"ticket=token&after=bad", None),
        (b"ticket=token&after=9223372036854775808", None),
        (b"ticket=token&after=0&extra=value", None),
        (b"ticket=\xff&after=0", None),
    ),
)
def test_websocket_query_is_exact_and_bounded(
    query: bytes | str,
    expected: tuple[str, int] | None,
) -> None:
    socket = cast(Any, type("Socket", (), {"scope": {"query_string": query}})())
    assert _websocket_query(socket) == expected


def test_path_and_integer_parameters_fail_closed() -> None:
    assert _path_id("run-1") == "run-1"
    for invalid in ("", "x" * 201, "run\x7f"):
        with pytest.raises(HttpBoundaryError) as caught:
            _path_id(invalid)
        assert caught.value.status_code == 400

    class Query:
        def __init__(self, values: dict[str, str]) -> None:
            self.query_params = values

    assert _query_integer(cast(Any, Query({})), "after", default=7, minimum=0, maximum=10) == 7
    assert (
        _query_integer(
            cast(Any, Query({"after": "10"})),
            "after",
            default=7,
            minimum=0,
            maximum=10,
        )
        == 10
    )
    for invalid in ("-1", "11", "not-a-number"):
        with pytest.raises(HttpBoundaryError):
            _query_integer(
                cast(Any, Query({"after": invalid})),
                "after",
                default=7,
                minimum=0,
                maximum=10,
            )


def test_query_authority_and_runtime_failures_are_content_free() -> None:
    principal = ServicePrincipal("client:test", (ServiceScope.READ,))
    authenticator = BearerAuthenticator(SecretValue(_TOKEN), principal)
    scope = cast(
        Any,
        {"headers": ((b"authorization", f"Bearer {_TOKEN}".encode("ascii")),)},
    )

    with pytest.raises(HttpBoundaryError) as quota:
        _authorize_query(
            scope,
            authenticator=authenticator,
            authorizer=ScopeAuthorizer(),
            request_quota=_RejectingQuota(),
        )
    assert quota.value.status_code == 429

    for code, status in (
        (RuntimeApiFailureCode.INVALID_REQUEST, 400),
        (RuntimeApiFailureCode.NOT_FOUND, 404),
        (RuntimeApiFailureCode.CONFLICT, 409),
        (RuntimeApiFailureCode.NOT_READY, 503),
        (RuntimeApiFailureCode.STORAGE_QUOTA_EXCEEDED, 507),
    ):
        with pytest.raises(HttpBoundaryError) as caught:
            _invoke(lambda code=code: _raise_runtime_error(code))
        assert caught.value.status_code == status
        assert str(caught.value) == code.value


def test_http_exception_normalization_never_discloses_internal_detail() -> None:
    server = _exception_response(
        cast(Any, object()),
        HTTPException(status_code=503, detail="provider secret"),
    )
    unknown = _exception_response(
        cast(Any, object()),
        HTTPException(status_code=418, detail="provider secret"),
    )
    unexpected = _exception_response(cast(Any, object()), RuntimeError("provider secret"))

    assert server.status_code == 500
    assert unknown.status_code == 400
    assert unexpected.status_code == 500
    for response in (server, unknown, unexpected):
        assert "provider secret" not in response.content.decode("utf-8")


@pytest.mark.parametrize("poll_seconds", (True, "0.25", 0.01, 6.0))
def test_http_app_rejects_ambiguous_poll_configuration(
    tmp_path,
    poll_seconds: object,
) -> None:
    repository = _repository(tmp_path)
    service = _HttpPort(RuntimeService(EventStore(tmp_path / "state.sqlite3"), repository))
    principal = ServicePrincipal("client:test", (ServiceScope.READ, ServiceScope.RUN))

    with pytest.raises(ValueError, match="web_poll_seconds"):
        create_http_app(
            cast(Any, service),
            authenticator=BearerAuthenticator(SecretValue(_TOKEN), principal),
            authorizer=ScopeAuthorizer(),
            web_poll_seconds=cast(Any, poll_seconds),
        )


def test_health_and_json_edges_are_explicit(tmp_path) -> None:
    repository = _repository(tmp_path)
    service = _HttpPort(RuntimeService(EventStore(tmp_path / "state.sqlite3"), repository))

    with _client(service) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        missing_media_type = client.post(
            "/api/v1/projects",
            content=b"{}",
            headers=_auth(),
        )
        invalid_after = client.get("/api/v1/events?after=-1", headers=_auth())

    assert live.status_code == ready.status_code == 200
    assert live.json()["status"] == "live"
    assert ready.json()["status"] == "ready"
    assert missing_media_type.status_code == 415
    assert invalid_after.status_code == 400


def test_query_transport_normalizes_encoding_and_service_failures(tmp_path) -> None:
    repository = _repository(tmp_path)
    runtime = RuntimeService(EventStore(tmp_path / "state.sqlite3"), repository)
    query = msgspec.json.encode(RunQueryRequest(schema_version="run-query-request/v1"))
    headers = {
        **_auth(),
        "accept": RUN_QUERY_RESULT_MEDIA_TYPE,
        "content-type": RUN_QUERY_MEDIA_TYPE,
    }

    with _client(_HttpPort(runtime)) as client:
        encoded = client.request(
            "QUERY",
            "/api/v1/runs",
            content=query,
            headers={**headers, "content-encoding": "gzip"},
        )
    assert encoded.status_code == 415
    assert encoded.json()["error"] == "unsupported-query-content-encoding"

    for failure, status in (
        (RuntimeApiError(RuntimeApiFailureCode.STORAGE_QUOTA_EXCEEDED), 507),
        (RuntimeError("provider secret"), 500),
    ):
        with _client(_FailingQueryPort(runtime, failure)) as client:
            response = client.request(
                "QUERY",
                "/api/v1/runs",
                content=query,
                headers=headers,
            )
        assert response.status_code == status
        assert "provider secret" not in response.text


class _RejectingQuota:
    def consume(self) -> bool:
        return False


class _FailingQueryPort(_HttpPort):
    def __init__(self, service: RuntimeService, failure: Exception) -> None:
        super().__init__(service)
        self.failure = failure

    def query_runs(self, request: RunQueryRequest) -> RunQueryResponse:
        del request
        raise self.failure


def _raise_runtime_error(code: RuntimeApiFailureCode) -> msgspec.Struct:
    raise RuntimeApiError(code)
