"""Direct-argv subprocess execution with bounded capture and deadline cleanup."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

_READ_SIZE = 64 * 1024
_MAX_TIMEOUT_SECONDS = 600.0
_MAX_CAPTURE_BYTES = 16 * 1024 * 1024
_DRAIN_GRACE_SECONDS = 1.0
_TERMINATION_GRACE_SECONDS = 0.5
_PROCESS_POLL_SECONDS = 0.05


class BoundedProcessFailureCode(StrEnum):
    INVALID_INVOCATION = "invalid-process-invocation"
    SPAWN_FAILED = "process-spawn-failed"
    CANCELED = "process-canceled"
    TIMED_OUT = "process-timed-out"
    OUTPUT_TOO_LARGE = "process-output-too-large"
    OUTPUT_INCOMPLETE = "process-output-incomplete"


class BoundedProcessError(RuntimeError):
    """A stable process failure that never includes argv, paths, or stream content."""

    def __init__(self, code: BoundedProcessFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class BoundedStreamCapture:
    captured: bytes = field(repr=False)
    total_bytes: int | None
    complete: bool

    def __post_init__(self) -> None:
        if not isinstance(self.captured, bytes):
            raise ValueError("captured stream must be bytes")
        if not isinstance(self.complete, bool):
            raise ValueError("stream completion must be a boolean")
        if self.complete:
            if (
                isinstance(self.total_bytes, bool)
                or not isinstance(self.total_bytes, int)
                or self.total_bytes < len(self.captured)
            ):
                raise ValueError("complete stream total is invalid")
        elif self.total_bytes is not None:
            raise ValueError("incomplete stream cannot claim an exact total")


@dataclass(frozen=True, slots=True)
class BoundedProcessResult:
    return_code: int
    stdout: BoundedStreamCapture
    stderr: BoundedStreamCapture

    def __post_init__(self) -> None:
        if isinstance(self.return_code, bool) or not isinstance(self.return_code, int):
            raise ValueError("process return code must be an integer")


class BoundedProcessRunner:
    """Run one subprocess without a shell and retain at most the declared stream budgets."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        environment: Mapping[str, str] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> BoundedProcessResult:
        _validate_invocation(
            argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=stderr_limit_bytes,
            environment=environment,
            cancel_requested=cancel_requested,
            pass_fds=pass_fds,
        )
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=None if environment is None else dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                text=False,
                close_fds=True,
                pass_fds=pass_fds,
                start_new_session=True,
            )
        except OSError as error:
            raise BoundedProcessError(BoundedProcessFailureCode.SPAWN_FAILED) from error

        stdout = process.stdout
        stderr = process.stderr
        if stdout is None or stderr is None:  # pragma: no cover - subprocess contract guard
            _terminate_process_group(process)
            raise BoundedProcessError(BoundedProcessFailureCode.OUTPUT_INCOMPLETE)

        stdout_drain = _DrainCollector(cast("_ReadablePipe", stdout), stdout_limit_bytes)
        stderr_drain = _DrainCollector(cast("_ReadablePipe", stderr), stderr_limit_bytes)
        drains = (
            threading.Thread(
                target=stdout_drain.drain,
                name="blackcell-process-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=stderr_drain.drain,
                name="blackcell-process-stderr",
                daemon=True,
            ),
        )
        for drain in drains:
            drain.start()

        timed_out = False
        canceled = False
        cancellation_error: Exception | None = None
        deadline = time.monotonic() + timeout_seconds
        while (return_code := process.poll()) is None:
            if cancel_requested is not None:
                try:
                    requested = cancel_requested()
                    if not isinstance(requested, bool):
                        raise TypeError
                except Exception as error:
                    cancellation_error = error
                    _terminate_process_group(process)
                    return_code = process.returncode
                    break
                if requested:
                    canceled = True
                    _terminate_process_group(process)
                    return_code = process.returncode
                    break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_process_group(process)
                return_code = process.returncode
                break
            try:
                return_code = process.wait(timeout=min(_PROCESS_POLL_SECONDS, remaining))
            except subprocess.TimeoutExpired:
                continue

        _join_drains(drains, _DRAIN_GRACE_SECONDS)
        if any(drain.is_alive() for drain in drains):
            _terminate_process_group(process, force=True)
            stdout_drain.close()
            stderr_drain.close()
            _join_drains(drains, _TERMINATION_GRACE_SECONDS)
            if canceled:
                raise BoundedProcessError(BoundedProcessFailureCode.CANCELED)
            if timed_out:
                raise BoundedProcessError(BoundedProcessFailureCode.TIMED_OUT)
            raise BoundedProcessError(BoundedProcessFailureCode.OUTPUT_INCOMPLETE)

        if cancellation_error is not None:
            raise BoundedProcessError(
                BoundedProcessFailureCode.OUTPUT_INCOMPLETE
            ) from cancellation_error
        if canceled:
            raise BoundedProcessError(BoundedProcessFailureCode.CANCELED)
        if timed_out:
            raise BoundedProcessError(BoundedProcessFailureCode.TIMED_OUT)
        if return_code is None:  # pragma: no cover - wait contract guard
            raise BoundedProcessError(BoundedProcessFailureCode.OUTPUT_INCOMPLETE)

        result = BoundedProcessResult(
            return_code=return_code,
            stdout=stdout_drain.snapshot(),
            stderr=stderr_drain.snapshot(),
        )
        if (
            cast("int", result.stdout.total_bytes) > stdout_limit_bytes
            or cast("int", result.stderr.total_bytes) > stderr_limit_bytes
        ):
            raise BoundedProcessError(BoundedProcessFailureCode.OUTPUT_TOO_LARGE)
        return result


class _ReadablePipe(Protocol):
    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


class _DrainCollector:
    def __init__(self, stream: _ReadablePipe, capture_limit: int) -> None:
        self._stream = stream
        self._capture_limit = capture_limit
        self._captured = bytearray()
        self._total = 0
        self._complete = False
        self._lock = threading.Lock()

    def drain(self) -> None:
        complete = False
        try:
            while True:
                chunk = self._stream.read(_READ_SIZE)
                if not chunk:
                    complete = True
                    break
                with self._lock:
                    self._total += len(chunk)
                    remaining = self._capture_limit - len(self._captured)
                    if remaining > 0:
                        self._captured.extend(chunk[:remaining])
        except OSError, ValueError:
            complete = False
        finally:
            with self._lock:
                self._complete = complete
            with suppress(OSError):
                self._stream.close()

    def close(self) -> None:
        with suppress(OSError):
            self._stream.close()

    def snapshot(self) -> BoundedStreamCapture:
        with self._lock:
            return BoundedStreamCapture(
                captured=bytes(self._captured),
                total_bytes=self._total if self._complete else None,
                complete=self._complete,
            )


def _validate_invocation(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    timeout_seconds: float,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
    environment: Mapping[str, str] | None,
    cancel_requested: Callable[[], bool] | None,
    pass_fds: tuple[int, ...],
) -> None:
    if not argv or not all(
        isinstance(token, str) and token and "\x00" not in token for token in argv
    ):
        raise BoundedProcessError(BoundedProcessFailureCode.INVALID_INVOCATION)
    if not isinstance(cwd, Path) or not cwd.is_dir():
        raise BoundedProcessError(BoundedProcessFailureCode.INVALID_INVOCATION)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int | float)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS
    ):
        raise BoundedProcessError(BoundedProcessFailureCode.INVALID_INVOCATION)
    for limit in (stdout_limit_bytes, stderr_limit_bytes):
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_CAPTURE_BYTES
        ):
            raise BoundedProcessError(BoundedProcessFailureCode.INVALID_INVOCATION)
    if environment is not None and not all(
        isinstance(key, str)
        and key
        and "\x00" not in key
        and isinstance(value, str)
        and "\x00" not in value
        for key, value in environment.items()
    ):
        raise BoundedProcessError(BoundedProcessFailureCode.INVALID_INVOCATION)
    if cancel_requested is not None and not callable(cancel_requested):
        raise BoundedProcessError(BoundedProcessFailureCode.INVALID_INVOCATION)
    if (
        not isinstance(pass_fds, tuple)
        or len(pass_fds) != len(set(pass_fds))
        or any(isinstance(fd, bool) or not isinstance(fd, int) or fd < 0 for fd in pass_fds)
    ):
        raise BoundedProcessError(BoundedProcessFailureCode.INVALID_INVOCATION)
    try:
        for fd in pass_fds:
            os.fstat(fd)
    except OSError as error:
        raise BoundedProcessError(BoundedProcessFailureCode.INVALID_INVOCATION) from error


def _join_drains(drains: tuple[threading.Thread, threading.Thread], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    for drain in drains:
        drain.join(max(0.0, deadline - time.monotonic()))


def _terminate_process_group(process: subprocess.Popen[bytes], *, force: bool = False) -> None:
    requested_signal = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(process.pid, requested_signal)
    except ProcessLookupError:
        pass
    except OSError as error:
        raise BoundedProcessError(BoundedProcessFailureCode.OUTPUT_INCOMPLETE) from error
    if process.poll() is not None:
        return
    try:
        process.wait(timeout=_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        if force:
            raise BoundedProcessError(BoundedProcessFailureCode.OUTPUT_INCOMPLETE) from None
        _terminate_process_group(process, force=True)


__all__ = [
    "BoundedProcessError",
    "BoundedProcessFailureCode",
    "BoundedProcessResult",
    "BoundedProcessRunner",
    "BoundedStreamCapture",
]
