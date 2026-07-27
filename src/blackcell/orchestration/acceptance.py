"""Content-bounded execution acceptance-command contracts.

The command contract names an administrator-owned executable alias rather than a path. Isolation
adapters resolve that alias, enforce host policy, and return content-addressed stream evidence.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from blackcell.kernel import JsonInput
from blackcell.kernel._json import bytes_digest, json_digest

ACCEPTANCE_COMMAND_SCHEMA = "blackcell.acceptance-command/v1"
ACCEPTANCE_RESULT_SCHEMA = "blackcell.acceptance-result/v1"
ACCEPTANCE_STREAM_SCHEMA = "blackcell.acceptance-stream/v1"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
_EXECUTABLE_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_ARGV = 32
_MAX_ARG_CHARS = 2_048
MAX_ACCEPTANCE_TIMEOUT_SECONDS = 600
MAX_ACCEPTANCE_STREAM_BYTES = 16 * 1024 * 1024


class AcceptanceFailureCode(StrEnum):
    """Stable infrastructure failures that never contain command or host data."""

    INVALID_COMMAND = "invalid-execution-acceptance-command"
    INVALID_POLICY = "invalid-execution-acceptance-policy"
    UNSUPPORTED_PLATFORM = "execution-acceptance-platform-unsupported"
    ISOLATION_UNAVAILABLE = "execution-acceptance-isolation-unavailable"
    EXECUTABLE_NOT_ALLOWED = "execution-acceptance-executable-not-allowed"
    WORKTREE_UNAVAILABLE = "execution-acceptance-worktree-unavailable"
    WORKTREE_POLICY_VIOLATION = "execution-acceptance-worktree-policy-violation"
    WORKTREE_CHANGED = "execution-acceptance-worktree-changed"
    SPAWN_FAILED = "execution-acceptance-spawn-failed"
    CANCELED = "execution-acceptance-canceled"
    TIMED_OUT = "execution-acceptance-timed-out"
    OUTPUT_TOO_LARGE = "execution-acceptance-output-too-large"
    OUTPUT_INCOMPLETE = "execution-acceptance-output-incomplete"


class AcceptanceError(RuntimeError):
    """A content-free execution acceptance infrastructure failure."""

    def __init__(self, code: AcceptanceFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


def is_acceptance_check_id(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def is_acceptance_executable_alias(value: object) -> bool:
    return isinstance(value, str) and _EXECUTABLE_ALIAS.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class AcceptanceCommand:
    check_id: str
    argv: tuple[str, ...] = field(repr=False)
    expected_exit_code: int
    timeout_seconds: float
    stdout_limit_bytes: int
    stderr_limit_bytes: int
    schema_version: Literal["blackcell.acceptance-command/v1"] = ACCEPTANCE_COMMAND_SCHEMA

    def __post_init__(self) -> None:
        if (
            self.schema_version != ACCEPTANCE_COMMAND_SCHEMA
            or not is_acceptance_check_id(self.check_id)
            or not isinstance(self.argv, tuple)
            or not 1 <= len(self.argv) <= _MAX_ARGV
            or not is_acceptance_executable_alias(self.argv[0])
        ):
            raise AcceptanceError(AcceptanceFailureCode.INVALID_COMMAND)
        for token in self.argv:
            if (
                not isinstance(token, str)
                or not token
                or len(token) > _MAX_ARG_CHARS
                or "\x00" in token
            ):
                raise AcceptanceError(AcceptanceFailureCode.INVALID_COMMAND)
        if (
            isinstance(self.expected_exit_code, bool)
            or not isinstance(self.expected_exit_code, int)
            or not 0 <= self.expected_exit_code <= 255
            or isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= MAX_ACCEPTANCE_TIMEOUT_SECONDS
        ):
            raise AcceptanceError(AcceptanceFailureCode.INVALID_COMMAND)
        for limit in (self.stdout_limit_bytes, self.stderr_limit_bytes):
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 1 <= limit <= MAX_ACCEPTANCE_STREAM_BYTES
            ):
                raise AcceptanceError(AcceptanceFailureCode.INVALID_COMMAND)

    @property
    def digest(self) -> str:
        return json_digest(acceptance_command_payload(self))


@dataclass(frozen=True, slots=True)
class AcceptanceStream:
    captured: bytes = field(repr=False)
    size_bytes: int = field(init=False)
    digest: str = field(init=False)
    schema_version: Literal["blackcell.acceptance-stream/v1"] = ACCEPTANCE_STREAM_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != ACCEPTANCE_STREAM_SCHEMA or not isinstance(self.captured, bytes):
            raise ValueError("invalid execution acceptance stream")
        object.__setattr__(self, "size_bytes", len(self.captured))
        object.__setattr__(self, "digest", bytes_digest(self.captured))


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    check_id: str
    command_digest: str
    worktree_spec_digest: str
    isolation_policy_digest: str
    inspection_before_digest: str
    inspection_after_digest: str
    return_code: int
    expected_exit_code: int
    passed: bool
    stdout: AcceptanceStream
    stderr: AcceptanceStream
    schema_version: Literal["blackcell.acceptance-result/v1"] = ACCEPTANCE_RESULT_SCHEMA

    def __post_init__(self) -> None:
        if (
            self.schema_version != ACCEPTANCE_RESULT_SCHEMA
            or not isinstance(self.check_id, str)
            or _IDENTIFIER.fullmatch(self.check_id) is None
            or any(
                not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None
                for digest in (
                    self.command_digest,
                    self.worktree_spec_digest,
                    self.isolation_policy_digest,
                    self.inspection_before_digest,
                    self.inspection_after_digest,
                )
            )
            or isinstance(self.return_code, bool)
            or not isinstance(self.return_code, int)
            or not 0 <= self.return_code <= 255
            or isinstance(self.expected_exit_code, bool)
            or not isinstance(self.expected_exit_code, int)
            or not 0 <= self.expected_exit_code <= 255
            or not isinstance(self.passed, bool)
            or self.passed != (self.return_code == self.expected_exit_code)
            or not isinstance(self.stdout, AcceptanceStream)
            or not isinstance(self.stderr, AcceptanceStream)
        ):
            raise ValueError("invalid execution acceptance result")

    @property
    def digest(self) -> str:
        return json_digest(acceptance_result_payload(self))


def acceptance_command_payload(command: AcceptanceCommand) -> dict[str, JsonInput]:
    if not isinstance(command, AcceptanceCommand):
        raise AcceptanceError(AcceptanceFailureCode.INVALID_COMMAND)
    return {
        "schema_version": command.schema_version,
        "check_id": command.check_id,
        "argv": list(command.argv),
        "expected_exit_code": command.expected_exit_code,
        "timeout_seconds": command.timeout_seconds,
        "stdout_limit_bytes": command.stdout_limit_bytes,
        "stderr_limit_bytes": command.stderr_limit_bytes,
    }


def acceptance_result_payload(result: AcceptanceResult) -> dict[str, JsonInput]:
    if not isinstance(result, AcceptanceResult):
        raise ValueError("invalid execution acceptance result")
    return {
        "schema_version": result.schema_version,
        "check_id": result.check_id,
        "command_digest": result.command_digest,
        "worktree_spec_digest": result.worktree_spec_digest,
        "isolation_policy_digest": result.isolation_policy_digest,
        "inspection_before_digest": result.inspection_before_digest,
        "inspection_after_digest": result.inspection_after_digest,
        "return_code": result.return_code,
        "expected_exit_code": result.expected_exit_code,
        "passed": result.passed,
        "stdout": {
            "schema_version": result.stdout.schema_version,
            "size_bytes": result.stdout.size_bytes,
            "digest": result.stdout.digest,
        },
        "stderr": {
            "schema_version": result.stderr.schema_version,
            "size_bytes": result.stderr.size_bytes,
            "digest": result.stderr.digest,
        },
    }


__all__ = [
    "ACCEPTANCE_COMMAND_SCHEMA",
    "ACCEPTANCE_RESULT_SCHEMA",
    "ACCEPTANCE_STREAM_SCHEMA",
    "MAX_ACCEPTANCE_STREAM_BYTES",
    "MAX_ACCEPTANCE_TIMEOUT_SECONDS",
    "AcceptanceCommand",
    "AcceptanceError",
    "AcceptanceFailureCode",
    "AcceptanceResult",
    "AcceptanceStream",
    "acceptance_command_payload",
    "acceptance_result_payload",
    "is_acceptance_check_id",
    "is_acceptance_executable_alias",
]
