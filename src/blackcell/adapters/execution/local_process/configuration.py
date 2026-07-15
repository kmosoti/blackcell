"""Trusted-command configuration; this module does not define a sandbox.

Version 1 accepts only administrator-owned, immutable, audited ELF executables and
administrator-owned immutable working/root directories. ``READ_ONLY`` is a declared and
reviewed property of the command, not something this adapter can enforce at the syscall layer.
See ``LOCAL_PROCESS_V1_ACTIVATION_CONTRACT`` in the package module before activation.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from blackcell.features.execute_affordance import (
    AffordanceDefinition,
    SideEffectClass,
)
from blackcell.kernel._json import json_digest

LOCAL_PROCESS_ADAPTER_ID = "blackcell.local-process"
_CONFIGURATION_SCHEMA = "local-process-affordance/v1"
_REGISTRY_SCHEMA = "local-process-registry/v1"
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_OPTION_PREFIX = re.compile(r"--[A-Za-z][A-Za-z0-9-]*=\Z")
_DANGEROUS_ENVIRONMENT_NAMES = frozenset(
    {
        "BASH_ENV",
        "CDPATH",
        "ENV",
        "GLOBIGNORE",
        "GLIBC_TUNABLES",
        "GCONV_PATH",
        "HOME",
        "HOSTALIASES",
        "IFS",
        "NLSPATH",
        "NODE_OPTIONS",
        "PATH",
        "PERL5OPT",
        "PERL5LIB",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "JAVA_TOOL_OPTIONS",
        "LUA_INIT",
        "RUBYLIB",
        "RUBYOPT",
        "SHELLOPTS",
        "TMPDIR",
    }
)
_DANGEROUS_ENVIRONMENT_PREFIXES = (
    "AWS_",
    "BASH_FUNC_",
    "DYLD_",
    "GIT_",
    "LD_",
    "SSH_",
    "XDG_",
)
_PROTECTED_DATA_ROOTS = tuple(
    Path(value) for value in ("/boot", "/dev", "/etc", "/proc", "/root", "/run", "/sys")
)


class LocalProcessConfigurationError(ValueError):
    """Configuration falls outside the trusted-command v1 activation contract."""


class ArgumentKind(StrEnum):
    TEXT = "text"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    PATH = "path"


@dataclass(frozen=True, slots=True)
class ArgumentBinding:
    """Map one typed affordance argument to exactly one argv token."""

    name: str
    kind: ArgumentKind
    option_prefix: str | None = None
    maximum_bytes: int = 4096

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise LocalProcessConfigurationError("argument binding name must not be empty")
        if not isinstance(self.kind, ArgumentKind):
            raise LocalProcessConfigurationError("argument binding kind must be recognized")
        if (
            isinstance(self.maximum_bytes, bool)
            or not isinstance(self.maximum_bytes, int)
            or self.maximum_bytes < 1
        ):
            raise LocalProcessConfigurationError("argument binding maximum must be positive")
        if self.option_prefix is not None and not _OPTION_PREFIX.fullmatch(self.option_prefix):
            raise LocalProcessConfigurationError(
                "argument option prefix must be a canonical --name= token prefix"
            )


@dataclass(frozen=True, slots=True, order=True)
class EnvironmentEntry:
    """Legacy-shaped fixed entry; v1 configurations require the tuple to be empty."""

    name: str
    value: str

    def __post_init__(self) -> None:
        if not _ENVIRONMENT_NAME.fullmatch(self.name):
            raise LocalProcessConfigurationError("environment name is invalid")
        normalized = self.name.upper()
        if normalized in _DANGEROUS_ENVIRONMENT_NAMES or normalized.startswith(
            _DANGEROUS_ENVIRONMENT_PREFIXES
        ):
            raise LocalProcessConfigurationError(
                f"environment variable {self.name!r} is not permitted"
            )
        _validate_token(self.value, label=f"environment variable {self.name!r}")
        if len(self.value.encode("utf-8")) > 4096:
            raise LocalProcessConfigurationError("environment value exceeds 4096 bytes")


@dataclass(frozen=True, slots=True)
class LocalProcessAffordance:
    """Administrator-owned declaration for one audited, read-only-labelled command."""

    definition: AffordanceDefinition
    executable: str
    fixed_argv: tuple[str, ...]
    bindings: tuple[ArgumentBinding, ...]
    working_directory: str
    allowed_path_roots: tuple[str, ...]
    environment: tuple[EnvironmentEntry, ...] = ()
    stdout_limit_bytes: int = 64 * 1024
    stderr_limit_bytes: int = 64 * 1024
    termination_grace_seconds: float = 1.0
    drain_grace_seconds: float = 1.0
    schema_version: str = _CONFIGURATION_SCHEMA
    executable_digest: str = field(init=False)
    executable_identity: tuple[int, int] = field(init=False)
    working_directory_identity: tuple[int, int] = field(init=False)
    allowed_path_root_identities: tuple[tuple[int, int], ...] = field(init=False)
    configuration_digest: str = field(init=False)

    def __post_init__(self) -> None:
        require_supported_platform()
        if self.schema_version != _CONFIGURATION_SCHEMA:
            raise LocalProcessConfigurationError(
                f"unsupported local-process configuration {self.schema_version!r}"
            )
        if self.definition.adapter_id != LOCAL_PROCESS_ADAPTER_ID:
            raise LocalProcessConfigurationError(
                f"definition adapter_id must be {LOCAL_PROCESS_ADAPTER_ID!r}"
            )
        if self.definition.side_effect_class is not SideEffectClass.READ_ONLY:
            raise LocalProcessConfigurationError(
                "local-process/v1 requires a SideEffectClass.READ_ONLY declaration; "
                "the adapter does not enforce effects"
            )
        if not math.isfinite(self.definition.timeout_seconds):
            raise LocalProcessConfigurationError("affordance timeout must be finite")
        executable = canonical_existing_path(
            self.executable,
            label="executable",
            kind="file",
        )
        executable_stat = executable.stat(follow_symlinks=False)
        if not stat.S_ISREG(executable_stat.st_mode) or not os.access(executable, os.X_OK):
            raise LocalProcessConfigurationError("executable must be an executable regular file")
        _require_immutable_permissions(executable_stat, label="executable", executable=True)
        _require_elf_binary(executable)
        object.__setattr__(self, "executable", os.fspath(executable))
        object.__setattr__(self, "executable_digest", file_digest(executable))
        object.__setattr__(self, "executable_identity", _identity(executable_stat))

        working_directory = canonical_existing_path(
            self.working_directory,
            label="working directory",
            kind="directory",
        )
        working_directory_stat = working_directory.stat(follow_symlinks=False)
        _require_immutable_permissions(
            working_directory_stat,
            label="working directory",
            executable=False,
        )
        object.__setattr__(self, "working_directory", os.fspath(working_directory))
        object.__setattr__(
            self,
            "working_directory_identity",
            _identity(working_directory_stat),
        )
        if not self.allowed_path_roots:
            raise LocalProcessConfigurationError("at least one allowed path root is required")
        roots = tuple(
            os.fspath(canonical_existing_path(value, label="allowed path root", kind="directory"))
            for value in self.allowed_path_roots
        )
        root_stats = tuple(Path(value).stat(follow_symlinks=False) for value in roots)
        for root_stat in root_stats:
            _require_immutable_permissions(
                root_stat,
                label="allowed path root",
                executable=False,
            )
        if roots != tuple(sorted(set(roots))):
            raise LocalProcessConfigurationError(
                "allowed path roots must be unique and sorted canonically"
            )
        if not any(path_is_within(working_directory, Path(root)) for root in roots):
            raise LocalProcessConfigurationError(
                "working directory must be confined to an allowed path root"
            )
        object.__setattr__(self, "allowed_path_roots", roots)
        object.__setattr__(
            self,
            "allowed_path_root_identities",
            tuple(_identity(root_stat) for root_stat in root_stats),
        )

        if len(self.fixed_argv) > 64:
            raise LocalProcessConfigurationError("fixed argv exceeds 64 tokens")
        for token in self.fixed_argv:
            _validate_token(token, label="fixed argv token")
            if len(token.encode("utf-8")) > 4096:
                raise LocalProcessConfigurationError("fixed argv token exceeds 4096 bytes")

        definition_arguments = self.definition.arguments
        if any(not item.required for item in definition_arguments):
            raise LocalProcessConfigurationError(
                "local-process definitions require every declared argument"
            )
        declared = tuple(item.name for item in definition_arguments)
        bound = tuple(item.name for item in self.bindings)
        if bound != declared:
            raise LocalProcessConfigurationError(
                "ordered bindings must exactly match definition arguments"
            )
        if any(item.option_prefix is None for item in self.bindings) and (
            not self.fixed_argv or self.fixed_argv[-1] != "--"
        ):
            raise LocalProcessConfigurationError(
                "positional bindings require fixed argv to end with '--'"
            )

        if self.environment:
            raise LocalProcessConfigurationError(
                "local-process/v1 requires an exactly empty environment"
            )

        for label, value in (
            ("stdout limit", self.stdout_limit_bytes),
            ("stderr limit", self.stderr_limit_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise LocalProcessConfigurationError(f"{label} must be a non-negative integer")
        for label, value in (
            ("termination grace", self.termination_grace_seconds),
            ("drain grace", self.drain_grace_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise LocalProcessConfigurationError(f"{label} must be positive")
            object.__setattr__(
                self,
                "termination_grace_seconds"
                if label == "termination grace"
                else "drain_grace_seconds",
                float(value),
            )
        object.__setattr__(
            self,
            "configuration_digest",
            json_digest(local_process_affordance_payload(self)),
        )


@dataclass(frozen=True, slots=True)
class LocalProcessRegistry:
    affordances: tuple[LocalProcessAffordance, ...]
    schema_version: str = _REGISTRY_SCHEMA
    registry_digest: str = field(init=False)
    contract_version: str = field(init=False)

    def __post_init__(self) -> None:
        require_supported_platform()
        if self.schema_version != _REGISTRY_SCHEMA:
            raise LocalProcessConfigurationError(
                f"unsupported local-process registry {self.schema_version!r}"
            )
        if not self.affordances:
            raise LocalProcessConfigurationError("local-process registry must not be empty")
        names = tuple(item.definition.name for item in self.affordances)
        if names != tuple(sorted(set(names))):
            raise LocalProcessConfigurationError(
                "local-process affordances must be unique and sorted by name"
            )
        digest = json_digest(
            {
                "schema_version": self.schema_version,
                "affordances": [
                    local_process_affordance_payload(item) for item in self.affordances
                ],
            }
        )
        object.__setattr__(self, "registry_digest", digest)
        object.__setattr__(self, "contract_version", f"{self.schema_version}@{digest}")

    def get(self, affordance: str) -> LocalProcessAffordance:
        for configured in self.affordances:
            if configured.definition.name == affordance:
                return configured
        raise LookupError(f"local-process affordance {affordance!r} is not registered")


def local_process_affordance_payload(config: LocalProcessAffordance) -> dict[str, object]:
    definition = config.definition
    return {
        "schema_version": config.schema_version,
        "definition": {
            "name": definition.name,
            "adapter_id": definition.adapter_id,
            "side_effect_class": definition.side_effect_class.value,
            "timeout_seconds": definition.timeout_seconds,
            "arguments": [
                {"name": item.name, "required": item.required} for item in definition.arguments
            ],
        },
        "executable": config.executable,
        "executable_digest": config.executable_digest,
        "executable_identity": {
            "device": config.executable_identity[0],
            "inode": config.executable_identity[1],
        },
        "fixed_argv": list(config.fixed_argv),
        "bindings": [
            {
                "name": item.name,
                "kind": item.kind.value,
                "option_prefix": item.option_prefix,
                "maximum_bytes": item.maximum_bytes,
            }
            for item in config.bindings
        ],
        "working_directory": config.working_directory,
        "working_directory_identity": {
            "device": config.working_directory_identity[0],
            "inode": config.working_directory_identity[1],
        },
        "allowed_path_roots": list(config.allowed_path_roots),
        "allowed_path_root_identities": [
            {"device": identity[0], "inode": identity[1]}
            for identity in config.allowed_path_root_identities
        ],
        "environment": [{"name": item.name, "value": item.value} for item in config.environment],
        "stdout_limit_bytes": config.stdout_limit_bytes,
        "stderr_limit_bytes": config.stderr_limit_bytes,
        "termination_grace_seconds": config.termination_grace_seconds,
        "drain_grace_seconds": config.drain_grace_seconds,
    }


def canonical_existing_path(
    value: str,
    *,
    label: str,
    kind: str,
) -> Path:
    if not isinstance(value, str) or not value:
        raise LocalProcessConfigurationError(f"{label} must be a non-empty path string")
    if "\x00" in value:
        raise LocalProcessConfigurationError(f"{label} contains a null byte")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise LocalProcessConfigurationError(f"{label} must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise LocalProcessConfigurationError(f"{label} does not resolve canonically") from error
    if candidate != resolved:
        raise LocalProcessConfigurationError(
            f"{label} must be canonical and contain no symlink components"
        )
    try:
        metadata = candidate.stat(follow_symlinks=False)
    except OSError as error:
        raise LocalProcessConfigurationError(f"{label} cannot be inspected") from error
    if kind == "file" and not stat.S_ISREG(metadata.st_mode):
        raise LocalProcessConfigurationError(f"{label} must be a regular file")
    if kind == "directory" and not stat.S_ISDIR(metadata.st_mode):
        raise LocalProcessConfigurationError(f"{label} must be a directory")
    if path_is_protected(candidate):
        raise LocalProcessConfigurationError(f"{label} uses a protected path")
    return candidate


def file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                hasher.update(chunk)
    except OSError as error:
        raise LocalProcessConfigurationError(
            "executable cannot be read deterministically"
        ) from error
    return f"sha256:{hasher.hexdigest()}"


def file_descriptor_digest(file_descriptor: int) -> str:
    hasher = hashlib.sha256()
    try:
        offset = os.lseek(file_descriptor, 0, os.SEEK_CUR)
        os.lseek(file_descriptor, 0, os.SEEK_SET)
        while chunk := os.read(file_descriptor, 64 * 1024):
            hasher.update(chunk)
        os.lseek(file_descriptor, offset, os.SEEK_SET)
    except OSError as error:
        raise LocalProcessConfigurationError(
            "pinned executable cannot be hashed deterministically"
        ) from error
    return f"sha256:{hasher.hexdigest()}"


def require_trusted_path_permissions(
    path: Path,
    *,
    label: str,
    executable: bool = False,
) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise LocalProcessConfigurationError(f"{label} cannot be inspected") from error
    _require_immutable_permissions(metadata, label=label, executable=executable)


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def path_is_protected(path: Path) -> bool:
    return path == Path("/") or any(path_is_within(path, root) for root in _PROTECTED_DATA_ROOTS)


def require_supported_platform() -> None:
    if os.name != "posix" or sys.platform != "linux":
        raise LocalProcessConfigurationError(
            "local-process execution is supported only on POSIX Linux"
        )


def _validate_token(value: str, *, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise LocalProcessConfigurationError(f"{label} must not be empty")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise LocalProcessConfigurationError(f"{label} contains a control character")


def _require_elf_binary(executable: Path) -> None:
    try:
        with executable.open("rb") as handle:
            header = handle.read(4)
    except OSError as error:
        raise LocalProcessConfigurationError("executable header cannot be inspected") from error
    if header != b"\x7fELF":
        raise LocalProcessConfigurationError(
            "local-process/v1 executable must be an ELF binary; scripts are unsupported"
        )


def _require_immutable_permissions(
    metadata: os.stat_result,
    *,
    label: str,
    executable: bool,
) -> None:
    if metadata.st_uid not in {0, os.geteuid()}:
        raise LocalProcessConfigurationError(
            f"{label} must be owned by root or the runtime administrator"
        )
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise LocalProcessConfigurationError(f"{label} must not be group- or world-writable")
    if executable and metadata.st_mode & (stat.S_ISUID | stat.S_ISGID):
        raise LocalProcessConfigurationError(
            "executable must not carry setuid or setgid permission bits"
        )


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino
