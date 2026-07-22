from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

ALPHA_TUI_CURSOR_SCHEMA = "blackcell.alpha-tui-cursor/v1"
_MAX_ENDPOINT_CHARS = 2_048
_ENDPOINT_ID = re.compile(r"[0-9a-f]{64}\Z")
_EVENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")


class AlphaTuiCursorFailureCode(StrEnum):
    INVALID_ENDPOINT = "alpha-tui-cursor-invalid-endpoint"
    INVALID_CHECKPOINT = "alpha-tui-cursor-invalid-checkpoint"
    ENDPOINT_MISMATCH = "alpha-tui-cursor-endpoint-mismatch"
    CURSOR_REGRESSION = "alpha-tui-cursor-regression"
    UNSAFE_STATE_DIRECTORY = "alpha-tui-cursor-unsafe-state-directory"
    UNSAFE_STATE_FILE = "alpha-tui-cursor-unsafe-state-file"
    IO_FAILED = "alpha-tui-cursor-io-failed"


class AlphaTuiCursorError(RuntimeError):
    """A content-free projection-checkpoint failure."""

    def __init__(self, code: AlphaTuiCursorFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class AlphaTuiCursorWitness:
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
            raise AlphaTuiCursorError(AlphaTuiCursorFailureCode.INVALID_CHECKPOINT)


@dataclass(frozen=True, slots=True)
class AlphaTuiCursorCheckpoint:
    endpoint_id: str
    cursor: int
    witness: AlphaTuiCursorWitness | None
    schema_version: Literal["blackcell.alpha-tui-cursor/v1"] = ALPHA_TUI_CURSOR_SCHEMA

    def __post_init__(self) -> None:
        if (
            self.schema_version != ALPHA_TUI_CURSOR_SCHEMA
            or not isinstance(self.endpoint_id, str)
            or _ENDPOINT_ID.fullmatch(self.endpoint_id) is None
            or isinstance(self.cursor, bool)
            or not isinstance(self.cursor, int)
            or not 0 <= self.cursor <= 2**63 - 1
            or (self.cursor == 0 and self.witness is not None)
            or (self.witness is not None and self.witness.cursor > self.cursor)
        ):
            raise AlphaTuiCursorError(AlphaTuiCursorFailureCode.INVALID_CHECKPOINT)


class AlphaTuiCursorStore(Protocol):
    def load(self, endpoint_id: str) -> AlphaTuiCursorCheckpoint: ...

    def save(self, checkpoint: AlphaTuiCursorCheckpoint) -> None: ...


def alpha_tui_endpoint_id(endpoint: str) -> str:
    if (
        not isinstance(endpoint, str)
        or not endpoint
        or len(endpoint) > _MAX_ENDPOINT_CHARS
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in endpoint)
    ):
        raise AlphaTuiCursorError(AlphaTuiCursorFailureCode.INVALID_ENDPOINT)
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


__all__ = [
    "ALPHA_TUI_CURSOR_SCHEMA",
    "AlphaTuiCursorCheckpoint",
    "AlphaTuiCursorError",
    "AlphaTuiCursorFailureCode",
    "AlphaTuiCursorStore",
    "AlphaTuiCursorWitness",
    "alpha_tui_endpoint_id",
]
