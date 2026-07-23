"""Closed, owner-only runtime configuration for the alpha review worker."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from blackcell.config.alpha_worker import AlphaProviderRuntimeConfig
from blackcell.gateway import DataClassification, LocalityPolicy

ALPHA_REVIEW_CONFIG_FILE_ENV = "BLACKCELL_ALPHA_REVIEW_CONFIG_FILE"
ALPHA_REVIEW_CONFIG_SCHEMA = "blackcell.alpha-review-config/v1"

_MAX_CONFIG_BYTES = 64 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_WORKER_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
_ENVIRONMENT_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}\Z")
_ROOT_KEYS = frozenset({"schema_version", "provider", "worker"})
_PROVIDER_KEYS = frozenset(
    {
        "profile_id",
        "model_id",
        "codex_executable",
        "git_executable",
        "classification",
        "locality",
        "max_input_tokens",
        "max_output_tokens",
        "max_cost_microusd",
        "timeout_ceiling_seconds",
        "environment_variables",
    }
)
_WORKER_KEYS = frozenset(
    {
        "worker_id",
        "supervisor_id",
        "lease_seconds",
        "poll_milliseconds",
    }
)


class AlphaReviewConfigFailureCode(StrEnum):
    INVALID = "invalid-alpha-review-config"


class AlphaReviewConfigError(RuntimeError):
    """A content-free review configuration failure."""

    def __init__(self) -> None:
        self.code = AlphaReviewConfigFailureCode.INVALID
        super().__init__(self.code.value)


@dataclass(frozen=True, slots=True)
class AlphaReviewWorkerLoopConfig:
    worker_id: str
    supervisor_id: str
    lease_seconds: int
    poll_milliseconds: int


@dataclass(frozen=True, slots=True)
class AlphaReviewWorkerRuntimeConfig:
    source_path: Path
    provider: AlphaProviderRuntimeConfig
    worker: AlphaReviewWorkerLoopConfig
    schema_version: str = ALPHA_REVIEW_CONFIG_SCHEMA


def load_alpha_review_config(
    environment: Mapping[str, str],
    *,
    repository_root: Path,
    expected_uid: int | None = None,
) -> AlphaReviewWorkerRuntimeConfig | None:
    """Load the optional review config without selecting ambient model authority."""

    value = environment.get(ALPHA_REVIEW_CONFIG_FILE_ENV)
    if value is None:
        return None
    try:
        uid = _current_uid() if expected_uid is None else expected_uid
        source = _config_path(value, repository_root)
        payload = _read_json(source, expected_uid=uid)
        _exact(payload, _ROOT_KEYS)
        if payload.get("schema_version") != ALPHA_REVIEW_CONFIG_SCHEMA:
            raise AlphaReviewConfigError
        provider = _provider(_mapping(payload, "provider"))
        worker = _worker(_mapping(payload, "worker"))
        if worker.lease_seconds <= provider.timeout_ceiling_seconds:
            raise AlphaReviewConfigError
        return AlphaReviewWorkerRuntimeConfig(source, provider, worker)
    except AlphaReviewConfigError:
        raise
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        raise AlphaReviewConfigError from error


def _provider(value: Mapping[str, object]) -> AlphaProviderRuntimeConfig:
    _exact(value, _PROVIDER_KEYS)
    classification_text = _text(value, "classification").upper()
    try:
        classification = DataClassification[classification_text]
    except KeyError as error:
        raise AlphaReviewConfigError from error
    if classification is DataClassification.SECRET:
        raise AlphaReviewConfigError
    try:
        locality = LocalityPolicy(_text(value, "locality"))
    except ValueError as error:
        raise AlphaReviewConfigError from error
    if locality is not LocalityPolicy.REMOTE_ALLOWED:
        raise AlphaReviewConfigError
    environment_value = value.get("environment_variables")
    if (
        not isinstance(environment_value, Sequence)
        or isinstance(environment_value, str | bytes | bytearray)
        or len(environment_value) > 32
    ):
        raise AlphaReviewConfigError
    environment_variables = tuple(sorted(_environment_name(item) for item in environment_value))
    if len(set(environment_variables)) != len(environment_variables):
        raise AlphaReviewConfigError
    return AlphaProviderRuntimeConfig(
        profile_id=_identifier(value, "profile_id"),
        model_id=_token(value, "model_id"),
        codex_executable=_executable(_text(value, "codex_executable")),
        git_executable=_executable(_text(value, "git_executable")),
        classification=classification,
        locality=locality,
        max_input_tokens=_integer(value, "max_input_tokens", 1, 1_000_000),
        max_output_tokens=_integer(value, "max_output_tokens", 1, 1_000_000),
        max_cost_microusd=_integer(value, "max_cost_microusd", 0, 1_000_000_000_000),
        timeout_ceiling_seconds=_integer(value, "timeout_ceiling_seconds", 1, 3_600),
        environment_variables=environment_variables,
    )


def _worker(value: Mapping[str, object]) -> AlphaReviewWorkerLoopConfig:
    _exact(value, _WORKER_KEYS)
    worker_id = _worker_identifier(value, "worker_id")
    supervisor_id = _worker_identifier(value, "supervisor_id")
    if worker_id == supervisor_id:
        raise AlphaReviewConfigError
    return AlphaReviewWorkerLoopConfig(
        worker_id=worker_id,
        supervisor_id=supervisor_id,
        lease_seconds=_integer(value, "lease_seconds", 1, 86_400),
        poll_milliseconds=_integer(value, "poll_milliseconds", 10, 60_000),
    )


def _config_path(value: str, repository_root: Path) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AlphaReviewConfigError
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise AlphaReviewConfigError
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise AlphaReviewConfigError from error
    if resolved != path or resolved.is_relative_to(repository_root):
        raise AlphaReviewConfigError
    return resolved


def _read_json(path: Path, *, expected_uid: int) -> Mapping[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise AlphaReviewConfigError from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > _MAX_CONFIG_BYTES
        ):
            raise AlphaReviewConfigError
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
            raise AlphaReviewConfigError
    except OSError as error:
        raise AlphaReviewConfigError from error
    finally:
        os.close(descriptor)
    try:
        decoded = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise AlphaReviewConfigError from error
    if not isinstance(decoded, Mapping):
        raise AlphaReviewConfigError
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
        raise AlphaReviewConfigError


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value.get(key)
    if not isinstance(item, Mapping) or not all(isinstance(field, str) for field in item):
        raise AlphaReviewConfigError
    return cast("Mapping[str, object]", item)


def _text(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item or "\x00" in item:
        raise AlphaReviewConfigError
    return item


def _identifier(value: Mapping[str, object], key: str) -> str:
    item = _text(value, key)
    if _IDENTIFIER.fullmatch(item) is None:
        raise AlphaReviewConfigError
    return item


def _worker_identifier(value: Mapping[str, object], key: str) -> str:
    item = _text(value, key)
    if _WORKER_IDENTIFIER.fullmatch(item) is None:
        raise AlphaReviewConfigError
    return item


def _token(value: Mapping[str, object], key: str) -> str:
    item = _text(value, key)
    if len(item) > 128 or any(not 0x21 <= ord(character) <= 0x7E for character in item):
        raise AlphaReviewConfigError
    return item


def _environment_name(value: object) -> str:
    if not isinstance(value, str) or _ENVIRONMENT_NAME.fullmatch(value) is None:
        raise AlphaReviewConfigError
    if value.startswith(("BLACKCELL_", "DYLD_", "GIT_", "LD_", "PYTHON")) or value in {
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
    }:
        raise AlphaReviewConfigError
    return value


def _integer(
    value: Mapping[str, object],
    key: str,
    minimum: int,
    maximum: int,
) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
        raise AlphaReviewConfigError
    return item


def _executable(value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AlphaReviewConfigError
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path.is_symlink():
        raise AlphaReviewConfigError
    try:
        resolved = path.resolve(strict=True)
        metadata = path.stat(follow_symlinks=False)
    except (OSError, RuntimeError) as error:
        raise AlphaReviewConfigError from error
    if (
        resolved != path
        or not stat.S_ISREG(metadata.st_mode)
        or not os.access(path, os.X_OK)
        or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | 0o022)
    ):
        raise AlphaReviewConfigError
    return path


def _current_uid() -> int:
    getuid = getattr(os, "geteuid", None)
    if getuid is None:
        raise AlphaReviewConfigError
    return int(getuid())


__all__ = [
    "ALPHA_REVIEW_CONFIG_FILE_ENV",
    "ALPHA_REVIEW_CONFIG_SCHEMA",
    "AlphaReviewConfigError",
    "AlphaReviewConfigFailureCode",
    "AlphaReviewWorkerLoopConfig",
    "AlphaReviewWorkerRuntimeConfig",
    "load_alpha_review_config",
]
