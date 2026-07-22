"""Typed systemd-user lifecycle boundary for the foreground BlackCell daemon."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from blackcell.adapters.bounded_process import (
    BoundedProcessError,
    BoundedProcessFailureCode,
    BoundedProcessResult,
    BoundedProcessRunner,
)
from blackcell.kernel._json import bytes_digest

SYSTEMD_UNIT_NAME = "blackcell.service"
_COMMAND_TIMEOUT_SECONDS = 30.0
_COMMAND_STDOUT_LIMIT = 256 * 1024
_COMMAND_STDERR_LIMIT = 64 * 1024
_MAX_UNIT_BYTES = 64 * 1024
_MAX_ENVIRONMENT_FILE_BYTES = 1024 * 1024
_MAX_LOG_LINES = 200
_MAX_LOG_MESSAGE_BYTES = 4096

SystemdOperation = Literal["install", "start", "stop", "restart"]
SystemdOutcome = Literal["installed", "unchanged", "started", "stopped", "restarted"]


class SystemdServiceFailureCode(StrEnum):
    UNSUPPORTED_PLATFORM = "systemd-user-unsupported"
    MANAGER_UNAVAILABLE = "systemd-user-unavailable"
    INVALID_ENVIRONMENT_FILE = "invalid-daemon-environment-file"
    INVALID_EXECUTABLE = "invalid-daemon-runtime-executable"
    INVALID_UNIT_DIRECTORY = "invalid-systemd-user-unit-directory"
    UNIT_CONFLICT = "blackcell-unit-conflict"
    INSTALL_FAILED = "blackcell-unit-install-failed"
    COMMAND_FAILED = "systemd-user-command-failed"
    INVALID_RESPONSE = "invalid-systemd-user-response"
    INVALID_LOG_LIMIT = "invalid-daemon-log-limit"


class SystemdServiceError(RuntimeError):
    """A stable lifecycle failure that excludes argv, paths, and command output."""

    def __init__(self, code: SystemdServiceFailureCode) -> None:
        self.code = code
        super().__init__(code.value)

    @property
    def cli_exit_code(self) -> int:
        if self.code in {
            SystemdServiceFailureCode.INVALID_ENVIRONMENT_FILE,
            SystemdServiceFailureCode.INVALID_EXECUTABLE,
            SystemdServiceFailureCode.INVALID_UNIT_DIRECTORY,
            SystemdServiceFailureCode.INVALID_LOG_LIMIT,
        }:
            return 1
        if self.code in {
            SystemdServiceFailureCode.UNSUPPORTED_PLATFORM,
            SystemdServiceFailureCode.MANAGER_UNAVAILABLE,
            SystemdServiceFailureCode.COMMAND_FAILED,
        }:
            return 3
        return 4


@dataclass(frozen=True, slots=True)
class SystemdUnitStatus:
    available: bool
    installed: bool
    enabled: bool
    active: bool
    substate: str
    main_pid: int | None
    last_exit_status: int | None
    unit: str = SYSTEMD_UNIT_NAME
    manager: Literal["systemd-user"] = "systemd-user"
    schema_version: Literal["systemd-user-status/v1"] = "systemd-user-status/v1"


@dataclass(frozen=True, slots=True)
class SystemdLifecycleResult:
    operation: SystemdOperation
    outcome: SystemdOutcome
    service: SystemdUnitStatus
    unit_path: Path | None = None
    unit_digest: str | None = None
    schema_version: Literal["daemon-lifecycle/v1"] = "daemon-lifecycle/v1"


@dataclass(frozen=True, slots=True)
class SystemdLogEntry:
    timestamp_us: int
    priority: int
    message: str
    pid: int | None
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class SystemdLogResult:
    entries: tuple[SystemdLogEntry, ...]
    lines_requested: int
    unit: str = SYSTEMD_UNIT_NAME
    schema_version: Literal["daemon-logs/v1"] = "daemon-logs/v1"


class SystemdProcessRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        environment: Mapping[str, str] | None = None,
    ) -> BoundedProcessResult: ...


@dataclass(slots=True)
class SystemdUserServiceManager:
    runner: SystemdProcessRunner = field(default_factory=BoundedProcessRunner, repr=False)
    systemctl: str = "systemctl"
    journalctl: str = "journalctl"
    unit_directory: Path | None = None
    platform: str = sys.platform

    def status(self) -> SystemdUnitStatus:
        if not self.platform.startswith("linux"):
            return _unavailable_status()
        try:
            result = self.runner.run(
                (
                    self.systemctl,
                    "--user",
                    "--no-pager",
                    "show",
                    SYSTEMD_UNIT_NAME,
                    "--property=LoadState",
                    "--property=ActiveState",
                    "--property=SubState",
                    "--property=UnitFileState",
                    "--property=MainPID",
                    "--property=ExecMainStatus",
                ),
                cwd=Path("/"),
                timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
                stdout_limit_bytes=_COMMAND_STDOUT_LIMIT,
                stderr_limit_bytes=_COMMAND_STDERR_LIMIT,
            )
        except BoundedProcessError as error:
            if error.code in {
                BoundedProcessFailureCode.SPAWN_FAILED,
                BoundedProcessFailureCode.TIMED_OUT,
            }:
                return _unavailable_status()
            raise SystemdServiceError(SystemdServiceFailureCode.INVALID_RESPONSE) from error
        if result.return_code != 0:
            return _unavailable_status()
        if result.stderr.captured:
            raise SystemdServiceError(SystemdServiceFailureCode.INVALID_RESPONSE)
        return _parse_status(result.stdout.captured)

    def install(
        self,
        *,
        environment_file: Path,
        runtime_executable: Path,
    ) -> SystemdLifecycleResult:
        self._require_platform()
        environment_path = _validate_environment_file(environment_file)
        executable_path = _validate_runtime_executable(runtime_executable)
        before = self.status()
        if not before.available:
            raise SystemdServiceError(SystemdServiceFailureCode.MANAGER_UNAVAILABLE)

        unit_directory = self.unit_directory or default_systemd_user_unit_directory()
        unit_path = _validate_unit_destination(unit_directory)
        content = render_systemd_user_unit(executable_path, environment_path).encode("utf-8")
        created = _install_unit_file(unit_path, content)
        try:
            self._run_control("daemon-reload")
            self._run_control("enable", SYSTEMD_UNIT_NAME)
        except SystemdServiceError:
            if created:
                self._rollback_created_unit(unit_path)
            raise

        try:
            service = self.status()
            if not service.available or not service.installed or not service.enabled:
                raise SystemdServiceError(SystemdServiceFailureCode.INSTALL_FAILED)
        except SystemdServiceError:
            if created:
                self._rollback_created_unit(unit_path)
            raise
        return SystemdLifecycleResult(
            operation="install",
            outcome="installed" if created else "unchanged",
            service=service,
            unit_path=unit_path,
            unit_digest=bytes_digest(content),
        )

    def start(self) -> SystemdLifecycleResult:
        return self._change_state("start", expected_active=True, outcome="started")

    def stop(self) -> SystemdLifecycleResult:
        return self._change_state("stop", expected_active=False, outcome="stopped")

    def restart(self) -> SystemdLifecycleResult:
        return self._change_state("restart", expected_active=True, outcome="restarted")

    def logs(self, *, lines: int = 100) -> SystemdLogResult:
        self._require_platform()
        if (
            isinstance(lines, bool)
            or not isinstance(lines, int)
            or not 1 <= lines <= _MAX_LOG_LINES
        ):
            raise SystemdServiceError(SystemdServiceFailureCode.INVALID_LOG_LIMIT)
        service = self.status()
        if not service.available:
            raise SystemdServiceError(SystemdServiceFailureCode.MANAGER_UNAVAILABLE)
        if not service.installed:
            raise SystemdServiceError(SystemdServiceFailureCode.COMMAND_FAILED)
        result = self._run(
            (
                self.journalctl,
                "--user-unit",
                SYSTEMD_UNIT_NAME,
                "--no-pager",
                "--output=json",
                "--output-fields=MESSAGE,__REALTIME_TIMESTAMP,PRIORITY,_PID",
                "--lines",
                str(lines),
            ),
            stdout_limit_bytes=_COMMAND_STDOUT_LIMIT,
        )
        if result.stderr.captured:
            raise SystemdServiceError(SystemdServiceFailureCode.INVALID_RESPONSE)
        return SystemdLogResult(entries=_parse_logs(result.stdout.captured), lines_requested=lines)

    def _change_state(
        self,
        operation: Literal["start", "stop", "restart"],
        *,
        expected_active: bool,
        outcome: Literal["started", "stopped", "restarted"],
    ) -> SystemdLifecycleResult:
        self._require_platform()
        before = self.status()
        if not before.available:
            raise SystemdServiceError(SystemdServiceFailureCode.MANAGER_UNAVAILABLE)
        if not before.installed:
            raise SystemdServiceError(SystemdServiceFailureCode.COMMAND_FAILED)
        self._run_control(operation, SYSTEMD_UNIT_NAME)
        service = self.status()
        if not service.available or service.active is not expected_active:
            raise SystemdServiceError(SystemdServiceFailureCode.COMMAND_FAILED)
        return SystemdLifecycleResult(operation=operation, outcome=outcome, service=service)

    def _run_control(self, operation: str, *arguments: str) -> BoundedProcessResult:
        return self._run(
            (
                self.systemctl,
                "--user",
                "--no-pager",
                "--no-ask-password",
                operation,
                *arguments,
            )
        )

    def _run(
        self,
        argv: tuple[str, ...],
        *,
        stdout_limit_bytes: int = _COMMAND_STDOUT_LIMIT,
    ) -> BoundedProcessResult:
        try:
            result = self.runner.run(
                argv,
                cwd=Path("/"),
                timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
                stdout_limit_bytes=stdout_limit_bytes,
                stderr_limit_bytes=_COMMAND_STDERR_LIMIT,
            )
        except BoundedProcessError as error:
            if error.code is BoundedProcessFailureCode.SPAWN_FAILED:
                code = SystemdServiceFailureCode.MANAGER_UNAVAILABLE
            elif error.code in {
                BoundedProcessFailureCode.OUTPUT_TOO_LARGE,
                BoundedProcessFailureCode.OUTPUT_INCOMPLETE,
            }:
                code = SystemdServiceFailureCode.INVALID_RESPONSE
            else:
                code = SystemdServiceFailureCode.COMMAND_FAILED
            raise SystemdServiceError(code) from error
        if result.return_code != 0:
            raise SystemdServiceError(SystemdServiceFailureCode.COMMAND_FAILED)
        return result

    def _rollback_created_unit(self, unit_path: Path) -> None:
        with suppress(SystemdServiceError):
            self._run_control("disable", SYSTEMD_UNIT_NAME)
        try:
            unit_path.unlink(missing_ok=True)
        except OSError:
            return
        with suppress(SystemdServiceError):
            self._run_control("daemon-reload")

    def _require_platform(self) -> None:
        if not self.platform.startswith("linux"):
            raise SystemdServiceError(SystemdServiceFailureCode.UNSUPPORTED_PLATFORM)


def default_systemd_user_unit_directory(
    environment: Mapping[str, str] | None = None,
) -> Path:
    values = os.environ if environment is None else environment
    configured = values.get("XDG_CONFIG_HOME")
    if configured:
        root = Path(configured)
        if not root.is_absolute():
            raise SystemdServiceError(SystemdServiceFailureCode.INVALID_UNIT_DIRECTORY)
    else:
        root = Path.home() / ".config"
    return root / "systemd/user"


def render_systemd_user_unit(runtime_executable: Path, environment_file: Path) -> str:
    executable = _quote_systemd_value(str(runtime_executable))
    environment = _quote_systemd_value(str(environment_file))
    return "\n".join(
        (
            "[Unit]",
            "Description=BlackCell foreground project runtime",
            "After=network.target",
            "StartLimitIntervalSec=60s",
            "StartLimitBurst=3",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={executable} daemon",
            f"EnvironmentFile={environment}",
            "Environment=PYTHONUNBUFFERED=1",
            "Restart=on-failure",
            "RestartSec=2s",
            "TimeoutStartSec=30s",
            "TimeoutStopSec=310s",
            "KillMode=control-group",
            "SendSIGKILL=yes",
            "UMask=0077",
            "NoNewPrivileges=yes",
            "PrivateTmp=yes",
            "StandardOutput=journal",
            "StandardError=journal",
            "SyslogIdentifier=blackcell",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        )
    )


def _parse_status(raw: bytes) -> SystemdUnitStatus:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise SystemdServiceError(SystemdServiceFailureCode.INVALID_RESPONSE) from error
    expected = {
        "LoadState",
        "ActiveState",
        "SubState",
        "UnitFileState",
        "MainPID",
        "ExecMainStatus",
    }
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in expected or key in values or len(value) > 128:
            raise SystemdServiceError(SystemdServiceFailureCode.INVALID_RESPONSE)
        values[key] = value
    if set(values) != expected:
        raise SystemdServiceError(SystemdServiceFailureCode.INVALID_RESPONSE)
    main_pid = _optional_nonnegative_integer(values["MainPID"], zero_is_none=True)
    last_exit = _optional_nonnegative_integer(values["ExecMainStatus"], zero_is_none=False)
    load_state = values["LoadState"]
    active_state = values["ActiveState"]
    return SystemdUnitStatus(
        available=True,
        installed=load_state != "not-found",
        enabled=values["UnitFileState"] in {"enabled", "enabled-runtime"},
        active=active_state == "active",
        substate=values["SubState"],
        main_pid=main_pid if active_state == "active" else None,
        last_exit_status=last_exit,
    )


def _parse_logs(raw: bytes) -> tuple[SystemdLogEntry, ...]:
    entries: list[SystemdLogEntry] = []
    for line in raw.splitlines():
        if not line:
            continue
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SystemdServiceError(SystemdServiceFailureCode.INVALID_RESPONSE) from error
        if not isinstance(value, dict):
            raise SystemdServiceError(SystemdServiceFailureCode.INVALID_RESPONSE)
        message = value.get("MESSAGE")
        timestamp = value.get("__REALTIME_TIMESTAMP")
        priority = value.get("PRIORITY", "6")
        pid = value.get("_PID")
        if not isinstance(message, str):
            raise SystemdServiceError(SystemdServiceFailureCode.INVALID_RESPONSE)
        timestamp_us = _decimal_integer(timestamp)
        parsed_priority = _decimal_integer(priority)
        if not 0 <= parsed_priority <= 7:
            raise SystemdServiceError(SystemdServiceFailureCode.INVALID_RESPONSE)
        parsed_pid = None if pid is None else _decimal_integer(pid)
        bounded_message, truncated = _bounded_message(message)
        entries.append(
            SystemdLogEntry(
                timestamp_us=timestamp_us,
                priority=parsed_priority,
                message=bounded_message,
                pid=parsed_pid,
                truncated=truncated,
            )
        )
    if len(entries) > _MAX_LOG_LINES:
        raise SystemdServiceError(SystemdServiceFailureCode.INVALID_RESPONSE)
    return tuple(entries)


def _bounded_message(value: str) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= _MAX_LOG_MESSAGE_BYTES:
        return value, False
    bounded = encoded[:_MAX_LOG_MESSAGE_BYTES].decode("utf-8", errors="ignore")
    return bounded, True


def _validate_environment_file(value: Path) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise SystemdServiceError(SystemdServiceFailureCode.INVALID_ENVIRONMENT_FILE)
    try:
        metadata = value.lstat()
    except OSError as error:
        raise SystemdServiceError(SystemdServiceFailureCode.INVALID_ENVIRONMENT_FILE) from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > _MAX_ENVIRONMENT_FILE_BYTES
    ):
        raise SystemdServiceError(SystemdServiceFailureCode.INVALID_ENVIRONMENT_FILE)
    return value


def _validate_runtime_executable(value: Path) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise SystemdServiceError(SystemdServiceFailureCode.INVALID_EXECUTABLE)
    try:
        resolved = value.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise SystemdServiceError(SystemdServiceFailureCode.INVALID_EXECUTABLE) from error
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise SystemdServiceError(SystemdServiceFailureCode.INVALID_EXECUTABLE)
    return resolved


def _validate_unit_destination(directory: Path) -> Path:
    if not isinstance(directory, Path) or not directory.is_absolute():
        raise SystemdServiceError(SystemdServiceFailureCode.INVALID_UNIT_DIRECTORY)
    try:
        if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
            raise SystemdServiceError(SystemdServiceFailureCode.INVALID_UNIT_DIRECTORY)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise SystemdServiceError(SystemdServiceFailureCode.INVALID_UNIT_DIRECTORY) from error
    return directory / SYSTEMD_UNIT_NAME


def _install_unit_file(path: Path, content: bytes) -> bool:
    if len(content) > _MAX_UNIT_BYTES:
        raise SystemdServiceError(SystemdServiceFailureCode.INSTALL_FAILED)
    try:
        if path.exists() or path.is_symlink():
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise SystemdServiceError(SystemdServiceFailureCode.UNIT_CONFLICT)
            if metadata.st_size > _MAX_UNIT_BYTES or path.read_bytes() != content:
                raise SystemdServiceError(SystemdServiceFailureCode.UNIT_CONFLICT)
            return False

        descriptor, temporary_name = tempfile.mkstemp(prefix=".blackcell.service.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.link(temporary, path, follow_symlinks=False)
        finally:
            temporary.unlink(missing_ok=True)
        return True
    except SystemdServiceError:
        raise
    except FileExistsError as error:
        raise SystemdServiceError(SystemdServiceFailureCode.UNIT_CONFLICT) from error
    except OSError as error:
        raise SystemdServiceError(SystemdServiceFailureCode.INSTALL_FAILED) from error


def _quote_systemd_value(value: str) -> str:
    if not value or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise SystemdServiceError(SystemdServiceFailureCode.INSTALL_FAILED)
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _optional_nonnegative_integer(value: object, *, zero_is_none: bool) -> int | None:
    parsed = _decimal_integer(value)
    if parsed < 0:
        raise SystemdServiceError(SystemdServiceFailureCode.INVALID_RESPONSE)
    if zero_is_none and parsed == 0:
        return None
    return parsed


def _decimal_integer(value: object) -> int:
    if not isinstance(value, str) or not value.isdecimal():
        raise SystemdServiceError(SystemdServiceFailureCode.INVALID_RESPONSE)
    return int(value)


def _unavailable_status() -> SystemdUnitStatus:
    return SystemdUnitStatus(
        available=False,
        installed=False,
        enabled=False,
        active=False,
        substate="unavailable",
        main_pid=None,
        last_exit_status=None,
    )


__all__ = [
    "SYSTEMD_UNIT_NAME",
    "SystemdLifecycleResult",
    "SystemdLogEntry",
    "SystemdLogResult",
    "SystemdServiceError",
    "SystemdServiceFailureCode",
    "SystemdUnitStatus",
    "SystemdUserServiceManager",
    "default_systemd_user_unit_directory",
    "render_systemd_user_unit",
]
