from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from contextlib import suppress
from itertools import pairwise
from typing import Any, cast
from urllib.parse import parse_qsl

import msgspec
from litestar import Litestar, Request, Response, WebSocket, get, post, websocket
from litestar.concurrency import sync_to_thread
from litestar.connection import ASGIConnection
from litestar.exceptions import HTTPException, WebSocketDisconnect
from litestar.handlers import BaseRouteHandler
from litestar.params import FromPath
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_202_ACCEPTED,
    HTTP_204_NO_CONTENT,
    HTTP_304_NOT_MODIFIED,
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_405_METHOD_NOT_ALLOWED,
    HTTP_406_NOT_ACCEPTABLE,
    HTTP_409_CONFLICT,
    HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    HTTP_422_UNPROCESSABLE_ENTITY,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_500_INTERNAL_SERVER_ERROR,
    HTTP_503_SERVICE_UNAVAILABLE,
    HTTP_507_INSUFFICIENT_STORAGE,
)
from litestar.types import ASGIApp, HTTPResponseBodyEvent, Receive, Scope, Send

from blackcell.gateway import ToolingSurfaceCatalog
from blackcell.interfaces import (
    AuthenticationError,
    AuthorizationError,
    BearerAuthenticator,
    ScopeAuthorizer,
    ServicePrincipal,
    ServiceScope,
)
from blackcell.interfaces.http.contracts import (
    MAX_REQUEST_BODY_BYTES,
    MAX_RUNTIME_EVENT_PAGE_SIZE,
    RUN_QUERY_MEDIA_TYPE,
    RUN_QUERY_RESULT_MEDIA_TYPE,
    CancelRunRequest,
    ErrorResponse,
    HealthResponse,
    IntentRequest,
    PlanRequest,
    ProjectRequest,
    RunQueryRequest,
    RunRequest,
    RuntimeEventPageResponse,
    WireContractError,
    decode_contract,
    encode_contract,
)
from blackcell.interfaces.http.ports import (
    RuntimeApiError,
    RuntimeApiFailureCode,
    RuntimeApiPort,
    RuntimeArtifactPayload,
)
from blackcell.interfaces.http.quota import RequestQuotaPort
from blackcell.interfaces.http.web import (
    WebConnectionLimiter,
    WebTicketAuthority,
    WebTicketError,
    WebTicketFailureCode,
)
from blackcell.interfaces.http.web_assets import load_web_assets
from blackcell.interfaces.presentation import (
    PresentationSurface,
    canonical_surface_bytes,
    run_surface,
    workspace_surface,
)

_PRINCIPAL_STATE_KEY = "blackcell.service_principal"
_MAX_PATH_ID_CHARS = 200
_MAX_WEB_SOCKET_QUERY_BYTES = 512
_WEB_EVENT_PAGE_LIMIT = 100
_DEFAULT_WEB_POLL_SECONDS = 0.25
_RUN_QUERY_PATH = "/api/v1/runs"
_RUN_QUERY_ALLOW = "HEAD, OPTIONS, QUERY"
PRESENTATION_MEDIA_TYPE = "application/vnd.blackcell.presentation+json"
_WS_INVALID_REQUEST = 4400
_WS_AUTHENTICATION_REQUIRED = 4401
_WS_CAPACITY_EXCEEDED = 4429
_WEB_ASSET_HEADERS = {
    "cache-control": "no-store",
    "content-security-policy": (
        "default-src 'none'; base-uri 'none'; connect-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; script-src 'self'; style-src 'self'"
    ),
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
}


class HttpBoundaryError(RuntimeError):
    def __init__(self, code: str, status_code: int) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class _QuerySyntaxError(ValueError):
    pass


class _RunQueryMiddleware:
    """Narrow ASGI adapter for RFC 10008 while Litestar lacks QUERY routing support."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        service: RuntimeApiPort,
        authenticator: BearerAuthenticator,
        authorizer: ScopeAuthorizer,
        request_quota: RequestQuotaPort | None,
    ) -> None:
        self._app = app
        self._service = service
        self._authenticator = authenticator
        self._authorizer = authorizer
        self._request_quota = request_quota

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != _RUN_QUERY_PATH:
            await self._app(scope, receive, send)
            return
        method = scope.get("method", "").upper()
        if method not in {"HEAD", "OPTIONS", "QUERY"}:
            await self._app(scope, receive, send)
            return
        if method == "OPTIONS":
            await _send_query_response(send, status=HTTP_204_NO_CONTENT)
            return
        if method == "HEAD":
            await _send_query_response(
                send,
                status=HTTP_200_OK,
                content_type=RUN_QUERY_RESULT_MEDIA_TYPE,
            )
            return
        try:
            _authorize_query(
                scope,
                authenticator=self._authenticator,
                authorizer=self._authorizer,
                request_quota=self._request_quota,
            )
            content_type_values = _header_values(scope, b"content-type")
            if not content_type_values:
                await _send_query_error(
                    send,
                    HTTP_400_BAD_REQUEST,
                    "query-content-type-required",
                )
                return
            if len(content_type_values) != 1 or not _supported_query_content_type(
                content_type_values[0]
            ):
                await _send_query_error(
                    send,
                    HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    "unsupported-query-media-type",
                )
                return
            content_encoding = _header_values(scope, b"content-encoding")
            if len(content_encoding) > 1 or (
                content_encoding and content_encoding[0].strip().casefold() != "identity"
            ):
                await _send_query_error(
                    send,
                    HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    "unsupported-query-content-encoding",
                )
                return
            if not _query_response_acceptable(_header_values(scope, b"accept")):
                await _send_query_error(send, HTTP_406_NOT_ACCEPTABLE, "not-acceptable")
                return
            body = await _read_query_body(scope, receive)
            try:
                decoded = _decode_query_json(body)
            except _QuerySyntaxError:
                await _send_query_error(
                    send,
                    HTTP_400_BAD_REQUEST,
                    "invalid-query-content",
                )
                return
            try:
                contract = msgspec.convert(decoded, type=RunQueryRequest, strict=True)
            except msgspec.ValidationError, TypeError, ValueError:
                await _send_query_error(
                    send,
                    HTTP_422_UNPROCESSABLE_ENTITY,
                    "unprocessable-query",
                )
                return
            response = await sync_to_thread(self._service.query_runs, contract)
            content = encode_contract(response)
            etag = f'"{hashlib.sha256(content).hexdigest()}"'
            if _etag_matches(_header_values(scope, b"if-none-match"), etag):
                await _send_query_response(
                    send,
                    status=HTTP_304_NOT_MODIFIED,
                    etag=etag,
                )
                return
            await _send_query_response(
                send,
                status=HTTP_200_OK,
                body=content,
                content_type=RUN_QUERY_RESULT_MEDIA_TYPE,
                etag=etag,
            )
        except AuthenticationError:
            await _send_query_error(
                send,
                HTTP_401_UNAUTHORIZED,
                "authentication-required",
                authenticate=True,
            )
        except AuthorizationError:
            await _send_query_error(send, HTTP_403_FORBIDDEN, "insufficient-scope")
        except HttpBoundaryError as error:
            await _send_query_error(
                send,
                error.status_code,
                error.code,
                authenticate=error.status_code == HTTP_401_UNAUTHORIZED,
            )
        except RuntimeApiError as error:
            statuses = {
                RuntimeApiFailureCode.INVALID_REQUEST: HTTP_400_BAD_REQUEST,
                RuntimeApiFailureCode.NOT_FOUND: HTTP_404_NOT_FOUND,
                RuntimeApiFailureCode.CONFLICT: HTTP_409_CONFLICT,
                RuntimeApiFailureCode.NOT_READY: HTTP_503_SERVICE_UNAVAILABLE,
                RuntimeApiFailureCode.STORAGE_QUOTA_EXCEEDED: HTTP_507_INSUFFICIENT_STORAGE,
            }
            await _send_query_error(send, statuses[error.code], error.code.value)
        except Exception:
            await _send_query_error(send, HTTP_500_INTERNAL_SERVER_ERROR, "internal-error")


def _authorize_query(
    scope: Scope,
    *,
    authenticator: BearerAuthenticator,
    authorizer: ScopeAuthorizer,
    request_quota: RequestQuotaPort | None,
) -> None:
    if request_quota is not None and not request_quota.consume():
        raise HttpBoundaryError("request-quota-exceeded", HTTP_429_TOO_MANY_REQUESTS)
    principal = authenticator.authenticate(_header_values(scope, b"authorization"))
    authorizer.require(principal, ServiceScope.READ)


def _header_values(scope: Scope, name: bytes) -> tuple[str, ...]:
    headers = scope.get("headers", ())
    return tuple(
        value.decode("latin-1") for candidate, value in headers if candidate.lower() == name
    )


def _supported_query_content_type(value: str) -> bool:
    pieces = tuple(piece.strip() for piece in value.split(";"))
    if not pieces or pieces[0].casefold() != RUN_QUERY_MEDIA_TYPE:
        return False
    for parameter in pieces[1:]:
        name, separator, raw_value = parameter.partition("=")
        if (
            not separator
            or name.strip().casefold() != "charset"
            or raw_value.strip().strip('"').casefold() != "utf-8"
        ):
            return False
    return True


def _query_response_acceptable(values: tuple[str, ...]) -> bool:
    if not values:
        return True
    combined = ",".join(values)
    if len(combined) > 8_192:
        return False
    offered_type, offered_subtype = RUN_QUERY_RESULT_MEDIA_TYPE.split("/", 1)
    for item in combined.split(","):
        fields = tuple(field.strip() for field in item.split(";"))
        media_range = fields[0].casefold()
        quality = 1.0
        valid = bool(media_range)
        for parameter in fields[1:]:
            name, separator, value = parameter.partition("=")
            if name.strip().casefold() != "q" or not separator:
                valid = False
                break
            try:
                quality = float(value)
            except ValueError:
                valid = False
                break
            if not 0.0 <= quality <= 1.0:
                valid = False
                break
        if not valid or quality <= 0.0:
            continue
        if media_range in {
            "*/*",
            f"{offered_type}/*",
            f"{offered_type}/{offered_subtype}",
        }:
            return True
    return False


async def _read_query_body(scope: Scope, receive: Receive) -> bytes:
    lengths = _header_values(scope, b"content-length")
    if len(lengths) > 1 or (
        lengths and (not lengths[0].isdecimal() or int(lengths[0]) > MAX_REQUEST_BODY_BYTES)
    ):
        status = (
            HTTP_413_REQUEST_ENTITY_TOO_LARGE
            if lengths and lengths[0].isdecimal()
            else HTTP_400_BAD_REQUEST
        )
        raise HttpBoundaryError(
            "request-too-large"
            if status == HTTP_413_REQUEST_ENTITY_TOO_LARGE
            else "invalid-request",
            status,
        )
    body = bytearray()
    while True:
        event = await receive()
        if event["type"] == "http.disconnect":
            raise HttpBoundaryError("invalid-request", HTTP_400_BAD_REQUEST)
        chunk = event.get("body", b"")
        if not isinstance(chunk, bytes):
            raise HttpBoundaryError("invalid-request", HTTP_400_BAD_REQUEST)
        body.extend(chunk)
        if len(body) > MAX_REQUEST_BODY_BYTES:
            raise HttpBoundaryError("request-too-large", HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        if not event.get("more_body", False):
            return bytes(body)


def _decode_query_json(body: bytes) -> object:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise _QuerySyntaxError()
            value[key] = item
        return value

    def reject_constant(_: str) -> object:
        raise _QuerySyntaxError()

    try:
        return json.loads(
            body.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _QuerySyntaxError) as error:
        raise _QuerySyntaxError() from error


def _etag_matches(values: tuple[str, ...], etag: str) -> bool:
    if not values:
        return False
    opaque = etag.removeprefix("W/")
    for item in ",".join(values).split(","):
        candidate = item.strip()
        if candidate == "*" or candidate.removeprefix("W/") == opaque:
            return True
    return False


async def _send_query_error(
    send: Send,
    status: int,
    code: str,
    *,
    authenticate: bool = False,
) -> None:
    await _send_query_response(
        send,
        status=status,
        body=encode_contract(ErrorResponse(error=code)),
        content_type="application/json",
        cache_control="no-store",
        authenticate=authenticate,
    )


async def _send_query_response(
    send: Send,
    *,
    status: int,
    body: bytes = b"",
    content_type: str | None = None,
    etag: str | None = None,
    cache_control: str = "private, max-age=0, must-revalidate",
    authenticate: bool = False,
) -> None:
    headers: list[tuple[bytes, bytes]] = [
        (b"accept-query", RUN_QUERY_MEDIA_TYPE.encode("ascii")),
        (b"allow", _RUN_QUERY_ALLOW.encode("ascii")),
        (b"cache-control", cache_control.encode("ascii")),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"x-content-type-options", b"nosniff"),
    ]
    if content_type is not None:
        headers.append((b"content-type", content_type.encode("ascii")))
    if etag is not None:
        headers.append((b"etag", etag.encode("ascii")))
    if authenticate:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    response_body: HTTPResponseBodyEvent = {
        "type": "http.response.body",
        "body": body,
        "more_body": False,
    }
    await send(response_body)


def create_http_app(
    service: RuntimeApiPort,
    *,
    authenticator: BearerAuthenticator,
    authorizer: ScopeAuthorizer,
    request_quota: RequestQuotaPort | None = None,
    web_ticket_authority: WebTicketAuthority | None = None,
    web_connection_limiter: WebConnectionLimiter | None = None,
    web_poll_seconds: float = _DEFAULT_WEB_POLL_SECONDS,
    tooling_catalog: ToolingSurfaceCatalog | None = None,
) -> Litestar:
    """Create the HTTP edge over one injected runtime application port."""
    if (
        isinstance(web_poll_seconds, bool)
        or not isinstance(web_poll_seconds, int | float)
        or not 0.05 <= web_poll_seconds <= 5.0
    ):
        raise ValueError("web_poll_seconds must be between 0.05 and 5.0")
    ticket_authority = web_ticket_authority or WebTicketAuthority()
    connection_limiter = web_connection_limiter or WebConnectionLimiter()
    web_assets = load_web_assets()
    read_guard = _scope_guard(authenticator, authorizer, ServiceScope.READ, request_quota)
    run_guard = _scope_guard(authenticator, authorizer, ServiceScope.RUN, request_quota)

    @get("/health/live", status_code=HTTP_200_OK, sync_to_thread=False)
    def liveness() -> Response[bytes]:
        return _json_response(HealthResponse(status="live"))

    @get("/health/ready", sync_to_thread=True)
    def readiness() -> Response[bytes]:
        response = service.readiness()
        status = HTTP_200_OK if response.status == "ready" else HTTP_503_SERVICE_UNAVAILABLE
        return _json_response(response, status_code=status)

    @get(["/ui", "/ui/"], sync_to_thread=False)
    def web_ui() -> Response[bytes]:
        return _web_asset_response(web_assets.html, media_type="text/html")

    @get("/ui/assets/app.css", sync_to_thread=False)
    def web_css() -> Response[bytes]:
        return _web_asset_response(web_assets.css, media_type="text/css")

    @get("/ui/assets/app.js", sync_to_thread=False)
    def web_javascript() -> Response[bytes]:
        return _web_asset_response(web_assets.javascript, media_type="application/javascript")

    @get("/ui/assets/runtime-client.js", sync_to_thread=False)
    def web_runtime_client() -> Response[bytes]:
        return _web_asset_response(
            web_assets.runtime_client_javascript,
            media_type="application/javascript",
        )

    @get("/ui/assets/surface-elements.js", sync_to_thread=False)
    def web_surface_elements() -> Response[bytes]:
        return _web_asset_response(
            web_assets.surface_elements_javascript,
            media_type="application/javascript",
        )

    @get("/ui/assets/tokens.json", sync_to_thread=False)
    def web_tokens() -> Response[bytes]:
        return _web_asset_response(web_assets.tokens, media_type="application/json")

    @get(
        "/api/v1/ui/surfaces/workspace",
        guards=[read_guard],
        sync_to_thread=True,
    )
    def project_workspace(request: Request[Any, Any, Any]) -> Response[bytes]:
        runs = _invoke(lambda: service.presentation_run_window(limit=50))
        return _presentation_response(
            request,
            workspace_surface(runs, tooling=tooling_catalog),
        )

    @get(
        "/api/v1/ui/surfaces/runs/{run_id:str}",
        guards=[read_guard],
        sync_to_thread=True,
    )
    def project_run(
        run_id: FromPath[str],
        request: Request[Any, Any, Any],
    ) -> Response[bytes]:
        selected_run_id = _path_id(run_id)
        snapshot = _invoke(lambda: service.presentation_run_snapshot(selected_run_id))
        return _presentation_response(request, run_surface(snapshot))

    @get(
        "/api/v1/runs/{run_id:str}/artifacts/{digest:str}",
        guards=[read_guard],
        sync_to_thread=True,
    )
    def read_run_artifact(
        run_id: FromPath[str],
        digest: FromPath[str],
        request: Request[Any, Any, Any],
    ) -> Response[bytes]:
        payload = _invoke(lambda: service.read_run_artifact(_path_id(run_id), _path_id(digest)))
        return _artifact_response(request, payload)

    @post(
        "/api/v1/projects",
        guards=[run_guard],
        status_code=HTTP_201_CREATED,
    )
    async def register_project(request: Request[Any, Any, Any]) -> Response[bytes]:
        contract = await _request_contract(request, ProjectRequest)
        principal_id = _principal(request).principal_id
        response = await sync_to_thread(
            _invoke,
            lambda: service.register_project(contract, principal_id=principal_id),
        )
        return _json_response(response, status_code=HTTP_201_CREATED)

    @post(
        "/api/v1/intents",
        guards=[run_guard],
        status_code=HTTP_201_CREATED,
    )
    async def accept_intent(request: Request[Any, Any, Any]) -> Response[bytes]:
        contract = await _request_contract(request, IntentRequest)
        principal_id = _principal(request).principal_id
        response = await sync_to_thread(
            _invoke,
            lambda: service.accept_intent(contract, principal_id=principal_id),
        )
        return _json_response(response, status_code=HTTP_201_CREATED)

    @post(
        "/api/v1/plans",
        guards=[run_guard],
        status_code=HTTP_201_CREATED,
    )
    async def accept_plan(request: Request[Any, Any, Any]) -> Response[bytes]:
        contract = await _request_contract(request, PlanRequest)
        principal_id = _principal(request).principal_id
        response = await sync_to_thread(
            _invoke,
            lambda: service.accept_plan(contract, principal_id=principal_id),
        )
        return _json_response(response, status_code=HTTP_201_CREATED)

    @post(
        "/api/v1/runs",
        guards=[run_guard],
        status_code=HTTP_202_ACCEPTED,
    )
    async def submit_run(request: Request[Any, Any, Any]) -> Response[bytes]:
        contract = await _request_contract(request, RunRequest)
        principal_id = _principal(request).principal_id
        response = await sync_to_thread(
            _invoke,
            lambda: service.submit_run(contract, principal_id=principal_id),
        )
        return _json_response(response, status_code=HTTP_202_ACCEPTED)

    @post(
        "/api/v1/runs/{run_id:str}/cancel",
        guards=[run_guard],
        status_code=HTTP_202_ACCEPTED,
    )
    async def cancel_run(run_id: FromPath[str], request: Request[Any, Any, Any]) -> Response[bytes]:
        contract = await _request_contract(request, CancelRunRequest)
        principal_id = _principal(request).principal_id
        response = await sync_to_thread(
            _invoke,
            lambda: service.cancel_run(_path_id(run_id), contract, principal_id=principal_id),
        )
        return _json_response(response, status_code=HTTP_202_ACCEPTED)

    @get(
        "/api/v1/runs/{run_id:str}/status",
        guards=[read_guard],
        sync_to_thread=True,
    )
    def inspect_run(run_id: FromPath[str]) -> Response[bytes]:
        return _json_response(_invoke(lambda: service.inspect_run(_path_id(run_id))))

    @get(
        "/api/v1/runs/{run_id:str}/replay",
        guards=[read_guard],
        sync_to_thread=True,
    )
    def replay_run(run_id: FromPath[str]) -> Response[bytes]:
        return _json_response(_invoke(lambda: service.replay_run(_path_id(run_id))))

    @get("/api/v1/events", guards=[read_guard], sync_to_thread=True)
    def list_events(request: Request[Any, Any, Any]) -> Response[bytes]:
        after = _query_integer(
            request,
            "after",
            default=0,
            minimum=0,
            maximum=2**63 - 1,
        )
        limit = _query_integer(
            request,
            "limit",
            default=100,
            minimum=1,
            maximum=MAX_RUNTIME_EVENT_PAGE_SIZE,
        )
        return _json_response(_invoke(lambda: service.list_events(after_cursor=after, limit=limit)))

    @post(
        "/api/v1/ui/socket-tickets",
        guards=[read_guard],
        status_code=HTTP_201_CREATED,
        sync_to_thread=True,
    )
    def issue_web_socket_ticket(request: Request[Any, Any, Any]) -> Response[bytes]:
        try:
            issued = ticket_authority.issue(_principal(request))
        except WebTicketError as error:
            status = (
                HTTP_429_TOO_MANY_REQUESTS
                if error.code is WebTicketFailureCode.CAPACITY_EXCEEDED
                else HTTP_500_INTERNAL_SERVER_ERROR
            )
            raise HttpBoundaryError(error.code.value, status) from error
        return _json_response(issued.response(), status_code=HTTP_201_CREATED)

    @websocket("/api/v1/ui/events")
    async def stream_web_events(socket: WebSocket[Any, Any, Any]) -> None:
        query = _websocket_query(socket)
        if query is None:
            await socket.close(code=_WS_INVALID_REQUEST, reason="invalid-request")
            return
        ticket, after_cursor = query
        try:
            ticket_authority.consume(ticket)
        except WebTicketError:
            await socket.close(
                code=_WS_AUTHENTICATION_REQUIRED,
                reason="authentication-required",
            )
            return
        if not connection_limiter.acquire():
            await socket.close(code=_WS_CAPACITY_EXCEEDED, reason="connection-capacity-exceeded")
            return
        receiver: asyncio.Task[str] | None = None
        try:
            await socket.accept()
            receiver = asyncio.create_task(socket.receive_text())
            cursor = after_cursor
            first_page = True
            while True:
                try:
                    page = await sync_to_thread(
                        service.list_events,
                        after_cursor=cursor,
                        limit=_WEB_EVENT_PAGE_LIMIT,
                    )
                except Exception:
                    await socket.close(code=1011, reason="event-source-failed")
                    return
                if not _valid_web_event_page(page, after_cursor=cursor):
                    await socket.close(code=1011, reason="event-source-failed")
                    return
                if first_page or page.next_cursor != cursor or page.events:
                    await socket.send_bytes(encode_contract(page))
                cursor = page.next_cursor
                first_page = False
                if page.has_more:
                    continue
                done, _ = await asyncio.wait({receiver}, timeout=float(web_poll_seconds))
                if receiver in done:
                    try:
                        receiver.result()
                    except WebSocketDisconnect:
                        return
                    except Exception:
                        await socket.close(code=1011, reason="connection-receive-failed")
                        return
                    await socket.close(code=_WS_INVALID_REQUEST, reason="read-only-channel")
                    return
        except WebSocketDisconnect:
            return
        finally:
            if receiver is not None:
                if not receiver.done():
                    receiver.cancel()
                with suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await receiver
            connection_limiter.release()

    application = Litestar(
        route_handlers=[
            liveness,
            readiness,
            web_ui,
            web_css,
            web_javascript,
            web_runtime_client,
            web_surface_elements,
            web_tokens,
            project_workspace,
            project_run,
            read_run_artifact,
            register_project,
            accept_intent,
            accept_plan,
            submit_run,
            cancel_run,
            inspect_run,
            replay_run,
            list_events,
            issue_web_socket_ticket,
            stream_web_events,
        ],
        debug=False,
        openapi_config=None,
        request_max_body_size=MAX_REQUEST_BODY_BYTES,
        exception_handlers={
            AuthenticationError: _exception_response,
            AuthorizationError: _exception_response,
            HttpBoundaryError: _exception_response,
            HTTPException: _exception_response,
            Exception: _exception_response,
        },
    )
    application.asgi_handler = _RunQueryMiddleware(
        application.asgi_handler,
        service=service,
        authenticator=authenticator,
        authorizer=authorizer,
        request_quota=request_quota,
    )
    return application


def _scope_guard(
    authenticator: BearerAuthenticator,
    authorizer: ScopeAuthorizer,
    required_scope: ServiceScope,
    request_quota: RequestQuotaPort | None,
) -> Callable[[ASGIConnection[Any, Any, Any, Any], BaseRouteHandler], None]:
    def guard(
        connection: ASGIConnection[Any, Any, Any, Any],
        _: BaseRouteHandler,
    ) -> None:
        if request_quota is not None and not request_quota.consume():
            raise HttpBoundaryError("request-quota-exceeded", HTTP_429_TOO_MANY_REQUESTS)
        headers = tuple(
            value.decode("latin-1")
            for name, value in connection.scope.get("headers", ())
            if name.lower() == b"authorization"
        )
        principal = authenticator.authenticate(headers)
        authorizer.require(principal, required_scope)
        state = cast(dict[str, object], connection.scope.setdefault("state", {}))
        state[_PRINCIPAL_STATE_KEY] = principal

    return guard


async def _request_contract[ResponseT: msgspec.Struct](
    request: Request[Any, Any, Any],
    contract_type: type[ResponseT],
) -> ResponseT:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().casefold()
    if content_type != "application/json":
        raise HttpBoundaryError("unsupported-media-type", HTTP_415_UNSUPPORTED_MEDIA_TYPE)
    try:
        return decode_contract(await request.body(), contract_type)
    except WireContractError as error:
        raise HttpBoundaryError(error.code, HTTP_400_BAD_REQUEST) from error


def _principal(request: Request[Any, Any, Any]) -> ServicePrincipal:
    state = cast(dict[str, object], request.scope.get("state", {}))
    principal = state.get(_PRINCIPAL_STATE_KEY)
    if not isinstance(principal, ServicePrincipal):
        raise HttpBoundaryError("authentication-required", HTTP_401_UNAUTHORIZED)
    return principal


def _invoke[ResponseT: msgspec.Struct](operation: Callable[[], ResponseT]) -> ResponseT:
    try:
        return operation()
    except RuntimeApiError as error:
        statuses = {
            RuntimeApiFailureCode.INVALID_REQUEST: HTTP_400_BAD_REQUEST,
            RuntimeApiFailureCode.NOT_FOUND: HTTP_404_NOT_FOUND,
            RuntimeApiFailureCode.CONFLICT: HTTP_409_CONFLICT,
            RuntimeApiFailureCode.NOT_READY: HTTP_503_SERVICE_UNAVAILABLE,
            RuntimeApiFailureCode.STORAGE_QUOTA_EXCEEDED: HTTP_507_INSUFFICIENT_STORAGE,
        }
        raise HttpBoundaryError(error.code.value, statuses[error.code]) from error


def _path_id(value: str) -> str:
    if (
        not value
        or len(value) > _MAX_PATH_ID_CHARS
        or any(ord(character) < 0x21 or ord(character) == 0x7F for character in value)
    ):
        raise HttpBoundaryError("invalid-request", HTTP_400_BAD_REQUEST)
    return value


def _query_integer(
    request: Request[Any, Any, Any],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    if not raw.isdecimal():
        raise HttpBoundaryError("invalid-request", HTTP_400_BAD_REQUEST)
    value = int(raw)
    if not minimum <= value <= maximum:
        raise HttpBoundaryError("invalid-request", HTTP_400_BAD_REQUEST)
    return value


def _websocket_query(socket: WebSocket[Any, Any, Any]) -> tuple[str, int] | None:
    raw = socket.scope.get("query_string", b"")
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= _MAX_WEB_SOCKET_QUERY_BYTES:
        return None
    try:
        values = parse_qsl(
            raw.decode("ascii"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=2,
        )
    except UnicodeDecodeError, ValueError:
        return None
    if len(values) != 2 or {name for name, _ in values} != {"ticket", "after"}:
        return None
    by_name = dict(values)
    ticket = by_name["ticket"]
    cursor_text = by_name["after"]
    if not cursor_text.isdecimal():
        return None
    cursor = int(cursor_text)
    if not 0 <= cursor <= 2**63 - 1:
        return None
    return ticket, cursor


def _valid_web_event_page(page: object, *, after_cursor: int) -> bool:
    if not isinstance(page, RuntimeEventPageResponse):
        return False
    cursors = tuple(event.cursor for event in page.events)
    return not (
        page.after_cursor != after_cursor
        or page.limit != _WEB_EVENT_PAGE_LIMIT
        or isinstance(page.scanned_events, bool)
        or not isinstance(page.scanned_events, int)
        or not 0 <= page.scanned_events <= _WEB_EVENT_PAGE_LIMIT
        or len(page.events) > page.scanned_events
        or isinstance(page.next_cursor, bool)
        or not isinstance(page.next_cursor, int)
        or not after_cursor <= page.next_cursor <= 2**63 - 1
        or not isinstance(page.has_more, bool)
        or (page.scanned_events == 0 and page.next_cursor != after_cursor)
        or (page.scanned_events > 0 and page.next_cursor <= after_cursor)
        or (page.scanned_events < _WEB_EVENT_PAGE_LIMIT and page.has_more)
        or (page.has_more and page.next_cursor == after_cursor)
        or any(
            isinstance(cursor, bool)
            or not isinstance(cursor, int)
            or not after_cursor < cursor <= page.next_cursor
            for cursor in cursors
        )
        or any(previous >= current for previous, current in pairwise(cursors))
        or len({event.event_id for event in page.events}) != len(page.events)
    )


def _json_response(
    value: msgspec.Struct,
    *,
    status_code: int = HTTP_200_OK,
    headers: dict[str, str] | None = None,
) -> Response[bytes]:
    response_headers = {
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        **(headers or {}),
    }
    return Response(
        content=encode_contract(value),
        media_type="application/json",
        status_code=status_code,
        headers=response_headers,
    )


def _presentation_response(
    request: Request[Any, Any, Any],
    surface: PresentationSurface,
) -> Response[bytes]:
    content = canonical_surface_bytes(surface)
    etag = f'"{hashlib.sha256(content).hexdigest()}"'
    headers = {
        "cache-control": "private, max-age=0, must-revalidate",
        "etag": etag,
        "x-content-type-options": "nosniff",
    }
    if _etag_matches((request.headers.get("if-none-match", ""),), etag):
        return Response(content=b"", status_code=HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(
        content=content,
        media_type=PRESENTATION_MEDIA_TYPE,
        status_code=HTTP_200_OK,
        headers=headers,
    )


def _artifact_response(
    request: Request[Any, Any, Any],
    payload: RuntimeArtifactPayload,
) -> Response[bytes]:
    etag = f'"{payload.digest.removeprefix("sha256:")}"'
    inline_media = {
        "application/json",
        "text/markdown",
        "text/plain",
    }
    media_type = (
        payload.media_type if payload.media_type in inline_media else "application/octet-stream"
    )
    disposition = "inline" if payload.media_type in inline_media else "attachment"
    filename = payload.digest.removeprefix("sha256:")
    headers = {
        "cache-control": "private, max-age=0, must-revalidate",
        "content-disposition": f'{disposition}; filename="{filename}"',
        "etag": etag,
        "x-content-type-options": "nosniff",
    }
    if _etag_matches((request.headers.get("if-none-match", ""),), etag):
        return Response(content=b"", status_code=HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(
        content=payload.content,
        media_type=media_type,
        status_code=HTTP_200_OK,
        headers=headers,
    )


def _web_asset_response(content: bytes, *, media_type: str) -> Response[bytes]:
    return Response(
        content=content,
        media_type=media_type,
        status_code=HTTP_200_OK,
        headers=dict(_WEB_ASSET_HEADERS),
    )


def _exception_response(
    _: Request[Any, Any, Any],
    error: Exception,
) -> Response[bytes]:
    if isinstance(error, AuthenticationError):
        return _json_response(
            ErrorResponse(error="authentication-required"),
            status_code=HTTP_401_UNAUTHORIZED,
            headers={"www-authenticate": "Bearer"},
        )
    if isinstance(error, AuthorizationError):
        return _json_response(
            ErrorResponse(error="insufficient-scope"),
            status_code=HTTP_403_FORBIDDEN,
        )
    if isinstance(error, HttpBoundaryError):
        headers = {"www-authenticate": "Bearer"} if error.status_code == 401 else None
        return _json_response(
            ErrorResponse(error=error.code),
            status_code=error.status_code,
            headers=headers,
        )
    if isinstance(error, HTTPException):
        codes = {
            HTTP_400_BAD_REQUEST: "invalid-request",
            HTTP_401_UNAUTHORIZED: "authentication-required",
            HTTP_403_FORBIDDEN: "insufficient-scope",
            HTTP_404_NOT_FOUND: "not-found",
            HTTP_405_METHOD_NOT_ALLOWED: "method-not-allowed",
            HTTP_413_REQUEST_ENTITY_TOO_LARGE: "request-too-large",
            HTTP_415_UNSUPPORTED_MEDIA_TYPE: "unsupported-media-type",
            HTTP_429_TOO_MANY_REQUESTS: "request-quota-exceeded",
        }
        if error.status_code >= HTTP_500_INTERNAL_SERVER_ERROR:
            return _json_response(
                ErrorResponse(error="internal-error"),
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            )
        status = error.status_code if error.status_code in codes else HTTP_400_BAD_REQUEST
        headers = {"www-authenticate": "Bearer"} if status == HTTP_401_UNAUTHORIZED else None
        return _json_response(
            ErrorResponse(error=codes.get(status, "invalid-request")),
            status_code=status,
            headers=headers,
        )
    return _json_response(
        ErrorResponse(error="internal-error"),
        status_code=HTTP_500_INTERNAL_SERVER_ERROR,
    )


__all__ = ["create_http_app"]
