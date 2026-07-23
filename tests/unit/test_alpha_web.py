from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from litestar.exceptions import WebSocketDisconnect
from litestar.testing import TestClient

from blackcell.bootstrap.alpha_runtime import AlphaRuntimeApiService
from blackcell.config import SecretValue
from blackcell.interfaces import (
    BearerAuthenticator,
    ScopeAuthorizer,
    ServicePrincipal,
    ServiceScope,
)
from blackcell.interfaces.http import (
    AlphaEventPageResponse,
    AlphaWebConnectionLimiter,
    AlphaWebSocketTicketResponse,
    AlphaWebTicketAuthority,
    AlphaWebTicketError,
    AlphaWebTicketFailureCode,
    create_http_app,
    decode_contract,
)
from blackcell.kernel import EventStore
from tests.unit.test_alpha_http_api import (
    _TOKEN,
    _AlphaHttpPort,
    _auth,
    _intent_body,
    _plan_body,
    _project_body,
    _run_body,
)
from tests.unit.test_alpha_runtime import _base_commit, _repository


def test_ticket_authority_is_single_use_bounded_expiring_and_redacted() -> None:
    now = [100.0]
    values = iter(("A" * 32, "B" * 32, "C" * 32, "D" * 32))
    authority = AlphaWebTicketAuthority(
        ttl_seconds=20,
        max_pending=1,
        clock=lambda: now[0],
        token_factory=lambda: next(values),
    )
    principal = ServicePrincipal("browser:test", (ServiceScope.READ,))

    first = authority.issue(principal)
    response = first.response()

    assert response.ticket == "A" * 32
    assert response.expires_in_seconds == 20
    assert response.websocket_path == "/api/alpha/v1/ui/events"
    assert response.schema_version == "alpha-web-socket-ticket/v1"
    assert response.ticket not in repr(response)
    assert response.ticket not in repr(first)
    assert authority.pending_count == 1
    with pytest.raises(AlphaWebTicketError) as captured:
        authority.issue(principal)
    assert captured.value.code is AlphaWebTicketFailureCode.CAPACITY_EXCEEDED

    assert authority.consume(response.ticket) == principal
    assert authority.pending_count == 0
    with pytest.raises(AlphaWebTicketError) as captured:
        authority.consume(response.ticket)
    assert captured.value.code is AlphaWebTicketFailureCode.INVALID_TICKET

    expiring = authority.issue(principal).response()
    now[0] += 21
    with pytest.raises(AlphaWebTicketError) as captured:
        authority.consume(expiring.ticket)
    assert captured.value.code is AlphaWebTicketFailureCode.INVALID_TICKET
    assert authority.pending_count == 0

    invalid_source = AlphaWebTicketAuthority(token_factory=lambda: "short")
    with pytest.raises(AlphaWebTicketError) as captured:
        invalid_source.issue(principal)
    assert captured.value.code is AlphaWebTicketFailureCode.GENERATION_FAILED


def test_ticket_route_requires_read_scope_and_returns_no_store_contract(tmp_path: Path) -> None:
    service = _service(tmp_path)
    authority = AlphaWebTicketAuthority(token_factory=lambda: "T" * 32)

    with _client(service, authority=authority) as client:
        unauthenticated = client.post("/api/alpha/v1/ui/socket-tickets")
        issued = client.post("/api/alpha/v1/ui/socket-tickets", headers=_auth())

    assert unauthenticated.status_code == 401
    assert issued.status_code == 201
    assert issued.headers["cache-control"] == "no-store"
    assert issued.headers["x-content-type-options"] == "nosniff"
    assert "set-cookie" not in issued.headers
    contract = decode_contract(issued.content, AlphaWebSocketTicketResponse)
    assert contract.ticket == "T" * 32
    assert _TOKEN not in issued.text
    assert authority.pending_count == 1

    with _client(
        service,
        authority=AlphaWebTicketAuthority(),
        scopes=(ServiceScope.RUN,),
    ) as client:
        forbidden = client.post("/api/alpha/v1/ui/socket-tickets", headers=_auth())
    assert forbidden.status_code == 403
    assert forbidden.json()["error"] == "insufficient-scope"


def test_websocket_streams_resumed_typed_events_and_rejects_ticket_replay(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    authority = AlphaWebTicketAuthority()
    limiter = AlphaWebConnectionLimiter(2)

    with _client(service, authority=authority, limiter=limiter) as client:
        _seed_run(client, tmp_path / "repository")
        ticket = _ticket(client)
        url = _socket_url(ticket, after=2)
        with client.websocket_connect(url) as socket:
            page = decode_contract(socket.receive_bytes(), AlphaEventPageResponse)
            assert tuple(event.cursor for event in page.events) == (3, 4)
            assert tuple(event.event_type for event in page.events) == (
                "alpha.plan.accepted",
                "alpha.run.queued",
            )
            assert page.after_cursor == 2
            assert page.next_cursor == 4
            assert page.has_more is False
        _wait_until(lambda: limiter.active == 0)

        with pytest.raises(WebSocketDisconnect) as captured, client.websocket_connect(url):
            pass
        assert captured.value.code == 4401
        assert authority.pending_count == 0


def test_websocket_rejects_invalid_cursor_client_writes_and_connection_overflow(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    authority = AlphaWebTicketAuthority()
    limiter = AlphaWebConnectionLimiter(1)

    with _client(
        service,
        authority=authority,
        limiter=limiter,
        web_poll_seconds=0.05,
    ) as client:
        invalid_ticket = _ticket(client)
        with (
            pytest.raises(WebSocketDisconnect) as captured,
            client.websocket_connect(
                f"/api/alpha/v1/ui/events?ticket={invalid_ticket}&after=invalid"
            ),
        ):
            pass
        assert captured.value.code == 4400

        first_ticket = _ticket(client)
        with client.websocket_connect(_socket_url(first_ticket)) as first:
            first.receive_bytes()
            assert limiter.active == 1

            overflow_ticket = _ticket(client)
            with (
                pytest.raises(WebSocketDisconnect) as captured,
                client.websocket_connect(_socket_url(overflow_ticket)),
            ):
                pass
            assert captured.value.code == 4429
            assert limiter.active == 1

            first.send_text("not-allowed")
            with pytest.raises(WebSocketDisconnect) as captured:
                first.receive_bytes()
            assert captured.value.code == 4400
        _wait_until(lambda: limiter.active == 0)

    invalid_root = tmp_path / "invalid-source"
    invalid_root.mkdir()
    invalid_service = _invalid_service(invalid_root)
    invalid_authority = AlphaWebTicketAuthority()
    invalid_limiter = AlphaWebConnectionLimiter(1)
    with _client(
        invalid_service,
        authority=invalid_authority,
        limiter=invalid_limiter,
    ) as client:
        ticket = _ticket(client)
        with client.websocket_connect(_socket_url(ticket)) as socket:
            with pytest.raises(WebSocketDisconnect) as captured:
                socket.receive_bytes()
            assert captured.value.code == 1011
    _wait_until(lambda: invalid_limiter.active == 0)


def _service(tmp_path: Path) -> _AlphaHttpPort:
    repository = _repository(tmp_path)
    return _AlphaHttpPort(
        AlphaRuntimeApiService(EventStore(tmp_path / "state.sqlite3"), repository)
    )


def _invalid_service(tmp_path: Path) -> _AlphaHttpPort:
    repository = _repository(tmp_path)
    return _InvalidEventPagePort(
        AlphaRuntimeApiService(EventStore(tmp_path / "state.sqlite3"), repository)
    )


class _InvalidEventPagePort(_AlphaHttpPort):
    def list_alpha_events(
        self,
        *,
        after_cursor: int,
        limit: int,
    ) -> AlphaEventPageResponse:
        return AlphaEventPageResponse(
            after_cursor=after_cursor,
            limit=limit,
            scanned_events=0,
            events=(),
            next_cursor=after_cursor + 1,
            has_more=False,
        )


def _client(
    service: _AlphaHttpPort,
    *,
    authority: AlphaWebTicketAuthority,
    limiter: AlphaWebConnectionLimiter | None = None,
    scopes: tuple[ServiceScope, ...] = (ServiceScope.READ, ServiceScope.RUN),
    web_poll_seconds: float = 0.25,
) -> TestClient[Any]:
    principal = ServicePrincipal("client:test", scopes)
    app = create_http_app(
        cast(Any, service),
        authenticator=BearerAuthenticator(SecretValue(_TOKEN), principal),
        authorizer=ScopeAuthorizer(),
        web_ticket_authority=authority,
        web_connection_limiter=limiter,
        web_poll_seconds=web_poll_seconds,
    )
    return TestClient(app)


def _seed_run(client: TestClient[Any], repository: Path) -> None:
    responses = (
        client.post("/api/alpha/v1/projects", json=_project_body(repository), headers=_auth()),
        client.post("/api/alpha/v1/intents", json=_intent_body(), headers=_auth()),
        client.post(
            "/api/alpha/v1/plans",
            json=_plan_body(_base_commit(repository)),
            headers=_auth(),
        ),
        client.post("/api/alpha/v1/runs", json=_run_body(), headers=_auth()),
    )
    assert tuple(response.status_code for response in responses) == (201, 201, 201, 202)


def _ticket(client: TestClient[Any]) -> str:
    response = client.post("/api/alpha/v1/ui/socket-tickets", headers=_auth())
    assert response.status_code == 201
    ticket = response.json()["ticket"]
    assert isinstance(ticket, str)
    return ticket


def _socket_url(ticket: str, *, after: int = 0) -> str:
    return f"/api/alpha/v1/ui/events?ticket={ticket}&after={after}"


def _wait_until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 1.0
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("timed out waiting for websocket cleanup")
        time.sleep(0.01)
