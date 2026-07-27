from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

TUI_CURSOR_SCHEMA = "blackcell.tui-cursor/v1"
_MAX_ENDPOINT_CHARS = 2_048
_ENDPOINT_ID = re.compile(r"[0-9a-f]{64}\Z")
_EVENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")


class TuiCursorFailureCode(StrEnum):
    INVALID_ENDPOINT = "tui-cursor-invalid-endpoint"
    INVALID_CHECKPOINT = "tui-cursor-invalid-checkpoint"
    ENDPOINT_MISMATCH = "tui-cursor-endpoint-mismatch"
    CURSOR_REGRESSION = "tui-cursor-regression"
    UNSAFE_STATE_DIRECTORY = "tui-cursor-unsafe-state-directory"
    UNSAFE_STATE_FILE = "tui-cursor-unsafe-state-file"
    IO_FAILED = "tui-cursor-io-failed"


class TuiCursorError(RuntimeError):
    """A content-free projection-checkpoint failure."""

    def __init__(self, code: TuiCursorFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class TuiCursorWitness:
    cursor: int
    event_id: str
    payload_digest: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.cursor, bool)
            or not isinstance(self.cursor, int)
            or not 1 <= self.cursor <= 2**63 - 1
            or not isinstance(self.event_id, str)
            or _EVENT_ID.fullmatch(self.event_id) is None
            or not isinstance(self.payload_digest, str)
            or _ENDPOINT_ID.fullmatch(self.payload_digest) is None
        ):
            raise TuiCursorError(TuiCursorFailureCode.INVALID_CHECKPOINT)


@dataclass(frozen=True, slots=True)
class TuiCursorCheckpoint:
    endpoint_id: str
    cursor: int
    witness: TuiCursorWitness | None
    schema_version: Literal["blackcell.tui-cursor/v1"] = TUI_CURSOR_SCHEMA

    def __post_init__(self) -> None:
        if (
            self.schema_version != TUI_CURSOR_SCHEMA
            or not isinstance(self.endpoint_id, str)
            or _ENDPOINT_ID.fullmatch(self.endpoint_id) is None
            or isinstance(self.cursor, bool)
            or not isinstance(self.cursor, int)
            or not 0 <= self.cursor <= 2**63 - 1
            or (self.cursor == 0 and self.witness is not None)
            or (self.witness is not None and self.witness.cursor > self.cursor)
        ):
            raise TuiCursorError(TuiCursorFailureCode.INVALID_CHECKPOINT)


class TuiCursorStore(Protocol):
    def load(self, endpoint_id: str) -> TuiCursorCheckpoint: ...

    def save(self, checkpoint: TuiCursorCheckpoint) -> None: ...


def tui_endpoint_id(endpoint: str) -> str:
    if (
        not isinstance(endpoint, str)
        or not endpoint
        or len(endpoint) > _MAX_ENDPOINT_CHARS
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in endpoint)
    ):
        raise TuiCursorError(TuiCursorFailureCode.INVALID_ENDPOINT)
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


__all__ = [
    "TUI_CURSOR_SCHEMA",
    "TuiCursorCheckpoint",
    "TuiCursorError",
    "TuiCursorFailureCode",
    "TuiCursorStore",
    "TuiCursorWitness",
    "tui_endpoint_id",
]
