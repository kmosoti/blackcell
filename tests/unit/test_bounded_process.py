from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from blackcell.adapters.bounded_process import (
    BoundedProcessError,
    BoundedProcessFailureCode,
    BoundedProcessRunner,
    BoundedStreamCapture,
)


def test_runner_uses_direct_argv_and_returns_complete_bounded_streams(tmp_path: Path) -> None:
    result = BoundedProcessRunner().run(
        (
            sys.executable,
            "-c",
            "import os,sys;sys.stdout.write(os.environ['MARKER']);sys.stderr.write('warn')",
        ),
        cwd=tmp_path,
        timeout_seconds=1.0,
        stdout_limit_bytes=8,
        stderr_limit_bytes=8,
        environment={"MARKER": "value"},
    )

    assert result.return_code == 0
    assert result.stdout == BoundedStreamCapture(b"value", 5, True)
    assert result.stderr == BoundedStreamCapture(b"warn", 4, True)


def test_runner_fails_typed_on_spawn_timeout_and_oversized_output(tmp_path: Path) -> None:
    runner = BoundedProcessRunner()

    with pytest.raises(BoundedProcessError) as spawn_failed:
        runner.run(
            (str(tmp_path / "missing-sensitive-command"),),
            cwd=tmp_path,
            timeout_seconds=1.0,
            stdout_limit_bytes=8,
            stderr_limit_bytes=8,
        )
    assert spawn_failed.value.code is BoundedProcessFailureCode.SPAWN_FAILED
    assert "sensitive" not in str(spawn_failed.value)

    with pytest.raises(BoundedProcessError) as timed_out:
        runner.run(
            (sys.executable, "-c", "import time;time.sleep(10)"),
            cwd=tmp_path,
            timeout_seconds=0.05,
            stdout_limit_bytes=8,
            stderr_limit_bytes=8,
        )
    assert timed_out.value.code is BoundedProcessFailureCode.TIMED_OUT

    with pytest.raises(BoundedProcessError) as oversized:
        runner.run(
            (sys.executable, "-c", "print('123456789', end='')"),
            cwd=tmp_path,
            timeout_seconds=1.0,
            stdout_limit_bytes=8,
            stderr_limit_bytes=8,
        )
    assert oversized.value.code is BoundedProcessFailureCode.OUTPUT_TOO_LARGE

    with pytest.raises(BoundedProcessError) as invalid:
        runner.run(
            (),
            cwd=tmp_path,
            timeout_seconds=1.0,
            stdout_limit_bytes=8,
            stderr_limit_bytes=8,
        )
    assert invalid.value.code is BoundedProcessFailureCode.INVALID_INVOCATION


def test_runner_cancellation_terminates_the_process_group(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    script = (
        "import pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='utf-8');"
        "time.sleep(30)"
    )

    with pytest.raises(BoundedProcessError) as canceled:
        BoundedProcessRunner().run(
            (sys.executable, "-c", script, str(child_pid_path)),
            cwd=tmp_path,
            timeout_seconds=5.0,
            stdout_limit_bytes=8,
            stderr_limit_bytes=8,
            cancel_requested=child_pid_path.exists,
        )

    assert canceled.value.code is BoundedProcessFailureCode.CANCELED
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 1.0
    while _process_is_live(child_pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _process_is_live(child_pid)


def _process_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2]
    except FileNotFoundError, ProcessLookupError:
        return False
    return state != "Z"
