from __future__ import annotations

import errno
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import cast

import pytest

from blackcell.adapters.execution.local_process import (
    LOCAL_PROCESS_ADAPTER_ID,
    LocalProcessAffordance,
    LocalProcessConfigurationError,
    LocalProcessRunner,
    ProcessCompletion,
    ProcessRun,
    StreamCapture,
)
from blackcell.features.execute_affordance import (
    AffordanceDefinition,
    SideEffectClass,
    UncertainExecutionError,
)


def test_runner_concurrently_drains_large_streams_with_exact_caps_and_full_hashes(
    tmp_path: Path,
) -> None:
    stdout = b"A" * 240_001
    stderr = b"B" * 220_003
    executable = _script(
        tmp_path,
        "large_streams",
        """
import os
import threading

left = threading.Thread(target=lambda: os.write(1, b"A" * 240_001))
right = threading.Thread(target=lambda: os.write(2, b"B" * 220_003))
left.start()
right.start()
left.join()
right.join()
""",
    )
    configuration = _configuration(
        tmp_path,
        executable,
        stdout_limit=4096,
        stderr_limit=3072,
    )

    result = LocalProcessRunner().run(configuration, _argv(configuration), {})

    assert result.completion is ProcessCompletion.EXITED
    assert result.return_code == 0
    assert result.stdout.captured == stdout[:4096]
    assert result.stdout.total_bytes == len(stdout)
    assert result.stdout.content_digest == _digest(stdout)
    assert result.stdout.eof and result.stdout.truncated
    assert result.stderr.captured == stderr[:3072]
    assert result.stderr.total_bytes == len(stderr)
    assert result.stderr.content_digest == _digest(stderr)
    assert result.stderr.eof and result.stderr.truncated


@pytest.mark.parametrize(
    ("body", "completion", "return_code", "signal_number"),
    (
        ("raise SystemExit(7)", ProcessCompletion.EXITED, 7, None),
        (
            "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
            ProcessCompletion.SIGNALED,
            -signal.SIGTERM,
            signal.SIGTERM,
        ),
    ),
)
def test_runner_preserves_nonzero_and_signal_status(
    tmp_path: Path,
    body: str,
    completion: ProcessCompletion,
    return_code: int,
    signal_number: int | None,
) -> None:
    executable = _script(tmp_path, "terminal", body)
    configuration = _configuration(tmp_path, executable)

    result = LocalProcessRunner().run(configuration, _argv(configuration), {})

    assert result.completion is completion
    assert result.return_code == return_code
    assert result.signal_number == signal_number
    assert result.stdout.eof and result.stderr.eof


def test_runner_times_out_terminates_and_reaps_the_process_group(tmp_path: Path) -> None:
    executable = _script(
        tmp_path,
        "timeout",
        "import time; time.sleep(60)",
    )
    configuration = _configuration(
        tmp_path,
        executable,
        timeout=0.05,
        termination_grace=0.1,
    )

    result = LocalProcessRunner().run(configuration, _argv(configuration), {})

    assert result.completion is ProcessCompletion.TIMED_OUT
    assert result.return_code is not None
    assert result.stdout.eof and result.stderr.eof


def test_runner_raises_uncertain_when_termination_cannot_be_confirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _script(tmp_path, "uncertain", "import time; time.sleep(60)")
    configuration = _configuration(tmp_path, executable, timeout=0.02)
    from blackcell.adapters.execution.local_process import runner as runner_module

    def uncertain(process: subprocess.Popen[bytes], _grace: float) -> None:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=1)
        raise UncertainExecutionError("cannot confirm termination")

    monkeypatch.setattr(runner_module, "_terminate_process_group", uncertain)
    with pytest.raises(UncertainExecutionError, match="cannot confirm"):
        LocalProcessRunner().run(configuration, _argv(configuration), {})


def test_runner_passes_empty_environment_and_devnull_stdin(tmp_path: Path) -> None:
    executable = _script(
        tmp_path,
        "environment",
        """
import os
import sys

payload = f"{os.environ.get('SAFE_VALUE')}|{os.environ.get('PATH')}|{len(sys.stdin.buffer.read())}"
os.write(1, payload.encode())
""",
    )
    configuration = _configuration(tmp_path, executable)

    result = LocalProcessRunner().run(configuration, _argv(configuration), {})

    assert result.stdout.captured == b"None|None|0"
    assert result.return_code == 0


def test_runner_records_spawn_failure_without_claiming_a_return_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _script(tmp_path, "spawn", "pass")
    configuration = _configuration(tmp_path, executable)
    from blackcell.adapters.execution.local_process import runner as runner_module

    def fail_spawn(*_args: object, **_kwargs: object) -> object:
        raise OSError(errno.EAGAIN, "bounded test")

    monkeypatch.setattr(runner_module.subprocess, "Popen", fail_spawn)

    result = LocalProcessRunner().run(configuration, _argv(configuration), {})

    assert result.completion is ProcessCompletion.SPAWN_FAILED
    assert result.return_code is None
    assert result.spawn_errno == errno.EAGAIN
    assert result.stdout.total_bytes == result.stderr.total_bytes == 0
    assert result.stdout.content_digest == _digest(b"")


def test_runner_uses_fd_pinned_non_shell_bounded_popen_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _script(tmp_path, "contract", "pass")
    configuration = _configuration(tmp_path, executable)
    captured: dict[str, object] = {}
    from blackcell.adapters.execution.local_process import runner as runner_module

    class FakeProcess:
        pid = 987654321
        stdout = BytesIO(b"stdout")
        stderr = BytesIO(b"stderr")
        returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            captured["timeout"] = timeout
            return 0

    def fake_popen(argv: list[str], **kwargs: object) -> FakeProcess:
        captured["argv"] = argv
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)

    result = LocalProcessRunner().run(configuration, _argv(configuration), {})

    assert result.return_code == 0
    assert captured["argv"] == list(_argv(configuration))
    assert str(captured["executable"]).startswith("/proc/self/fd/")
    assert str(captured["cwd"]).startswith("/proc/self/fd/")
    assert captured["env"] == {}
    assert captured["stdin"] is runner_module.subprocess.DEVNULL
    assert captured["stdout"] is runner_module.subprocess.PIPE
    assert captured["stderr"] is runner_module.subprocess.PIPE
    assert captured["shell"] is False
    assert captured["text"] is False
    assert captured["close_fds"] is True
    pass_fds = captured["pass_fds"]
    assert isinstance(pass_fds, tuple) and len(pass_fds) == 2
    assert captured["start_new_session"] is True


def test_stream_capture_validation_paths() -> None:
    with pytest.raises(ValueError, match="requires"):
        StreamCapture(b"x", None, None, True, False)
    with pytest.raises(ValueError, match="smaller"):
        StreamCapture(b"xx", 1, _digest(b"x"), True, True)
    with pytest.raises(ValueError, match="truncation"):
        StreamCapture(b"x", 1, _digest(b"x"), True, True)
    with pytest.raises(ValueError, match="cannot claim"):
        StreamCapture(b"x", 1, _digest(b"x"), False, True)


def test_process_run_rejects_incoherent_status_metadata() -> None:
    now = datetime(2026, 7, 11, 12, tzinfo=UTC)
    empty = StreamCapture(b"", 0, _digest(b""), True, False)
    base = ProcessRun(ProcessCompletion.EXITED, now, now, 0, None, empty, empty)
    cases = (
        ({"started_at": datetime(2026, 7, 11, 12)}, "start"),
        ({"completed_at": datetime(2026, 7, 11, 12)}, "completion"),
        ({"completed_at": now - timedelta(seconds=1)}, "precede"),
        (
            {"completion": ProcessCompletion.SPAWN_FAILED, "return_code": 0},
            "spawn failure",
        ),
        ({"return_code": None}, "requires a return"),
        (
            {
                "completion": ProcessCompletion.SIGNALED,
                "return_code": -9,
                "signal_number": 15,
            },
            "does not match",
        ),
        ({"return_code": 0, "signal_number": 9}, "cannot claim"),
        ({"completion": cast("ProcessCompletion", "unknown")}, "recognized"),
        ({"spawn_errno": -1}, "spawn errno"),
    )
    for changes, message in cases:
        with pytest.raises(ValueError, match=message):
            replace(base, **changes)


def test_runner_revalidates_argv_environment_executable_and_clock(tmp_path: Path) -> None:
    executable = _mutable_script(tmp_path, "runtime_validation", "pass")
    configuration = _configuration(
        tmp_path,
        executable,
    )
    runner = LocalProcessRunner()

    with pytest.raises(LocalProcessConfigurationError, match="argv executable"):
        runner.run(configuration, (), {})
    with pytest.raises(LocalProcessConfigurationError, match="invalid token"):
        runner.run(configuration, (*_argv(configuration), "bad\x00token"), {})
    with pytest.raises(LocalProcessConfigurationError, match=r"environment.*empty"):
        runner.run(configuration, _argv(configuration), {"SAFE": "fixed"})

    executable.binary.chmod(0o600)
    with pytest.raises(LocalProcessConfigurationError, match="no longer executable"):
        runner.run(configuration, _argv(configuration), {})
    executable.binary.chmod(0o700)

    naive = LocalProcessRunner(clock=lambda: datetime(2026, 7, 11, 12))
    with pytest.raises(ValueError, match="timezone-aware"):
        naive.run(configuration, _argv(configuration), {})

    with executable.binary.open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(LocalProcessConfigurationError, match="content has changed"):
        runner.run(configuration, _argv(configuration), {})


def test_runner_marks_output_incomplete_when_a_descendant_keeps_pipes_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _script(tmp_path, "incomplete", "pass")
    configuration = replace(
        _configuration(tmp_path, executable),
        drain_grace_seconds=0.01,
    )
    from blackcell.adapters.execution.local_process import runner as runner_module

    class BlockingPipe:
        def __init__(self) -> None:
            self.closed = threading.Event()

        def read(self, _size: int = -1) -> bytes:
            self.closed.wait(1)
            raise OSError("pipe closed without EOF")

        def close(self) -> None:
            self.closed.set()

    class FakeProcess:
        pid = 987654321
        stdout = BlockingPipe()
        stderr = BlockingPipe()
        returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

    monkeypatch.setattr(runner_module.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(runner_module, "_terminate_remaining_group", lambda *_args: None)

    result = LocalProcessRunner().run(configuration, _argv(configuration), {})

    assert result.completion is ProcessCompletion.OUTPUT_INCOMPLETE
    assert not result.stdout.eof and result.stdout.total_bytes is None
    assert not result.stderr.eof and result.stderr.content_digest is None


def test_fd_pinned_spawn_rejects_executable_and_cwd_inode_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blackcell.adapters.execution.local_process import runner as runner_module

    def must_not_spawn(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Popen must not observe a replaced filesystem object")

    monkeypatch.setattr(runner_module.subprocess, "Popen", must_not_spawn)
    executable = _mutable_script(tmp_path, "pinned-python", "pass")
    executable_configuration = _configuration(tmp_path, executable)
    original_executable = tmp_path / "original-python"
    executable.binary.rename(original_executable)
    shutil.copy2(Path(sys.executable).resolve(), executable.binary)
    executable.binary.chmod(0o700)

    with pytest.raises(LocalProcessConfigurationError, match="executable identity has changed"):
        LocalProcessRunner().run(
            executable_configuration,
            _argv(executable_configuration),
            {},
        )

    cwd = tmp_path / "cwd"
    cwd.mkdir(mode=0o700)
    cwd_configuration = replace(
        _configuration(cwd, _script(tmp_path, "cwd-program", "pass")),
        allowed_path_roots=(str(tmp_path),),
    )
    original_cwd = tmp_path / "original-cwd"
    cwd.rename(original_cwd)
    cwd.mkdir(mode=0o700)

    with pytest.raises(LocalProcessConfigurationError, match="working directory identity"):
        LocalProcessRunner().run(cwd_configuration, _argv(cwd_configuration), {})


def _configuration(
    root: Path,
    executable: _TrustedProgram,
    *,
    timeout: float = 1.0,
    stdout_limit: int = 4096,
    stderr_limit: int = 4096,
    termination_grace: float = 0.2,
) -> LocalProcessAffordance:
    return LocalProcessAffordance(
        definition=AffordanceDefinition(
            "probe",
            LOCAL_PROCESS_ADAPTER_ID,
            SideEffectClass.READ_ONLY,
            timeout,
        ),
        executable=str(executable.binary.resolve()),
        fixed_argv=("-I", "-S", "-c", f"exec({executable.body!r})"),
        bindings=(),
        working_directory=str(root.resolve()),
        allowed_path_roots=(str(root.resolve()),),
        stdout_limit_bytes=stdout_limit,
        stderr_limit_bytes=stderr_limit,
        termination_grace_seconds=termination_grace,
        drain_grace_seconds=0.2,
    )


@dataclass(frozen=True, slots=True)
class _TrustedProgram:
    binary: Path
    body: str


def _script(root: Path, name: str, body: str) -> _TrustedProgram:
    del root, name
    return _TrustedProgram(Path(sys.executable).resolve(), body)


def _mutable_script(root: Path, name: str, body: str) -> _TrustedProgram:
    path = root / name
    shutil.copy2(Path(sys.executable).resolve(), path)
    path.chmod(0o700)
    return _TrustedProgram(path, body)


def _argv(configuration: LocalProcessAffordance) -> tuple[str, ...]:
    return (configuration.executable, *configuration.fixed_argv)


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"
