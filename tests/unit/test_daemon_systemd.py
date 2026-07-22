from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from blackcell.adapters.bounded_process import (
    BoundedProcessError,
    BoundedProcessFailureCode,
    BoundedProcessResult,
    BoundedStreamCapture,
)
from blackcell.adapters.daemon_systemd import (
    SYSTEMD_UNIT_NAME,
    SystemdServiceError,
    SystemdServiceFailureCode,
    SystemdUserServiceManager,
    render_systemd_user_unit,
)


@dataclass(frozen=True, slots=True)
class RecordedCommand:
    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: float
    stdout_limit_bytes: int
    stderr_limit_bytes: int


class FakeRunner:
    def __init__(self, *results: BoundedProcessResult | BoundedProcessError) -> None:
        self.results = list(results)
        self.commands: list[RecordedCommand] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        environment=None,
    ) -> BoundedProcessResult:
        assert environment is None
        self.commands.append(
            RecordedCommand(
                argv,
                cwd,
                timeout_seconds,
                stdout_limit_bytes,
                stderr_limit_bytes,
            )
        )
        result = self.results.pop(0)
        if isinstance(result, BoundedProcessError):
            raise result
        return result


def test_install_is_atomic_idempotent_and_enables_one_foreground_unit(tmp_path: Path) -> None:
    environment_file = _environment_file(tmp_path)
    executable = _runtime_executable()
    unit_directory = tmp_path / "systemd/user"
    first_runner = FakeRunner(
        _result(_status(load="not-found")),
        _result(b""),
        _result(b""),
        _result(_status(unit_file="enabled")),
    )
    manager = SystemdUserServiceManager(runner=first_runner, unit_directory=unit_directory)

    installed = manager.install(
        environment_file=environment_file,
        runtime_executable=executable,
    )

    unit_path = unit_directory / SYSTEMD_UNIT_NAME
    content = unit_path.read_text(encoding="utf-8")
    assert installed.outcome == "installed"
    assert installed.service.installed and installed.service.enabled
    assert installed.unit_path == unit_path
    assert installed.unit_digest is not None and installed.unit_digest.startswith("sha256:")
    assert f'ExecStart="{executable.resolve()}" daemon' in content
    assert f'EnvironmentFile="{environment_file}"' in content
    assert "KillMode=control-group" in content
    assert "PIDFile" not in content
    assert [command.argv[4] for command in first_runner.commands[1:3]] == [
        "daemon-reload",
        "enable",
    ]

    second_runner = FakeRunner(
        _result(_status(unit_file="enabled")),
        _result(b""),
        _result(b""),
        _result(_status(unit_file="enabled")),
    )
    unchanged = SystemdUserServiceManager(
        runner=second_runner,
        unit_directory=unit_directory,
    ).install(environment_file=environment_file, runtime_executable=executable)

    assert unchanged.outcome == "unchanged"
    assert unchanged.unit_digest == installed.unit_digest
    assert unit_path.read_text(encoding="utf-8") == content


def test_install_checks_manager_before_writing_and_rejects_conflicts(tmp_path: Path) -> None:
    environment_file = _environment_file(tmp_path)
    executable = _runtime_executable()
    unavailable_directory = tmp_path / "unavailable/systemd"
    unavailable = SystemdUserServiceManager(
        runner=FakeRunner(
            BoundedProcessError(BoundedProcessFailureCode.SPAWN_FAILED),
        ),
        unit_directory=unavailable_directory,
    )

    with pytest.raises(SystemdServiceError) as missing_manager:
        unavailable.install(
            environment_file=environment_file,
            runtime_executable=executable,
        )
    assert missing_manager.value.code is SystemdServiceFailureCode.MANAGER_UNAVAILABLE
    assert not unavailable_directory.exists()

    conflict_directory = tmp_path / "conflict/systemd"
    conflict_directory.mkdir(parents=True)
    unit_path = conflict_directory / SYSTEMD_UNIT_NAME
    unit_path.write_text("preserve\n", encoding="utf-8")
    conflicting = SystemdUserServiceManager(
        runner=FakeRunner(_result(_status(load="not-found"))),
        unit_directory=conflict_directory,
    )
    with pytest.raises(SystemdServiceError) as conflict:
        conflicting.install(
            environment_file=environment_file,
            runtime_executable=executable,
        )
    assert conflict.value.code is SystemdServiceFailureCode.UNIT_CONFLICT
    assert unit_path.read_text(encoding="utf-8") == "preserve\n"


def test_start_stop_and_restart_require_expected_typed_states() -> None:
    cases = (
        ("start", _status(), _status(active="active", substate="running", pid=41), "started"),
        ("stop", _status(active="active", substate="running", pid=41), _status(), "stopped"),
        (
            "restart",
            _status(active="active", substate="running", pid=41),
            _status(active="active", substate="running", pid=52),
            "restarted",
        ),
    )
    for operation, before, after, expected_outcome in cases:
        runner = FakeRunner(_result(before), _result(b""), _result(after))
        manager = SystemdUserServiceManager(runner=runner)

        result = getattr(manager, operation)()

        assert result.operation == operation
        assert result.outcome == expected_outcome
        assert result.service.active is (operation != "stop")
        assert runner.commands[1].argv[4:6] == (operation, SYSTEMD_UNIT_NAME)


def test_status_does_not_treat_a_stale_main_pid_as_active() -> None:
    manager = SystemdUserServiceManager(
        runner=FakeRunner(
            _result(
                _status(
                    active="failed",
                    substate="failed",
                    pid=9876,
                    exit_status=7,
                )
            )
        )
    )

    status = manager.status()

    assert status.available and status.installed
    assert not status.active
    assert status.main_pid is None
    assert status.last_exit_status == 7


def test_logs_are_bounded_and_typed() -> None:
    log_output = b"\n".join(
        (
            json.dumps(
                {
                    "MESSAGE": "started",
                    "__REALTIME_TIMESTAMP": "1720000000000000",
                    "PRIORITY": "6",
                    "_PID": "42",
                }
            ).encode(),
            json.dumps(
                {
                    "MESSAGE": "x" * 5000,
                    "__REALTIME_TIMESTAMP": "1720000000000001",
                    "PRIORITY": "4",
                }
            ).encode(),
        )
    )
    runner = FakeRunner(_result(_status()), _result(log_output))

    result = SystemdUserServiceManager(runner=runner).logs(lines=2)

    assert result.lines_requested == 2
    assert result.entries[0].message == "started"
    assert result.entries[0].pid == 42
    assert result.entries[1].truncated
    assert len(result.entries[1].message.encode()) == 4096
    assert runner.commands[1].argv[-2:] == ("--lines", "2")

    with pytest.raises(SystemdServiceError) as invalid:
        SystemdUserServiceManager(runner=FakeRunner()).logs(lines=201)
    assert invalid.value.code is SystemdServiceFailureCode.INVALID_LOG_LIMIT

    with pytest.raises(SystemdServiceError) as not_installed:
        SystemdUserServiceManager(
            runner=FakeRunner(_result(_status(load="not-found", unit_file="")))
        ).logs(lines=1)
    assert not_installed.value.code is SystemdServiceFailureCode.COMMAND_FAILED


def test_rendered_unit_is_valid_and_has_no_pid_authority(tmp_path: Path) -> None:
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:  # pragma: no cover - Linux systemd acceptance host guard
        pytest.skip("systemd-analyze is unavailable")
    environment_file = _environment_file(tmp_path)
    unit = render_systemd_user_unit(_runtime_executable(), environment_file)
    unit_path = tmp_path / SYSTEMD_UNIT_NAME
    unit_path.write_text(unit, encoding="utf-8")

    checked = subprocess.run(
        [analyzer, "--user", "verify", str(unit_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert checked.returncode == 0, checked.stderr
    assert unit.count("ExecStart=") == 1
    assert " daemon\n" in unit
    assert "PIDFile=" not in unit
    assert "Type=forking" not in unit


def _environment_file(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.env"
    path.write_text("BLACKCELL_DATA_DIR=/tmp/blackcell-test\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _runtime_executable() -> Path:
    executable = shutil.which("blackcell-runtime")
    assert executable is not None
    return Path(executable)


def _status(
    *,
    load: str = "loaded",
    active: str = "inactive",
    substate: str = "dead",
    unit_file: str = "enabled",
    pid: int = 0,
    exit_status: int = 0,
) -> bytes:
    return (
        f"MainPID={pid}\n"
        f"ExecMainStatus={exit_status}\n"
        f"LoadState={load}\n"
        f"ActiveState={active}\n"
        f"SubState={substate}\n"
        f"UnitFileState={unit_file}\n"
    ).encode()


def _result(stdout: bytes, *, return_code: int = 0, stderr: bytes = b"") -> BoundedProcessResult:
    return BoundedProcessResult(
        return_code,
        BoundedStreamCapture(stdout, len(stdout), True),
        BoundedStreamCapture(stderr, len(stderr), True),
    )
