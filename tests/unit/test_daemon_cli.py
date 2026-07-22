from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar, Literal

from blackcell.adapters.daemon_systemd import (
    SystemdLifecycleResult,
    SystemdLogEntry,
    SystemdLogResult,
    SystemdUnitStatus,
)
from blackcell.adapters.runtime_http import (
    RUNTIME_ENDPOINT_ENV,
    RuntimeClientError,
    RuntimeClientFailureCode,
    RuntimeServiceStatus,
)
from blackcell.cli.app import app
from tests.cli_runner import CycloptsCliRunner

runner = CycloptsCliRunner()


class FakeRuntimeClient:
    instances: ClassVar[list[FakeRuntimeClient]] = []
    status_result: ClassVar[RuntimeServiceStatus | RuntimeClientError]

    def __init__(self, *, endpoint: str) -> None:
        self.endpoint = endpoint
        type(self).instances.append(self)

    def status(self) -> RuntimeServiceStatus:
        if isinstance(self.status_result, RuntimeClientError):
            raise self.status_result
        return self.status_result


class FakeSystemdManager:
    instances: ClassVar[list[FakeSystemdManager]] = []
    status_result: ClassVar[SystemdUnitStatus]
    install_calls: ClassVar[list[tuple[Path, Path]]] = []
    lifecycle_calls: ClassVar[list[str]] = []
    logs_calls: ClassVar[list[int]] = []

    def __init__(self) -> None:
        type(self).instances.append(self)

    def status(self) -> SystemdUnitStatus:
        return self.status_result

    def install(
        self,
        *,
        environment_file: Path,
        runtime_executable: Path,
    ) -> SystemdLifecycleResult:
        type(self).install_calls.append((environment_file, runtime_executable))
        return _lifecycle("install", "installed")

    def start(self) -> SystemdLifecycleResult:
        type(self).lifecycle_calls.append("start")
        return _lifecycle("start", "started", active=True)

    def stop(self) -> SystemdLifecycleResult:
        type(self).lifecycle_calls.append("stop")
        return _lifecycle("stop", "stopped", active=False)

    def restart(self) -> SystemdLifecycleResult:
        type(self).lifecycle_calls.append("restart")
        return _lifecycle("restart", "restarted", active=True)

    def logs(self, *, lines: int) -> SystemdLogResult:
        type(self).logs_calls.append(lines)
        return SystemdLogResult(
            entries=(SystemdLogEntry(1720000000000000, 6, "ready", 42),),
            lines_requested=lines,
        )


def test_daemon_status_reports_runtime_readiness(monkeypatch) -> None:
    _install_fakes(monkeypatch)
    FakeRuntimeClient.status_result = RuntimeServiceStatus(
        endpoint="https://runtime.example",
        live=True,
        ready=True,
    )
    FakeSystemdManager.status_result = _service(active=True)

    result = runner.invoke(
        app,
        ["daemon", "status", "--endpoint", "https://runtime.example"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "endpoint": "https://runtime.example",
        "live": True,
        "ready": True,
        "runtime_error": None,
        "schema_version": "daemon-status/v1",
        "service": {
            "active": True,
            "available": True,
            "enabled": True,
            "installed": True,
            "last_exit_status": 0,
            "main_pid": 42,
            "manager": "systemd-user",
            "schema_version": "systemd-user-status/v1",
            "substate": "running",
            "unit": "blackcell.service",
        },
    }


def test_daemon_status_uses_environment_endpoint_and_fails_when_not_ready(monkeypatch) -> None:
    _install_fakes(monkeypatch)
    monkeypatch.setenv(RUNTIME_ENDPOINT_ENV, "https://runtime.internal")
    FakeRuntimeClient.status_result = RuntimeClientError(RuntimeClientFailureCode.CONNECTION_FAILED)
    FakeSystemdManager.status_result = _service(active=False)

    result = runner.invoke(app, ["daemon", "status"], catch_exceptions=False)

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["endpoint"] == "https://runtime.internal"
    assert payload["live"] is False and payload["ready"] is False
    assert payload["runtime_error"] == "runtime-connection-failed"
    assert payload["service"]["active"] is False
    assert result.stderr == ""


def test_daemon_install_and_lifecycle_commands_emit_typed_results(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _install_fakes(monkeypatch)
    environment_file = tmp_path / "runtime.env"
    executable = tmp_path / "blackcell-runtime"

    installed = runner.invoke(
        app,
        [
            "daemon",
            "install",
            "--environment-file",
            str(environment_file),
            "--runtime-executable",
            str(executable),
        ],
        catch_exceptions=False,
    )
    assert installed.exit_code == 0
    assert json.loads(installed.stdout)["outcome"] == "installed"
    assert FakeSystemdManager.install_calls == [(environment_file, executable)]

    outcomes = {}
    for operation in ("start", "stop", "restart"):
        result = runner.invoke(app, ["daemon", operation], catch_exceptions=False)
        assert result.exit_code == 0
        outcomes[operation] = json.loads(result.stdout)["outcome"]
    assert outcomes == {"start": "started", "stop": "stopped", "restart": "restarted"}
    assert FakeSystemdManager.lifecycle_calls == ["start", "stop", "restart"]


def test_daemon_logs_emit_bounded_entries(monkeypatch) -> None:
    _install_fakes(monkeypatch)

    result = runner.invoke(app, ["daemon", "logs", "--lines", "2"], catch_exceptions=False)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "daemon-logs/v1"
    assert payload["lines_requested"] == 2
    assert payload["entries"][0] == {
        "message": "ready",
        "pid": 42,
        "priority": 6,
        "timestamp_us": 1720000000000000,
        "truncated": False,
    }
    assert FakeSystemdManager.logs_calls == [2]


def test_daemon_foreground_delegates_to_the_canonical_runtime_entrypoint(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_runtime_main(arguments: tuple[str, ...]) -> int:
        calls.append(arguments)
        return 0

    monkeypatch.setattr("blackcell.cli.app.runtime_process_main", fake_runtime_main)

    result = runner.invoke(app, ["daemon", "foreground"], catch_exceptions=False)

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "operation": "foreground",
        "outcome": "stopped",
        "schema_version": "daemon-lifecycle/v1",
    }
    assert calls == [("daemon",)]


def test_daemon_help_exposes_status_without_legacy_submission() -> None:
    result = runner.invoke(app, ["daemon", "--help"], catch_exceptions=False)

    assert result.exit_code == 0
    for command in ("foreground", "install", "start", "stop", "restart", "status", "logs"):
        assert command in result.stdout
    assert "submit" not in result.stdout


def _install_fakes(monkeypatch) -> None:
    FakeRuntimeClient.instances = []
    FakeSystemdManager.instances = []
    FakeSystemdManager.install_calls = []
    FakeSystemdManager.lifecycle_calls = []
    FakeSystemdManager.logs_calls = []
    FakeSystemdManager.status_result = _service(active=False)
    FakeRuntimeClient.status_result = RuntimeServiceStatus(
        endpoint="https://runtime.example",
        live=True,
        ready=True,
    )
    monkeypatch.delenv(RUNTIME_ENDPOINT_ENV, raising=False)
    monkeypatch.setattr("blackcell.cli.app.RuntimeHttpClient", FakeRuntimeClient)
    monkeypatch.setattr("blackcell.cli.app.SystemdUserServiceManager", FakeSystemdManager)


def _service(*, active: bool) -> SystemdUnitStatus:
    return SystemdUnitStatus(
        available=True,
        installed=True,
        enabled=True,
        active=active,
        substate="running" if active else "dead",
        main_pid=42 if active else None,
        last_exit_status=0,
    )


def _lifecycle(
    operation: Literal["install", "start", "stop", "restart"],
    outcome: Literal["installed", "unchanged", "started", "stopped", "restarted"],
    *,
    active: bool = False,
) -> SystemdLifecycleResult:
    return SystemdLifecycleResult(
        operation=operation,
        outcome=outcome,
        service=_service(active=active),
    )
