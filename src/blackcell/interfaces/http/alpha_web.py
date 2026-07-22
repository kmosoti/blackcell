from __future__ import annotations

import hashlib
import math
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from blackcell.interfaces.auth import ServicePrincipal
from blackcell.interfaces.http.contracts import StrictStruct

ALPHA_WEB_SOCKET_PATH = "/api/alpha/v1/ui/events"
_MAX_TICKET_CHARS = 128
_TICKET = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")


class AlphaWebTicketFailureCode(StrEnum):
    INVALID_CONFIG = "alpha-web-ticket-invalid-config"
    GENERATION_FAILED = "alpha-web-ticket-generation-failed"
    CAPACITY_EXCEEDED = "alpha-web-ticket-capacity-exceeded"
    INVALID_TICKET = "alpha-web-ticket-invalid"


class AlphaWebTicketError(RuntimeError):
    """A content-free browser-ticket failure."""

    def __init__(self, code: AlphaWebTicketFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class AlphaWebSocketTicketResponse(StrictStruct, frozen=True):
    ticket: str
    expires_in_seconds: int
    websocket_path: Literal["/api/alpha/v1/ui/events"] = ALPHA_WEB_SOCKET_PATH
    schema_version: Literal["alpha-web-socket-ticket/v1"] = "alpha-web-socket-ticket/v1"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.ticket, str)
            or _TICKET.fullmatch(self.ticket) is None
            or isinstance(self.expires_in_seconds, bool)
            or not isinstance(self.expires_in_seconds, int)
            or not 1 <= self.expires_in_seconds <= 60
        ):
            raise ValueError("invalid alpha web ticket response")

    def __repr__(self) -> str:
        return (
            "AlphaWebSocketTicketResponse(ticket=[REDACTED], "
            f"expires_in_seconds={self.expires_in_seconds}, "
            f"websocket_path={self.websocket_path!r}, schema_version={self.schema_version!r})"
        )


@dataclass(frozen=True, slots=True)
class IssuedAlphaWebTicket:
    _value: str = field(repr=False)
    expires_in_seconds: int

    def response(self) -> AlphaWebSocketTicketResponse:
        return AlphaWebSocketTicketResponse(
            ticket=self._value,
            expires_in_seconds=self.expires_in_seconds,
        )


@dataclass(frozen=True, slots=True)
class _PendingTicket:
    principal: ServicePrincipal
    expires_at: float


class AlphaWebTicketAuthority:
    """Issue one-use browser handshake credentials while retaining only their digests."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 20,
        max_pending: int = 64,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not 1 <= ttl_seconds <= 60
            or isinstance(max_pending, bool)
            or not isinstance(max_pending, int)
            or not 1 <= max_pending <= 1_024
            or not callable(clock)
            or (token_factory is not None and not callable(token_factory))
        ):
            raise AlphaWebTicketError(AlphaWebTicketFailureCode.INVALID_CONFIG)
        self._ttl_seconds = ttl_seconds
        self._max_pending = max_pending
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._pending: dict[bytes, _PendingTicket] = {}
        self._lock = threading.Lock()

    @property
    def pending_count(self) -> int:
        with self._lock:
            self._prune(self._now())
            return len(self._pending)

    def issue(self, principal: ServicePrincipal) -> IssuedAlphaWebTicket:
        if (
            not isinstance(principal, ServicePrincipal)
            or len(principal.principal_id) > 200
            or len(principal.scopes) > 4
        ):
            raise AlphaWebTicketError(AlphaWebTicketFailureCode.GENERATION_FAILED)
        now = self._now()
        with self._lock:
            self._prune(now)
            if len(self._pending) >= self._max_pending:
                raise AlphaWebTicketError(AlphaWebTicketFailureCode.CAPACITY_EXCEEDED)
            for _ in range(4):
                try:
                    value = self._token_factory()
                except Exception as error:
                    raise AlphaWebTicketError(
                        AlphaWebTicketFailureCode.GENERATION_FAILED
                    ) from error
                if not isinstance(value, str) or _TICKET.fullmatch(value) is None:
                    raise AlphaWebTicketError(AlphaWebTicketFailureCode.GENERATION_FAILED)
                digest = _ticket_digest(value)
                if digest not in self._pending:
                    self._pending[digest] = _PendingTicket(
                        principal=principal,
                        expires_at=now + self._ttl_seconds,
                    )
                    return IssuedAlphaWebTicket(value, self._ttl_seconds)
        raise AlphaWebTicketError(AlphaWebTicketFailureCode.GENERATION_FAILED)

    def consume(self, value: str) -> ServicePrincipal:
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= _MAX_TICKET_CHARS
            or _TICKET.fullmatch(value) is None
        ):
            raise AlphaWebTicketError(AlphaWebTicketFailureCode.INVALID_TICKET)
        now = self._now()
        digest = _ticket_digest(value)
        with self._lock:
            self._prune(now)
            pending = self._pending.pop(digest, None)
        if pending is None or pending.expires_at <= now:
            raise AlphaWebTicketError(AlphaWebTicketFailureCode.INVALID_TICKET)
        return pending.principal

    def _now(self) -> float:
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
        ):
            raise AlphaWebTicketError(AlphaWebTicketFailureCode.GENERATION_FAILED)
        return float(value)

    def _prune(self, now: float) -> None:
        expired = [digest for digest, ticket in self._pending.items() if ticket.expires_at <= now]
        for digest in expired:
            del self._pending[digest]


class AlphaWebConnectionLimiter:
    def __init__(self, max_connections: int = 16) -> None:
        if (
            isinstance(max_connections, bool)
            or not isinstance(max_connections, int)
            or not 1 <= max_connections <= 256
        ):
            raise AlphaWebTicketError(AlphaWebTicketFailureCode.INVALID_CONFIG)
        self._maximum = max_connections
        self._active = 0
        self._lock = threading.Lock()

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def acquire(self) -> bool:
        with self._lock:
            if self._active >= self._maximum:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._active < 1:
                raise RuntimeError("alpha web connection limiter underflow")
            self._active -= 1


def _ticket_digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("ascii")).digest()


__all__ = [
    "ALPHA_WEB_SOCKET_PATH",
    "AlphaWebConnectionLimiter",
    "AlphaWebSocketTicketResponse",
    "AlphaWebTicketAuthority",
    "AlphaWebTicketError",
    "AlphaWebTicketFailureCode",
    "IssuedAlphaWebTicket",
]
