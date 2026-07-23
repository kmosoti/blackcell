from __future__ import annotations

import ipaddress
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from http.client import HTTPMessage
from typing import IO, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, ProxyHandler, Request, build_opener

from blackcell.config import SecretValue
from blackcell.interfaces.http import (
    MAX_ALPHA_EVENT_PAGE_SIZE,
    MAX_RESPONSE_BODY_BYTES,
    AlphaCancelRunRequest,
    AlphaEventPageResponse,
    AlphaIntentRequest,
    AlphaIntentResponse,
    AlphaPlanRequest,
    AlphaPlanResponse,
    AlphaProjectRequest,
    AlphaProjectResponse,
    AlphaReplayResponse,
    AlphaRunRequest,
    AlphaRunResponse,
    ErrorResponse,
    HealthResponse,
    StrictStruct,
    WireContractError,
    decode_response_contract,
    encode_contract,
)

DEFAULT_RUNTIME_ENDPOINT = "http://127.0.0.1:8080"
RUNTIME_ENDPOINT_ENV = "BLACKCELL_RUNTIME_ENDPOINT"
_DEFAULT_TIMEOUT_SECONDS = 5.0
_MAX_TIMEOUT_SECONDS = 30.0
_DEFAULT_REPLAY_TIMEOUT_SECONDS = 600.0
_MAX_REPLAY_TIMEOUT_SECONDS = 3_600.0
_MAX_ENDPOINT_CHARS = 2_048
_MAX_SERVICE_ERROR_CHARS = 100

HttpMethod = Literal["GET", "POST"]


class RuntimeClientFailureCode(StrEnum):
    INVALID_ENDPOINT = "invalid-runtime-endpoint"
    INVALID_TIMEOUT = "invalid-runtime-timeout"
    INVALID_REQUEST = "invalid-runtime-request"
    MISSING_AUTHENTICATION = "runtime-authentication-required"
    CONNECTION_FAILED = "runtime-connection-failed"
    RESPONSE_TOO_LARGE = "runtime-response-too-large"
    INVALID_RESPONSE = "invalid-runtime-response"
    REQUEST_REJECTED = "runtime-request-rejected"


class RuntimeClientError(RuntimeError):
    """A bounded client failure that never includes response content."""

    def __init__(
        self,
        code: RuntimeClientFailureCode,
        *,
        status_code: int | None = None,
        service_error: str | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.service_error = _safe_service_error(service_error)
        detail = code.value
        if code is RuntimeClientFailureCode.REQUEST_REJECTED and status_code is not None:
            detail = f"{detail}: status={status_code} error={self.service_error}"
        super().__init__(detail)

    @property
    def cli_exit_code(self) -> int:
        if self.code in {
            RuntimeClientFailureCode.INVALID_ENDPOINT,
            RuntimeClientFailureCode.INVALID_TIMEOUT,
            RuntimeClientFailureCode.INVALID_REQUEST,
            RuntimeClientFailureCode.MISSING_AUTHENTICATION,
        }:
            return 2
        if self.code is RuntimeClientFailureCode.CONNECTION_FAILED:
            return 3
        if self.code is RuntimeClientFailureCode.REQUEST_REJECTED:
            return 4
        return 1


@dataclass(frozen=True, slots=True)
class RuntimeHttpResponse:
    status_code: int
    content_type: str
    body: bytes = field(repr=False)


class RuntimeHttpTransport(Protocol):
    def request(
        self,
        method: HttpMethod,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> RuntimeHttpResponse: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request | None:
        del req, fp, code, msg, headers, newurl
        return None


@dataclass(slots=True)
class UrllibRuntimeTransport:
    """Direct stdlib HTTP transport with redirects and ambient proxies disabled."""

    _opener: OpenerDirector = field(
        default_factory=lambda: build_opener(ProxyHandler({}), _NoRedirectHandler()),
        repr=False,
    )

    def request(
        self,
        method: HttpMethod,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> RuntimeHttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            response = self._opener.open(request, timeout=timeout_seconds)
        except HTTPError as error:
            try:
                try:
                    return _read_response(
                        status_code=error.code,
                        content_type=error.headers.get("content-type", ""),
                        response=error,
                    )
                except OSError as read_error:
                    raise RuntimeClientError(
                        RuntimeClientFailureCode.CONNECTION_FAILED
                    ) from read_error
            finally:
                error.close()
        except (OSError, TimeoutError, URLError) as error:
            raise RuntimeClientError(RuntimeClientFailureCode.CONNECTION_FAILED) from error
        try:
            return _read_response(
                status_code=response.status,
                content_type=response.headers.get("content-type", ""),
                response=response,
            )
        except OSError as error:
            raise RuntimeClientError(RuntimeClientFailureCode.CONNECTION_FAILED) from error
        finally:
            response.close()


@dataclass(frozen=True, slots=True)
class RuntimeServiceStatus:
    endpoint: str
    live: bool
    ready: bool
    schema_version: Literal["runtime-service-status/v1"] = "runtime-service-status/v1"


@dataclass(frozen=True, slots=True)
class RuntimeHttpClient:
    endpoint: str = DEFAULT_RUNTIME_ENDPOINT
    transport: RuntimeHttpTransport = field(
        default_factory=UrllibRuntimeTransport,
        repr=False,
        compare=False,
    )
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    token: SecretValue | None = field(default=None, repr=False, compare=False)
    replay_timeout_seconds: float = _DEFAULT_REPLAY_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoint", _normalize_endpoint(self.endpoint))
        if not _valid_timeout(
            self.timeout_seconds,
            maximum=_MAX_TIMEOUT_SECONDS,
        ) or not _valid_timeout(
            self.replay_timeout_seconds,
            maximum=_MAX_REPLAY_TIMEOUT_SECONDS,
        ):
            raise RuntimeClientError(RuntimeClientFailureCode.INVALID_TIMEOUT)

    def status(self) -> RuntimeServiceStatus:
        live_response = self._request("/health/live")
        live = _decode_expected(live_response, (200,), HealthResponse)
        if live.status != "live":
            raise RuntimeClientError(RuntimeClientFailureCode.INVALID_RESPONSE)

        ready_response = self._request("/health/ready")
        ready = _decode_expected(ready_response, (200, 503), HealthResponse)
        if (ready_response.status_code, ready.status) not in {
            (200, "ready"),
            (503, "not-ready"),
        }:
            raise RuntimeClientError(RuntimeClientFailureCode.INVALID_RESPONSE)
        return RuntimeServiceStatus(
            endpoint=self.endpoint,
            live=True,
            ready=ready.status == "ready",
        )

    def register_alpha_project(self, request: AlphaProjectRequest) -> AlphaProjectResponse:
        return self._alpha_request(
            "POST",
            "/api/alpha/v1/projects",
            (201,),
            AlphaProjectResponse,
            request,
        )

    def accept_alpha_intent(self, request: AlphaIntentRequest) -> AlphaIntentResponse:
        return self._alpha_request(
            "POST",
            "/api/alpha/v1/intents",
            (201,),
            AlphaIntentResponse,
            request,
        )

    def accept_alpha_plan(self, request: AlphaPlanRequest) -> AlphaPlanResponse:
        return self._alpha_request(
            "POST",
            "/api/alpha/v1/plans",
            (201,),
            AlphaPlanResponse,
            request,
        )

    def submit_alpha_run(self, request: AlphaRunRequest) -> AlphaRunResponse:
        return self._alpha_request(
            "POST",
            "/api/alpha/v1/runs",
            (202,),
            AlphaRunResponse,
            request,
        )

    def inspect_alpha_run(self, run_id: str) -> AlphaRunResponse:
        return self._alpha_request(
            "GET",
            f"/api/alpha/v1/runs/{_path_identifier(run_id)}/status",
            (200,),
            AlphaRunResponse,
        )

    def cancel_alpha_run(
        self,
        run_id: str,
        request: AlphaCancelRunRequest,
    ) -> AlphaRunResponse:
        return self._alpha_request(
            "POST",
            f"/api/alpha/v1/runs/{_path_identifier(run_id)}/cancel",
            (202,),
            AlphaRunResponse,
            request,
        )

    def replay_alpha_run(self, run_id: str) -> AlphaReplayResponse:
        return self._alpha_request(
            "GET",
            f"/api/alpha/v1/runs/{_path_identifier(run_id)}/replay",
            (200,),
            AlphaReplayResponse,
            timeout_seconds=self.replay_timeout_seconds,
        )

    def list_alpha_events(
        self,
        *,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> AlphaEventPageResponse:
        if (
            isinstance(after_cursor, bool)
            or not isinstance(after_cursor, int)
            or not 0 <= after_cursor <= 2**63 - 1
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_ALPHA_EVENT_PAGE_SIZE
        ):
            raise RuntimeClientError(RuntimeClientFailureCode.INVALID_REQUEST)
        query = urlencode({"after": after_cursor, "limit": limit})
        return self._alpha_request(
            "GET",
            f"/api/alpha/v1/events?{query}",
            (200,),
            AlphaEventPageResponse,
        )

    def _alpha_request[ResponseT: StrictStruct](
        self,
        method: HttpMethod,
        path: str,
        expected_statuses: tuple[int, ...],
        response_type: type[ResponseT],
        request: StrictStruct | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> ResponseT:
        if self.token is None:
            raise RuntimeClientError(RuntimeClientFailureCode.MISSING_AUTHENTICATION)
        headers = {
            "accept": "application/json",
            "authorization": self.token.authorization_header(),
        }
        body = None
        if request is not None:
            headers["content-type"] = "application/json"
            body = encode_contract(request)
        return _decode_expected(
            self._request(
                path,
                method=method,
                headers=headers,
                body=body,
                timeout_seconds=timeout_seconds,
            ),
            expected_statuses,
            response_type,
        )

    def _request(
        self,
        path: str,
        *,
        method: HttpMethod = "GET",
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout_seconds: float | None = None,
    ) -> RuntimeHttpResponse:
        return self.transport.request(
            method,
            f"{self.endpoint}{path}",
            headers={"accept": "application/json"} if headers is None else headers,
            body=body,
            timeout_seconds=float(
                self.timeout_seconds if timeout_seconds is None else timeout_seconds
            ),
        )


def _read_response(
    *,
    status_code: int,
    content_type: str,
    response: object,
) -> RuntimeHttpResponse:
    read = getattr(response, "read", None)
    if not callable(read):
        raise RuntimeClientError(RuntimeClientFailureCode.INVALID_RESPONSE)
    body = read(MAX_RESPONSE_BODY_BYTES + 1)
    if not isinstance(body, bytes):
        raise RuntimeClientError(RuntimeClientFailureCode.INVALID_RESPONSE)
    if len(body) > MAX_RESPONSE_BODY_BYTES:
        raise RuntimeClientError(RuntimeClientFailureCode.RESPONSE_TOO_LARGE)
    return RuntimeHttpResponse(
        status_code=status_code,
        content_type=content_type,
        body=body,
    )


def _decode_expected[ContractT: StrictStruct](
    response: RuntimeHttpResponse,
    expected_statuses: tuple[int, ...],
    contract_type: type[ContractT],
) -> ContractT:
    if response.status_code not in expected_statuses:
        raise RuntimeClientError(
            RuntimeClientFailureCode.REQUEST_REJECTED,
            status_code=response.status_code,
            service_error=_decode_service_error(response),
        )
    if _media_type(response.content_type) != "application/json":
        raise RuntimeClientError(RuntimeClientFailureCode.INVALID_RESPONSE)
    try:
        return decode_response_contract(response.body, contract_type)
    except WireContractError as error:
        raise RuntimeClientError(RuntimeClientFailureCode.INVALID_RESPONSE) from error


def _decode_service_error(response: RuntimeHttpResponse) -> str | None:
    if _media_type(response.content_type) != "application/json":
        return None
    try:
        return decode_response_contract(response.body, ErrorResponse).error
    except WireContractError:
        return None


def _normalize_endpoint(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_ENDPOINT_CHARS
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise RuntimeClientError(RuntimeClientFailureCode.INVALID_ENDPOINT)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise RuntimeClientError(RuntimeClientFailureCode.INVALID_ENDPOINT) from error
    scheme = parsed.scheme.casefold()
    if (
        scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65_535)
    ):
        raise RuntimeClientError(RuntimeClientFailureCode.INVALID_ENDPOINT)
    if scheme == "http" and not _is_loopback(parsed.hostname):
        raise RuntimeClientError(RuntimeClientFailureCode.INVALID_ENDPOINT)
    return urlunsplit((scheme, parsed.netloc, "", "", ""))


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _valid_timeout(value: object, *, maximum: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(value)
        and 0 < value <= maximum
    )


def _media_type(value: str) -> str:
    return value.partition(";")[0].strip().casefold()


def _path_identifier(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 120
        or any(
            not (character.isascii() and (character.isalnum() or character in "-._"))
            for character in value
        )
    ):
        raise RuntimeClientError(RuntimeClientFailureCode.INVALID_REQUEST)
    return quote(value, safe="-._")


def _safe_service_error(value: str | None) -> str:
    if (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_SERVICE_ERROR_CHARS
        and all(
            character.isascii() and (character.isalnum() or character in "-._")
            for character in value
        )
    ):
        return value
    return "unknown-error"


__all__ = [
    "DEFAULT_RUNTIME_ENDPOINT",
    "RUNTIME_ENDPOINT_ENV",
    "RuntimeClientError",
    "RuntimeClientFailureCode",
    "RuntimeHttpClient",
    "RuntimeHttpResponse",
    "RuntimeHttpTransport",
    "RuntimeServiceStatus",
    "UrllibRuntimeTransport",
]
