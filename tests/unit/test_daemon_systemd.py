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
    default_systemd_user_unit_directory,
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


def test_daemon_errors_have_stable_cli_exit_classes() -> None:
    invalid_input = {
        SystemdServiceFailureCode.INVALID_ENVIRONMENT_FILE,
        SystemdServiceFailureCode.INVALID_EXECUTABLE,
        SystemdServiceFailureCode.INVALID_UNIT_DIRECTORY,
        SystemdServiceFailureCode.INVALID_LOG_LIMIT,
    }
    unavailable = {
        SystemdServiceFailureCode.UNSUPPORTED_PLATFORM,
        SystemdServiceFailureCode.MANAGER_UNAVAILABLE,
        SystemdServiceFailureCode.COMMAND_FAILED,
    }

    for code in SystemdServiceFailureCode:
        expected = 1 if code in invalid_input else 3 if code in unavailable else 4
        assert SystemdServiceError(code).cli_exit_code == expected


def test_status_rejects_untrusted_manager_responses() -> None:
    assert not SystemdUserServiceManager(runner=FakeRunner(), platform="darwin").status().available

    invalid_results = (
        BoundedProcessError(BoundedProcessFailureCode.OUTPUT_INCOMPLETE),
        _result(b"", return_code=1),
        _result(_status(), stderr=b"unexpected"),
        _result(b"\xff"),
        _result(_status() + b"LoadState=loaded\n"),
        _result(b"LoadState=loaded\n"),
        _result(_status(pid=-1)),
    )
    expected_codes = (
        SystemdServiceFailureCode.INVALID_RESPONSE,
        None,
        SystemdServiceFailureCode.INVALID_RESPONSE,
        SystemdServiceFailureCode.INVALID_RESPONSE,
        SystemdServiceFailureCode.INVALID_RESPONSE,
        SystemdServiceFailureCode.INVALID_RESPONSE,
        SystemdServiceFailureCode.INVALID_RESPONSE,
    )
    for result, expected_code in zip(invalid_results, expected_codes, strict=True):
        manager = SystemdUserServiceManager(runner=FakeRunner(result))
        if expected_code is None:
            assert not manager.status().available
        else:
            with pytest.raises(SystemdServiceError) as caught:
                manager.status()
            assert caught.value.code is expected_code


def test_control_commands_map_process_failures_and_state_mismatches() -> None:
    unavailable = SystemdUserServiceManager(runner=FakeRunner(_result(b"", return_code=1)))
    with pytest.raises(SystemdServiceError) as unavailable_error:
        unavailable.start()
    assert unavailable_error.value.code is SystemdServiceFailureCode.MANAGER_UNAVAILABLE

    missing = SystemdUserServiceManager(
        runner=FakeRunner(_result(_status(load="not-found", unit_file="")))
    )
    with pytest.raises(SystemdServiceError) as missing_error:
        missing.stop()
    assert missing_error.value.code is SystemdServiceFailureCode.COMMAND_FAILED

    mismatch = SystemdUserServiceManager(
        runner=FakeRunner(_result(_status()), _result(b""), _result(_status()))
    )
    with pytest.raises(SystemdServiceError) as mismatch_error:
        mismatch.restart()
    assert mismatch_error.value.code is SystemdServiceFailureCode.COMMAND_FAILED

    failure_codes = (
        (BoundedProcessFailureCode.SPAWN_FAILED, SystemdServiceFailureCode.MANAGER_UNAVAILABLE),
        (BoundedProcessFailureCode.OUTPUT_TOO_LARGE, SystemdServiceFailureCode.INVALID_RESPONSE),
        (BoundedProcessFailureCode.TIMED_OUT, SystemdServiceFailureCode.COMMAND_FAILED),
    )
    for process_code, expected_code in failure_codes:
        manager = SystemdUserServiceManager(
            runner=FakeRunner(_result(_status()), BoundedProcessError(process_code))
        )
        with pytest.raises(SystemdServiceError) as caught:
            manager.start()
        assert caught.value.code is expected_code

    nonzero = SystemdUserServiceManager(
        runner=FakeRunner(_result(_status()), _result(b"", return_code=1))
    )
    with pytest.raises(SystemdServiceError) as nonzero_error:
        nonzero.start()
    assert nonzero_error.value.code is SystemdServiceFailureCode.COMMAND_FAILED


def test_install_rolls_back_new_unit_on_control_or_postcondition_failure(tmp_path: Path) -> None:
    environment_file = _environment_file(tmp_path)
    executable = _runtime_executable()

    control_directory = tmp_path / "control-failure"
    control_runner = FakeRunner(
        _result(_status(load="not-found")),
        BoundedProcessError(BoundedProcessFailureCode.TIMED_OUT),
        _result(b""),
        _result(b""),
    )
    with pytest.raises(SystemdServiceError) as control_error:
        SystemdUserServiceManager(
            runner=control_runner,
            unit_directory=control_directory,
        ).install(environment_file=environment_file, runtime_executable=executable)
    assert control_error.value.code is SystemdServiceFailureCode.COMMAND_FAILED
    assert not (control_directory / SYSTEMD_UNIT_NAME).exists()

    postcondition_directory = tmp_path / "postcondition-failure"
    postcondition_runner = FakeRunner(
        _result(_status(load="not-found")),
        _result(b""),
        _result(b""),
        _result(_status(unit_file="disabled")),
        _result(b""),
        _result(b""),
    )
    with pytest.raises(SystemdServiceError) as postcondition_error:
        SystemdUserServiceManager(
            runner=postcondition_runner,
            unit_directory=postcondition_directory,
        ).install(environment_file=environment_file, runtime_executable=executable)
    assert postcondition_error.value.code is SystemdServiceFailureCode.INSTALL_FAILED
    assert not (postcondition_directory / SYSTEMD_UNIT_NAME).exists()


def test_logs_reject_unavailable_malformed_and_unbounded_journal_data() -> None:
    unavailable = SystemdUserServiceManager(runner=FakeRunner(_result(b"", return_code=1)))
    with pytest.raises(SystemdServiceError) as unavailable_error:
        unavailable.logs(lines=1)
    assert unavailable_error.value.code is SystemdServiceFailureCode.MANAGER_UNAVAILABLE

    malformed = (
        _result(b"{}", stderr=b"unexpected"),
        _result(b"not-json"),
        _result(b"[]"),
        _result(json.dumps({"__REALTIME_TIMESTAMP": "1"}).encode()),
        _result(json.dumps({"MESSAGE": "message", "__REALTIME_TIMESTAMP": "bad"}).encode()),
        _result(
            json.dumps(
                {"MESSAGE": "message", "__REALTIME_TIMESTAMP": "1", "PRIORITY": "8"}
            ).encode()
        ),
        _result(
            json.dumps(
                {
                    "MESSAGE": "message",
                    "__REALTIME_TIMESTAMP": "1",
                    "PRIORITY": "6",
                    "_PID": "bad",
                }
            ).encode()
        ),
        _result(
            b"\n".join(
                json.dumps({"MESSAGE": "message", "__REALTIME_TIMESTAMP": str(index)}).encode()
                for index in range(201)
            )
        ),
    )
    for result in malformed:
        manager = SystemdUserServiceManager(runner=FakeRunner(_result(_status()), result))
        with pytest.raises(SystemdServiceError) as caught:
            manager.logs(lines=1)
        assert caught.value.code is SystemdServiceFailureCode.INVALID_RESPONSE

    default_fields = SystemdUserServiceManager(
        runner=FakeRunner(
            _result(_status()),
            _result(
                b"\n" + json.dumps({"MESSAGE": "message", "__REALTIME_TIMESTAMP": "1"}).encode()
            ),
        )
    ).logs(lines=1)
    assert default_fields.entries[0].priority == 6
    assert default_fields.entries[0].pid is None


def test_install_validates_paths_permissions_platform_and_unit_conflicts(tmp_path: Path) -> None:
    environment_file = _environment_file(tmp_path)
    executable = _runtime_executable()

    invalid_environments = [Path("relative.env"), (tmp_path / "missing.env").resolve()]
    wrong_mode = tmp_path / "wrong-mode.env"
    wrong_mode.write_text("VALUE=1\n")
    wrong_mode.chmod(0o644)
    invalid_environments.append(wrong_mode.resolve())
    environment_directory = tmp_path / "environment-directory"
    environment_directory.mkdir()
    environment_directory.chmod(0o600)
    invalid_environments.append(environment_directory.resolve())
    environment_link = tmp_path / "environment-link"
    environment_link.symlink_to(environment_file)
    invalid_environments.append(environment_link)
    for value in invalid_environments:
        with pytest.raises(SystemdServiceError) as caught:
            SystemdUserServiceManager(runner=FakeRunner()).install(
                environment_file=value,
                runtime_executable=executable,
            )
        assert caught.value.code is SystemdServiceFailureCode.INVALID_ENVIRONMENT_FILE

    non_executable = tmp_path / "not-executable"
    non_executable.write_text("#!/bin/sh\n")
    non_executable.chmod(0o600)
    invalid_executables = [Path("relative"), (tmp_path / "missing-bin").resolve(), non_executable]
    for value in invalid_executables:
        with pytest.raises(SystemdServiceError) as caught:
            SystemdUserServiceManager(runner=FakeRunner()).install(
                environment_file=environment_file,
                runtime_executable=value,
            )
        assert caught.value.code is SystemdServiceFailureCode.INVALID_EXECUTABLE

    with pytest.raises(SystemdServiceError) as unsupported:
        SystemdUserServiceManager(runner=FakeRunner(), platform="darwin").install(
            environment_file=environment_file,
            runtime_executable=executable,
        )
    assert unsupported.value.code is SystemdServiceFailureCode.UNSUPPORTED_PLATFORM

    with pytest.raises(SystemdServiceError) as relative_directory:
        SystemdUserServiceManager(
            runner=FakeRunner(_result(_status())),
            unit_directory=Path("relative-systemd"),
        ).install(environment_file=environment_file, runtime_executable=executable)
    assert relative_directory.value.code is SystemdServiceFailureCode.INVALID_UNIT_DIRECTORY

    file_directory = tmp_path / "not-a-directory"
    file_directory.write_text("preserve")
    with pytest.raises(SystemdServiceError) as invalid_directory:
        SystemdUserServiceManager(
            runner=FakeRunner(_result(_status())),
            unit_directory=file_directory,
        ).install(environment_file=environment_file, runtime_executable=executable)
    assert invalid_directory.value.code is SystemdServiceFailureCode.INVALID_UNIT_DIRECTORY

    conflict_directory = tmp_path / "symlink-conflict"
    conflict_directory.mkdir()
    (conflict_directory / SYSTEMD_UNIT_NAME).symlink_to(environment_file)
    with pytest.raises(SystemdServiceError) as symlink_conflict:
        SystemdUserServiceManager(
            runner=FakeRunner(_result(_status(load="not-found"))),
            unit_directory=conflict_directory,
        ).install(environment_file=environment_file, runtime_executable=executable)
    assert symlink_conflict.value.code is SystemdServiceFailureCode.UNIT_CONFLICT


def test_default_unit_directory_and_unit_quoting_are_fail_closed(tmp_path: Path) -> None:
    configured = tmp_path / "xdg"
    assert default_systemd_user_unit_directory({"XDG_CONFIG_HOME": str(configured)}) == (
        configured / "systemd/user"
    )
    assert default_systemd_user_unit_directory({}).parts[-2:] == ("systemd", "user")
    with pytest.raises(SystemdServiceError) as relative:
        default_systemd_user_unit_directory({"XDG_CONFIG_HOME": "relative"})
    assert relative.value.code is SystemdServiceFailureCode.INVALID_UNIT_DIRECTORY

    unit = render_systemd_user_unit(Path('/opt/black%cell"\\runtime'), Path("/tmp/runtime.env"))
    assert 'ExecStart="/opt/black%%cell\\"\\\\runtime" daemon' in unit
    with pytest.raises(SystemdServiceError) as control_character:
        render_systemd_user_unit(Path("/opt/blackcell\nruntime"), Path("/tmp/runtime.env"))
    assert control_character.value.code is SystemdServiceFailureCode.INSTALL_FAILED


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
