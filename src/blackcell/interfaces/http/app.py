from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import msgspec
from litestar import Litestar, Request, Response, get, post
from litestar.concurrency import sync_to_thread
from litestar.connection import ASGIConnection
from litestar.exceptions import HTTPException
from litestar.handlers import BaseRouteHandler
from litestar.params import FromPath
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_405_METHOD_NOT_ALLOWED,
    HTTP_409_CONFLICT,
    HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_500_INTERNAL_SERVER_ERROR,
    HTTP_503_SERVICE_UNAVAILABLE,
    HTTP_507_INSUFFICIENT_STORAGE,
)

from blackcell.interfaces import (
    AuthenticationError,
    AuthorizationError,
    BearerAuthenticator,
    ScopeAuthorizer,
    ServicePrincipal,
    ServiceScope,
)
from blackcell.interfaces.http.contracts import (
    MAX_EVENT_PAGE_SIZE,
    MAX_REQUEST_BODY_BYTES,
    ApprovalRequest,
    ErrorResponse,
    HealthResponse,
    ObservationIngestRequest,
    RunSubmissionRequest,
    WireContractError,
    decode_contract,
    encode_contract,
)
from blackcell.interfaces.http.ports import (
    RuntimeApiError,
    RuntimeApiFailureCode,
    RuntimeApiPort,
)
from blackcell.interfaces.http.quota import RequestQuotaPort

_PRINCIPAL_STATE_KEY = "blackcell.service_principal"
_MAX_PATH_ID_CHARS = 200


class HttpBoundaryError(RuntimeError):
    def __init__(self, code: str, status_code: int) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def create_http_app(
    service: RuntimeApiPort,
    *,
    authenticator: BearerAuthenticator,
    authorizer: ScopeAuthorizer,
    request_quota: RequestQuotaPort | None = None,
) -> Litestar:
    """Create the versioned HTTP edge over one injected runtime application port."""

    read_guard = _scope_guard(authenticator, authorizer, ServiceScope.READ, request_quota)
    run_guard = _scope_guard(authenticator, authorizer, ServiceScope.RUN, request_quota)
    approve_guard = _scope_guard(authenticator, authorizer, ServiceScope.APPROVE, request_quota)

    @get("/health/live", status_code=HTTP_200_OK, sync_to_thread=False)
    def liveness() -> Response[bytes]:
        return _json_response(HealthResponse(status="live"))

    @get("/health/ready", sync_to_thread=True)
    def readiness() -> Response[bytes]:
        response = service.readiness()
        status = HTTP_200_OK if response.status == "ready" else HTTP_503_SERVICE_UNAVAILABLE
        return _json_response(response, status_code=status)

    @post(
        "/api/v1/observations",
        guards=[run_guard],
        status_code=HTTP_201_CREATED,
    )
    async def ingest_observations(request: Request[Any, Any, Any]) -> Response[bytes]:
        contract = await _request_contract(request, ObservationIngestRequest)
        principal_id = _principal(request).principal_id
        response = await sync_to_thread(
            _invoke,
            lambda: service.ingest_observations(contract, principal_id=principal_id),
        )
        return _json_response(response, status_code=HTTP_201_CREATED)

    @post("/api/v1/runs", guards=[run_guard], status_code=HTTP_201_CREATED)
    async def submit_run(request: Request[Any, Any, Any]) -> Response[bytes]:
        contract = await _request_contract(request, RunSubmissionRequest)
        principal_id = _principal(request).principal_id
        response = await sync_to_thread(
            _invoke,
            lambda: service.submit_run(contract, principal_id=principal_id),
        )
        return _json_response(response, status_code=HTTP_201_CREATED)

    @get("/api/v1/runs/{run_id:str}", guards=[read_guard], sync_to_thread=True)
    def inspect_run(run_id: FromPath[str]) -> Response[bytes]:
        return _json_response(_invoke(lambda: service.inspect_run(_path_id(run_id))))

    @get(
        "/api/v1/runs/{run_id:str}/context",
        guards=[read_guard],
        sync_to_thread=True,
    )
    def inspect_context(run_id: FromPath[str]) -> Response[bytes]:
        return _json_response(_invoke(lambda: service.inspect_context(_path_id(run_id))))

    @get(
        "/api/v1/runs/{run_id:str}/replay",
        guards=[read_guard],
        sync_to_thread=True,
    )
    def replay_run(run_id: FromPath[str]) -> Response[bytes]:
        return _json_response(_invoke(lambda: service.replay_run(_path_id(run_id))))

    @get(
        "/api/v1/runs/{run_id:str}/evaluation",
        guards=[read_guard],
        sync_to_thread=True,
    )
    def inspect_evaluation(run_id: FromPath[str]) -> Response[bytes]:
        return _json_response(_invoke(lambda: service.inspect_evaluation(_path_id(run_id))))

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
            maximum=MAX_EVENT_PAGE_SIZE,
        )
        return _json_response(
            _invoke(lambda: service.list_events(after_position=after, limit=limit))
        )

    @get(
        "/api/v1/orchestration/runs/{run_id:str}",
        guards=[read_guard],
        sync_to_thread=True,
    )
    def inspect_orchestration(run_id: FromPath[str]) -> Response[bytes]:
        return _json_response(_invoke(lambda: service.inspect_orchestration(_path_id(run_id))))

    @post(
        "/api/v1/orchestration/runs/{run_id:str}/nodes/{node_id:str}/approvals",
        guards=[approve_guard],
    )
    async def record_approval(
        request: Request[Any, Any, Any],
        run_id: FromPath[str],
        node_id: FromPath[str],
    ) -> Response[bytes]:
        contract = await _request_contract(request, ApprovalRequest)
        principal_id = _principal(request).principal_id
        response = await sync_to_thread(
            _invoke,
            lambda: service.record_orchestration_approval(
                _path_id(run_id), _path_id(node_id), contract, principal_id=principal_id
            ),
        )
        return _json_response(response)

    return Litestar(
        route_handlers=[
            liveness,
            readiness,
            ingest_observations,
            submit_run,
            inspect_run,
            inspect_context,
            replay_run,
            inspect_evaluation,
            list_events,
            inspect_orchestration,
            record_approval,
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
