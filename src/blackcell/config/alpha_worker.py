"""Closed, owner-only runtime configuration for opt-in alpha dispatch."""

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

from blackcell.gateway import DataClassification, LocalityPolicy

ALPHA_WORKER_CONFIG_FILE_ENV = "BLACKCELL_ALPHA_WORKER_CONFIG_FILE"
ALPHA_WORKER_CONFIG_SCHEMA = "blackcell.alpha-worker-config/v1"

_MAX_CONFIG_BYTES = 64 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")
_WORKER_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
_ENVIRONMENT_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}\Z")
_ROOT_KEYS = frozenset({"schema_version", "provider", "isolation", "worker"})
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
_ISOLATION_KEYS = frozenset(
    {
        "root",
        "executables",
        "runtime_roots",
        "bubblewrap_executable",
        "prlimit_executable",
        "probe_executable",
        "limits",
    }
)
_LIMIT_KEYS = frozenset(
    {
        "address_space_bytes",
        "cpu_seconds",
        "processes",
        "open_files",
        "file_size_bytes",
        "tmpfs_bytes",
    }
)
_WORKER_KEYS = frozenset(
    {
        "worker_id",
        "stdout_limit_bytes",
        "stderr_limit_bytes",
        "lease_grace_seconds",
        "max_retained_successful_worktrees",
    }
)


class AlphaWorkerConfigFailureCode(StrEnum):
    INVALID = "invalid-alpha-worker-config"


class AlphaWorkerConfigError(RuntimeError):
    """A content-free alpha configuration failure."""

    def __init__(self) -> None:
        self.code = AlphaWorkerConfigFailureCode.INVALID
        super().__init__(self.code.value)


@dataclass(frozen=True, slots=True)
class AlphaProviderRuntimeConfig:
    profile_id: str
    model_id: str
    codex_executable: Path
    git_executable: Path
    classification: DataClassification
    locality: LocalityPolicy
    max_input_tokens: int
    max_output_tokens: int
    max_cost_microusd: int
    timeout_ceiling_seconds: int
    environment_variables: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AlphaExecutableAliasConfig:
    alias: str
    path: Path


@dataclass(frozen=True, slots=True)
class AlphaIsolationRuntimeConfig:
    root: Path
    executables: tuple[AlphaExecutableAliasConfig, ...]
    runtime_roots: tuple[Path, ...]
    bubblewrap_executable: Path
    prlimit_executable: Path
    probe_executable: Path
    address_space_limit_bytes: int
    cpu_limit_seconds: int
    process_limit: int
    open_file_limit: int
    file_size_limit_bytes: int
    tmpfs_limit_bytes: int


@dataclass(frozen=True, slots=True)
class AlphaWorkerLoopConfig:
    worker_id: str
    stdout_limit_bytes: int
    stderr_limit_bytes: int
    lease_grace_seconds: int
    max_retained_successful_worktrees: int


@dataclass(frozen=True, slots=True)
class AlphaWorkerRuntimeConfig:
    source_path: Path
    provider: AlphaProviderRuntimeConfig
    isolation: AlphaIsolationRuntimeConfig
    worker: AlphaWorkerLoopConfig
    schema_version: str = ALPHA_WORKER_CONFIG_SCHEMA


def load_alpha_worker_config(
    environment: Mapping[str, str],
    *,
    repository_root: Path,
    data_root: Path,
    expected_uid: int | None = None,
) -> AlphaWorkerRuntimeConfig | None:
    """Load the optional alpha worker file without accepting ambient defaults."""

    value = environment.get(ALPHA_WORKER_CONFIG_FILE_ENV)
    if value is None:
        return None
    try:
        uid = _current_uid() if expected_uid is None else expected_uid
        source = _config_path(value, repository_root)
        payload = _read_json(source, expected_uid=uid)
        _exact(payload, _ROOT_KEYS)
        if payload.get("schema_version") != ALPHA_WORKER_CONFIG_SCHEMA:
            raise AlphaWorkerConfigError
        provider = _provider(_mapping(payload, "provider"))
        isolation = _isolation(
            _mapping(payload, "isolation"),
            repository_root=repository_root,
            data_root=data_root,
            expected_uid=uid,
        )
        worker = _worker(_mapping(payload, "worker"))
        return AlphaWorkerRuntimeConfig(source, provider, isolation, worker)
    except AlphaWorkerConfigError:
        raise
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        raise AlphaWorkerConfigError from error


def _provider(value: Mapping[str, object]) -> AlphaProviderRuntimeConfig:
    _exact(value, _PROVIDER_KEYS)
    classification_text = _text(value, "classification").upper()
    try:
        classification = DataClassification[classification_text]
    except KeyError as error:
        raise AlphaWorkerConfigError from error
    if classification is DataClassification.SECRET:
        raise AlphaWorkerConfigError
    try:
        locality = LocalityPolicy(_text(value, "locality"))
    except ValueError as error:
        raise AlphaWorkerConfigError from error
    if locality is not LocalityPolicy.REMOTE_ALLOWED:
        raise AlphaWorkerConfigError
    environment_value = value.get("environment_variables")
    if (
        not isinstance(environment_value, Sequence)
        or isinstance(environment_value, str | bytes | bytearray)
        or len(environment_value) > 32
    ):
        raise AlphaWorkerConfigError
    environment_variables = tuple(sorted(_environment_name(item) for item in environment_value))
    if len(set(environment_variables)) != len(environment_variables):
        raise AlphaWorkerConfigError
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


def _isolation(
    value: Mapping[str, object],
    *,
    repository_root: Path,
    data_root: Path,
    expected_uid: int,
) -> AlphaIsolationRuntimeConfig:
    _exact(value, _ISOLATION_KEYS)
    root = _owner_directory(_text(value, "root"), expected_uid=expected_uid)
    if _overlap(root, repository_root):
        raise AlphaWorkerConfigError

    aliases_value = value.get("executables")
    if not isinstance(aliases_value, Mapping) or not 1 <= len(aliases_value) <= 64:
        raise AlphaWorkerConfigError
    aliases: list[AlphaExecutableAliasConfig] = []
    for alias, executable in aliases_value.items():
        if (
            not isinstance(alias, str)
            or _ALIAS.fullmatch(alias) is None
            or not isinstance(executable, str)
        ):
            raise AlphaWorkerConfigError
        aliases.append(AlphaExecutableAliasConfig(alias, _executable(executable)))
    aliases.sort(key=lambda item: item.alias)

    runtime_values = value.get("runtime_roots")
    if (
        not isinstance(runtime_values, Sequence)
        or isinstance(runtime_values, str | bytes | bytearray)
        or len(runtime_values) > 32
    ):
        raise AlphaWorkerConfigError
    runtime_roots = tuple(sorted((_directory(item) for item in runtime_values), key=str))
    if len(set(runtime_roots)) != len(runtime_roots) or any(
        _overlap(left, right)
        for index, left in enumerate(runtime_roots)
        for right in runtime_roots[index + 1 :]
    ):
        raise AlphaWorkerConfigError
    protected = (repository_root, data_root, root)
    if any(_overlap(runtime, item) for runtime in runtime_roots for item in protected):
        raise AlphaWorkerConfigError

    limits = _mapping(value, "limits")
    _exact(limits, _LIMIT_KEYS)
    return AlphaIsolationRuntimeConfig(
        root=root,
        executables=tuple(aliases),
        runtime_roots=runtime_roots,
        bubblewrap_executable=_executable(_text(value, "bubblewrap_executable")),
        prlimit_executable=_executable(_text(value, "prlimit_executable")),
        probe_executable=_executable(_text(value, "probe_executable")),
        address_space_limit_bytes=_integer(
            limits,
            "address_space_bytes",
            64 * 1024 * 1024,
            64 * 1024 * 1024 * 1024,
        ),
        cpu_limit_seconds=_integer(limits, "cpu_seconds", 1, 600),
        process_limit=_integer(limits, "processes", 1, 4_096),
        open_file_limit=_integer(limits, "open_files", 16, 4_096),
        file_size_limit_bytes=_integer(
            limits,
            "file_size_bytes",
            1,
            1024 * 1024 * 1024,
        ),
        tmpfs_limit_bytes=_integer(
            limits,
            "tmpfs_bytes",
            1024 * 1024,
            1024 * 1024 * 1024,
        ),
    )


def _worker(value: Mapping[str, object]) -> AlphaWorkerLoopConfig:
    _exact(value, _WORKER_KEYS)
    worker_id = _text(value, "worker_id")
    if _WORKER_IDENTIFIER.fullmatch(worker_id) is None:
        raise AlphaWorkerConfigError
    return AlphaWorkerLoopConfig(
        worker_id=worker_id,
        stdout_limit_bytes=_integer(value, "stdout_limit_bytes", 1, 16 * 1024 * 1024),
        stderr_limit_bytes=_integer(value, "stderr_limit_bytes", 1, 16 * 1024 * 1024),
        lease_grace_seconds=_integer(value, "lease_grace_seconds", 1, 3_600),
        max_retained_successful_worktrees=_integer(
            value,
            "max_retained_successful_worktrees",
            0,
            1_024,
        ),
    )


def _config_path(value: str, repository_root: Path) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AlphaWorkerConfigError
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise AlphaWorkerConfigError
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise AlphaWorkerConfigError from error
    if resolved != path or resolved.is_relative_to(repository_root):
        raise AlphaWorkerConfigError
    return resolved


def _read_json(path: Path, *, expected_uid: int) -> Mapping[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise AlphaWorkerConfigError from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > _MAX_CONFIG_BYTES
        ):
            raise AlphaWorkerConfigError
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
            raise AlphaWorkerConfigError
    except OSError as error:
        raise AlphaWorkerConfigError from error
    finally:
        os.close(descriptor)
    try:
        decoded = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise AlphaWorkerConfigError from error
    if not isinstance(decoded, Mapping):
        raise AlphaWorkerConfigError
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
        raise AlphaWorkerConfigError


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value.get(key)
    if not isinstance(item, Mapping) or not all(isinstance(field, str) for field in item):
        raise AlphaWorkerConfigError
    return cast("Mapping[str, object]", item)


def _text(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item or "\x00" in item:
        raise AlphaWorkerConfigError
    return item


def _identifier(value: Mapping[str, object], key: str) -> str:
    item = _text(value, key)
    if _IDENTIFIER.fullmatch(item) is None:
        raise AlphaWorkerConfigError
    return item


def _token(value: Mapping[str, object], key: str) -> str:
    item = _text(value, key)
    if len(item) > 128 or any(not 0x21 <= ord(character) <= 0x7E for character in item):
        raise AlphaWorkerConfigError
    return item


def _environment_name(value: object) -> str:
    if not isinstance(value, str) or _ENVIRONMENT_NAME.fullmatch(value) is None:
        raise AlphaWorkerConfigError
    if value.startswith(("BLACKCELL_", "DYLD_", "GIT_", "LD_", "PYTHON")) or value in {
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
    }:
        raise AlphaWorkerConfigError
    return value


def _integer(
    value: Mapping[str, object],
    key: str,
    minimum: int,
    maximum: int,
) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
        raise AlphaWorkerConfigError
    return item


def _executable(value: object) -> Path:
    path = _canonical_path(value)
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise AlphaWorkerConfigError from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or not os.access(path, os.X_OK)
        or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID)
        or metadata.st_mode & 0o022
    ):
        raise AlphaWorkerConfigError
    return path


def _directory(value: object) -> Path:
    path = _canonical_path(value)
    if not path.is_dir():
        raise AlphaWorkerConfigError
    return path


def _owner_directory(value: object, *, expected_uid: int) -> Path:
    path = _directory(value)
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise AlphaWorkerConfigError from error
    if metadata.st_uid != expected_uid or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise AlphaWorkerConfigError
    return path


def _canonical_path(value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AlphaWorkerConfigError
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path.is_symlink():
        raise AlphaWorkerConfigError
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise AlphaWorkerConfigError from error
    if resolved != path:
        raise AlphaWorkerConfigError
    return resolved


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _current_uid() -> int:
    getuid = getattr(os, "geteuid", None)
    if getuid is None:
        raise AlphaWorkerConfigError
    return int(getuid())


__all__ = [
    "ALPHA_WORKER_CONFIG_FILE_ENV",
    "ALPHA_WORKER_CONFIG_SCHEMA",
    "AlphaExecutableAliasConfig",
    "AlphaIsolationRuntimeConfig",
    "AlphaProviderRuntimeConfig",
    "AlphaWorkerConfigError",
    "AlphaWorkerConfigFailureCode",
    "AlphaWorkerLoopConfig",
    "AlphaWorkerRuntimeConfig",
    "load_alpha_worker_config",
]
