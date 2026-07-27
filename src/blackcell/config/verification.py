"""Closed, owner-only runtime configuration for deterministic execution verification."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

VERIFICATION_CONFIG_FILE_ENV = "BLACKCELL_VERIFICATION_CONFIG_FILE"
VERIFICATION_CONFIG_SCHEMA = "blackcell.verification-config/v1"

_MAX_CONFIG_BYTES = 16 * 1024
_WORKER_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
_ROOT_KEYS = frozenset({"schema_version", "worker"})
_WORKER_KEYS = frozenset(
    {
        "worker_id",
        "supervisor_id",
        "lease_seconds",
        "poll_milliseconds",
    }
)


class VerificationConfigFailureCode(StrEnum):
    INVALID = "invalid-verification-config"


class VerificationConfigError(RuntimeError):
    """A content-free deterministic-verifier configuration failure."""

    def __init__(self) -> None:
        self.code = VerificationConfigFailureCode.INVALID
        super().__init__(self.code.value)


@dataclass(frozen=True, slots=True)
class VerificationWorkerLoopConfig:
    worker_id: str
    supervisor_id: str
    lease_seconds: int
    poll_milliseconds: int


@dataclass(frozen=True, slots=True)
class VerificationWorkerRuntimeConfig:
    source_path: Path
    worker: VerificationWorkerLoopConfig
    schema_version: str = VERIFICATION_CONFIG_SCHEMA


def load_verification_config(
    environment: Mapping[str, str],
    *,
    repository_root: Path,
    expected_uid: int | None = None,
) -> VerificationWorkerRuntimeConfig | None:
    """Load the optional verifier config without inferring ambient authority."""

    value = environment.get(VERIFICATION_CONFIG_FILE_ENV)
    if value is None:
        return None
    try:
        uid = _current_uid() if expected_uid is None else expected_uid
        source = _config_path(value, repository_root)
        payload = _read_json(source, expected_uid=uid)
        _exact(payload, _ROOT_KEYS)
        if payload.get("schema_version") != VERIFICATION_CONFIG_SCHEMA:
            raise VerificationConfigError
        worker = _worker(_mapping(payload, "worker"))
        return VerificationWorkerRuntimeConfig(source, worker)
    except VerificationConfigError:
        raise
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        raise VerificationConfigError from error


def _worker(value: Mapping[str, object]) -> VerificationWorkerLoopConfig:
    _exact(value, _WORKER_KEYS)
    worker_id = _worker_identifier(value, "worker_id")
    supervisor_id = _worker_identifier(value, "supervisor_id")
    if worker_id == supervisor_id:
        raise VerificationConfigError
    return VerificationWorkerLoopConfig(
        worker_id=worker_id,
        supervisor_id=supervisor_id,
        lease_seconds=_integer(value, "lease_seconds", 1, 86_400),
        poll_milliseconds=_integer(value, "poll_milliseconds", 10, 60_000),
    )


def _config_path(value: str, repository_root: Path) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise VerificationConfigError
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise VerificationConfigError
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise VerificationConfigError from error
    if resolved != path or resolved.is_relative_to(repository_root):
        raise VerificationConfigError
    return resolved


def _read_json(path: Path, *, expected_uid: int) -> Mapping[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise VerificationConfigError from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > _MAX_CONFIG_BYTES
        ):
            raise VerificationConfigError
        chunks: list[bytes] = []
        remaining = _MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) != metadata.st_size or len(content) > _MAX_CONFIG_BYTES:
            raise VerificationConfigError
    except OSError as error:
        raise VerificationConfigError from error
    finally:
        os.close(descriptor)
    try:
        decoded = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise VerificationConfigError from error
    if not isinstance(decoded, Mapping):
        raise VerificationConfigError
    return cast("Mapping[str, object]", decoded)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError("duplicate or invalid JSON object key")
        result[key] = value
    return result


def _exact(value: Mapping[str, object], expected: frozenset[str]) -> None:
    if set(value) != expected:
        raise VerificationConfigError


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value.get(key)
    if not isinstance(item, Mapping) or not all(isinstance(field, str) for field in item):
        raise VerificationConfigError
    return cast("Mapping[str, object]", item)


def _text(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item or "\x00" in item:
        raise VerificationConfigError
    return item


def _worker_identifier(value: Mapping[str, object], key: str) -> str:
    item = _text(value, key)
    if _WORKER_IDENTIFIER.fullmatch(item) is None:
        raise VerificationConfigError
    return item


def _integer(
    value: Mapping[str, object],
    key: str,
    minimum: int,
    maximum: int,
) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
        raise VerificationConfigError
    return item


def _current_uid() -> int:
    getuid = getattr(os, "geteuid", None)
    if getuid is None:
        raise VerificationConfigError
    return int(getuid())


__all__ = [
    "VERIFICATION_CONFIG_FILE_ENV",
    "VERIFICATION_CONFIG_SCHEMA",
    "VerificationConfigError",
    "VerificationConfigFailureCode",
    "VerificationWorkerLoopConfig",
    "VerificationWorkerRuntimeConfig",
    "load_verification_config",
]
