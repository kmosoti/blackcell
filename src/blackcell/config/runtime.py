from __future__ import annotations

import ipaddress
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from blackcell.config.secrets import (
    SecretValue,
    SecurityConfigError,
    SecurityConfigFailureCode,
    load_service_token,
)
from blackcell.interfaces import (
    ALL_SERVICE_SCOPES,
    BearerAuthenticator,
    ScopeAuthorizer,
    ServicePrincipal,
)
from blackcell.telemetry import ContentMode, ContentPolicy

DATA_DIR_ENV = "BLACKCELL_DATA_DIR"
BIND_HOST_ENV = "BLACKCELL_BIND_HOST"
BIND_PORT_ENV = "BLACKCELL_BIND_PORT"
TRUSTED_PROXY_HOPS_ENV = "BLACKCELL_TRUSTED_PROXY_HOPS"


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    data_root: Path
    database_path: Path
    artifact_root: Path
    backup_root: Path

    def __post_init__(self) -> None:
        if (
            self.database_path != self.data_root / "kernel.sqlite3"
            or self.artifact_root != self.data_root / "artifacts"
            or self.backup_root != self.data_root / "backups"
        ):
            raise ValueError("runtime paths must use the canonical data-root layout")

    @classmethod
    def prepare(cls, value: str, *, expected_uid: int | None = None) -> RuntimePaths:
        if not isinstance(value, str) or not value:
            raise SecurityConfigError(SecurityConfigFailureCode.INVALID_DATA_DIRECTORY)
        root = Path(value)
        if not root.is_absolute() or ".." in root.parts:
            raise SecurityConfigError(SecurityConfigFailureCode.INVALID_DATA_DIRECTORY)
        uid = _current_uid() if expected_uid is None else expected_uid
        _prepare_owner_directory(root, uid=uid)
        database = root / "kernel.sqlite3"
        if database.exists() or database.is_symlink():
            _validate_owner_file(database, uid=uid)
        artifacts = root / "artifacts"
        backups = root / "backups"
        _prepare_owner_directory(artifacts, uid=uid)
        _prepare_owner_directory(backups, uid=uid)
        return cls(root, database, artifacts, backups)

    def ensure_database_file(self, *, expected_uid: int | None = None) -> Path:
        """Create or revalidate the canonical owner-only SQLite file."""

        uid = _current_uid() if expected_uid is None else expected_uid
        _prepare_owner_directory(self.data_root, uid=uid)
        _prepare_owner_directory(self.artifact_root, uid=uid)
        _prepare_owner_directory(self.backup_root, uid=uid)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.database_path, flags, 0o600)
        except FileExistsError:
            _validate_owner_file(self.database_path, uid=uid)
            return self.database_path
        except OSError as error:
            raise SecurityConfigError(SecurityConfigFailureCode.UNSAFE_DATA_DIRECTORY) from error
        try:
            try:
                os.fchmod(descriptor, 0o600)
                metadata = os.fstat(descriptor)
            except OSError as error:
                raise SecurityConfigError(
                    SecurityConfigFailureCode.UNSAFE_DATA_DIRECTORY
                ) from error
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != uid
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise SecurityConfigError(SecurityConfigFailureCode.UNSAFE_DATA_DIRECTORY)
        finally:
            os.close(descriptor)
        return self.database_path


@dataclass(frozen=True, slots=True)
class RuntimeSecurityConfig:
    paths: RuntimePaths
    bind_host: str
    bind_port: int
    trusted_proxy_hops: int
    principal: ServicePrincipal
    _token: SecretValue = field(repr=False, compare=False)

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        expected_uid: int | None = None,
    ) -> RuntimeSecurityConfig:
        values = os.environ if environment is None else environment
        if DATA_DIR_ENV not in values:
            raise SecurityConfigError(SecurityConfigFailureCode.INVALID_DATA_DIRECTORY)
        paths = RuntimePaths.prepare(values[DATA_DIR_ENV], expected_uid=expected_uid)
        token = load_service_token(values, expected_uid=expected_uid)
        bind_host = _bind_host(values.get(BIND_HOST_ENV, "127.0.0.1"))
        bind_port = _bind_port(values.get(BIND_PORT_ENV, "8080"))
        trusted_proxy_hops = _trusted_proxy_hops(values.get(TRUSTED_PROXY_HOPS_ENV, "0"))
        principal = ServicePrincipal("service:runtime-v1", ALL_SERVICE_SCOPES)
        return cls(paths, bind_host, bind_port, trusted_proxy_hops, principal, token)

    def authenticator(self) -> BearerAuthenticator:
        return BearerAuthenticator(self._token, self.principal)

    def authorizer(self) -> ScopeAuthorizer:
        return ScopeAuthorizer()

    def telemetry_policy(self) -> ContentPolicy:
        return ContentPolicy(
            mode=ContentMode.METADATA_ONLY,
            sensitive_values=(self._token.redaction_value(),),
        )


def _prepare_owner_directory(path: Path, *, uid: int) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=False)
            os.chmod(path, 0o700)
            metadata = path.lstat()
        except OSError as error:
            raise SecurityConfigError(SecurityConfigFailureCode.UNSAFE_DATA_DIRECTORY) from error
    except OSError as error:
        raise SecurityConfigError(SecurityConfigFailureCode.UNSAFE_DATA_DIRECTORY) from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != uid
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise SecurityConfigError(SecurityConfigFailureCode.UNSAFE_DATA_DIRECTORY)


def _validate_owner_file(path: Path, *, uid: int) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise SecurityConfigError(SecurityConfigFailureCode.UNSAFE_DATA_DIRECTORY) from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != uid
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise SecurityConfigError(SecurityConfigFailureCode.UNSAFE_DATA_DIRECTORY)


def _bind_host(value: str) -> str:
    if not isinstance(value, str):
        raise SecurityConfigError(SecurityConfigFailureCode.INVALID_BIND)
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError as error:
        raise SecurityConfigError(SecurityConfigFailureCode.INVALID_BIND) from error


def _bind_port(value: str) -> int:
    if not isinstance(value, str) or not value.isdecimal():
        raise SecurityConfigError(SecurityConfigFailureCode.INVALID_BIND)
    port = int(value)
    if not 1 <= port <= 65_535:
        raise SecurityConfigError(SecurityConfigFailureCode.INVALID_BIND)
    return port


def _trusted_proxy_hops(value: str) -> int:
    if value != "0":
        raise SecurityConfigError(SecurityConfigFailureCode.INVALID_PROXY_TRUST)
    return 0


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise SecurityConfigError(SecurityConfigFailureCode.UNSAFE_DATA_DIRECTORY)
    return int(getuid())


__all__ = [
    "BIND_HOST_ENV",
    "BIND_PORT_ENV",
    "DATA_DIR_ENV",
    "TRUSTED_PROXY_HOPS_ENV",
    "RuntimePaths",
    "RuntimeSecurityConfig",
]
