"""Bounded execution mechanics for trusted commands, not a process sandbox.

The executable and cwd are opened and identity-checked before spawn. Process groups support
deadline cleanup for cooperative, non-daemonizing commands. They do not contain ``setsid()``
escapes or provide syscall, filesystem, network, cgroup, or PID-namespace isolation.
"""

from __future__ import annotations

import hashlib
import os
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from blackcell.adapters.execution.local_process.configuration import (
    LocalProcessAffordance,
    LocalProcessConfigurationError,
    canonical_existing_path,
    file_descriptor_digest,
    require_supported_platform,
)
from blackcell.features.execute_affordance import UncertainExecutionError
from blackcell.kernel._json import bytes_digest

_READ_SIZE = 64 * 1024


def _is_digest(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
        return False
    try:
        int(value.removeprefix("sha256:"), 16)
    except ValueError:
        return False
    return True


class ProcessCompletion(StrEnum):
    EXITED = "exited"
    SIGNALED = "signaled"
    TIMED_OUT = "timed-out"
    SPAWN_FAILED = "spawn-failed"
    OUTPUT_INCOMPLETE = "output-incomplete"
    LINGERING_PROCESS = "lingering-process"


@dataclass(frozen=True, slots=True)
class StreamCapture:
    captured: bytes
    total_bytes: int | None
    content_digest: str | None
    eof: bool
    truncated: bool

    def __post_init__(self) -> None:
        if not isinstance(self.captured, bytes):
            raise ValueError("stream capture must be bytes")
        if not isinstance(self.eof, bool) or not isinstance(self.truncated, bool):
            raise ValueError("stream EOF and truncation flags must be booleans")
        if self.eof:
            if self.total_bytes is None or self.content_digest is None:
                raise ValueError("an EOF stream requires its total and full digest")
            if isinstance(self.total_bytes, bool) or not isinstance(self.total_bytes, int):
                raise ValueError("stream total must be an integer")
            if self.total_bytes < len(self.captured):
                raise ValueError("stream total cannot be smaller than its capture")
            if not _is_digest(self.content_digest):
                raise ValueError("stream content digest must be SHA-256")
            if self.truncated != (self.total_bytes > len(self.captured)):
                raise ValueError("stream truncation does not match its exact total")
            if not self.truncated and self.content_digest != bytes_digest(self.captured):
                raise ValueError("complete stream digest does not match captured bytes")
        elif self.total_bytes is not None or self.content_digest is not None:
            raise ValueError("an incomplete stream cannot claim a total or full digest")
        elif not self.truncated:
            raise ValueError("an incomplete stream must be marked truncated")


@dataclass(frozen=True, slots=True)
class ProcessRun:
    completion: ProcessCompletion
    started_at: datetime
    completed_at: datetime
    return_code: int | None
    signal_number: int | None
    stdout: StreamCapture
    stderr: StreamCapture
    spawn_errno: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.completion, ProcessCompletion):
            raise ValueError("process completion kind must be recognized")
        if not isinstance(self.stdout, StreamCapture) or not isinstance(self.stderr, StreamCapture):
            raise ValueError("process streams must be typed captures")
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("process start must be timezone-aware")
        if self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None:
            raise ValueError("process completion must be timezone-aware")
        if self.completed_at < self.started_at:
            raise ValueError("process completion cannot precede process start")
        if self.completion is ProcessCompletion.SPAWN_FAILED:
            if self.return_code is not None or self.signal_number is not None:
                raise ValueError("a spawn failure cannot claim a return status")
        elif self.return_code is None:
            raise ValueError("a spawned process requires a return code")
        elif isinstance(self.return_code, bool) or not isinstance(self.return_code, int):
            raise ValueError("process return code must be an integer")
        if self.completion is ProcessCompletion.EXITED and cast("int", self.return_code) < 0:
            raise ValueError("an exited process cannot claim a signal return code")
        if self.completion is ProcessCompletion.SIGNALED and cast("int", self.return_code) >= 0:
            raise ValueError("a signaled process requires a negative return code")
        if self.return_code is not None and self.return_code < 0:
            if self.signal_number != -self.return_code:
                raise ValueError("signal metadata does not match the return code")
        elif self.signal_number is not None:
            raise ValueError("a non-signaled process cannot claim a signal")
        if self.spawn_errno is not None and (
            isinstance(self.spawn_errno, bool)
            or not isinstance(self.spawn_errno, int)
            or self.spawn_errno < 0
        ):
            raise ValueError("spawn errno must be a non-negative integer")
        if self.completion is not ProcessCompletion.SPAWN_FAILED and self.spawn_errno is not None:
            raise ValueError("only a spawn failure may claim spawn errno")


class _ReadablePipe(Protocol):
    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


class LocalProcessRunner:
    """Bound runtime and output for one trusted, audited allowlisted command."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        require_supported_platform()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic

    def run(
        self,
        configuration: LocalProcessAffordance,
        argv: Sequence[str],
        environment: Mapping[str, str],
    ) -> ProcessRun:
        require_supported_platform()
        _validate_runtime_inputs(configuration, argv, environment)
        started_at = self._now()
        empty = _empty_capture()
        with _pin_configuration(configuration) as pinned:
            try:
                process = subprocess.Popen(
                    list(argv),
                    executable=pinned.executable_path,
                    cwd=pinned.working_directory_path,
                    env={},
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    text=False,
                    close_fds=True,
                    pass_fds=(pinned.executable_fd, pinned.working_directory_fd),
                    start_new_session=True,
                )
            except OSError as error:
                return ProcessRun(
                    ProcessCompletion.SPAWN_FAILED,
                    started_at,
                    self._now(),
                    None,
                    None,
                    empty,
                    empty,
                    error.errno,
                )

        stdout = process.stdout
        stderr = process.stderr
        if stdout is None or stderr is None:  # pragma: no cover - Popen contract guard
            _terminate_process_group(process, configuration.termination_grace_seconds)
            raise UncertainExecutionError("process pipes were not created")
        stdout_drain = _DrainCollector(
            cast("_ReadablePipe", stdout), configuration.stdout_limit_bytes
        )
        stderr_drain = _DrainCollector(
            cast("_ReadablePipe", stderr), configuration.stderr_limit_bytes
        )
        drain_threads = (
            threading.Thread(
                target=stdout_drain.drain,
                name="blackcell-local-process-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=stderr_drain.drain,
                name="blackcell-local-process-stderr",
                daemon=True,
            ),
        )
        for thread in drain_threads:
            thread.start()

        completion: ProcessCompletion
        try:
            return_code = process.wait(timeout=configuration.definition.timeout_seconds)
        except subprocess.TimeoutExpired:
            _terminate_process_group(process, configuration.termination_grace_seconds)
            return_code = process.returncode
            if return_code is None:  # pragma: no cover - termination helper invariant
                raise UncertainExecutionError("timed-out process could not be reaped") from None
            completion = ProcessCompletion.TIMED_OUT
        else:
            completion = ProcessCompletion.SIGNALED if return_code < 0 else ProcessCompletion.EXITED

        drain_deadline = self._monotonic() + configuration.drain_grace_seconds
        _join_until(drain_threads, drain_deadline, self._monotonic)
        drains_incomplete = any(thread.is_alive() for thread in drain_threads)
        lingering_process = _group_exists(process.pid)
        if drains_incomplete or lingering_process:
            _terminate_remaining_group(process.pid, configuration.termination_grace_seconds)
            if completion is not ProcessCompletion.TIMED_OUT:
                completion = (
                    ProcessCompletion.LINGERING_PROCESS
                    if lingering_process
                    else ProcessCompletion.OUTPUT_INCOMPLETE
                )
            drain_deadline = self._monotonic() + configuration.drain_grace_seconds
            _join_until(drain_threads, drain_deadline, self._monotonic)
        if any(thread.is_alive() for thread in drain_threads):
            stdout_drain.close()
            stderr_drain.close()
            drain_deadline = self._monotonic() + min(configuration.drain_grace_seconds, 0.25)
            _join_until(drain_threads, drain_deadline, self._monotonic)

        completed_at = self._now()
        return ProcessRun(
            completion,
            started_at,
            completed_at,
            return_code,
            -return_code if return_code < 0 else None,
            stdout_drain.snapshot(),
            stderr_drain.snapshot(),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("local-process clock must return a timezone-aware datetime")
        return value


class _DrainCollector:
    def __init__(self, stream: _ReadablePipe, capture_limit: int) -> None:
        self._stream = stream
        self._capture_limit = capture_limit
        self._captured = bytearray()
        self._total = 0
        self._hasher = hashlib.sha256()
        self._eof = False
        self._lock = threading.Lock()

    def drain(self) -> None:
        eof = False
        try:
            while True:
                chunk = self._stream.read(_READ_SIZE)
                if not chunk:
                    eof = True
                    break
                with self._lock:
                    self._total += len(chunk)
                    self._hasher.update(chunk)
                    remaining = self._capture_limit - len(self._captured)
                    if remaining > 0:
                        self._captured.extend(chunk[:remaining])
        except OSError, ValueError:
            eof = False
        finally:
            with self._lock:
                self._eof = eof
            with suppress(OSError):
                self._stream.close()

    def close(self) -> None:
        with suppress(OSError):
            self._stream.close()

    def snapshot(self) -> StreamCapture:
        with self._lock:
            captured = bytes(self._captured)
            if not self._eof:
                return StreamCapture(captured, None, None, False, True)
            return StreamCapture(
                captured,
                self._total,
                f"sha256:{self._hasher.hexdigest()}",
                True,
                self._total > len(captured),
            )


def _validate_runtime_inputs(
    configuration: LocalProcessAffordance,
    argv: Sequence[str],
    environment: Mapping[str, str],
) -> None:
    if not argv or argv[0] != configuration.executable:
        raise LocalProcessConfigurationError("argv executable differs from configuration")
    if not all(isinstance(item, str) and item and "\x00" not in item for item in argv):
        raise LocalProcessConfigurationError("argv contains an invalid token")
    if configuration.environment or environment:
        raise LocalProcessConfigurationError("local-process/v1 environment must remain empty")
    for path, expected_identity in zip(
        configuration.allowed_path_roots,
        configuration.allowed_path_root_identities,
        strict=True,
    ):
        root = canonical_existing_path(path, label="allowed path root", kind="directory")
        metadata = root.stat(follow_symlinks=False)
        _require_pinned_permissions(metadata, label="allowed path root", executable=False)
        if _identity(metadata) != expected_identity:
            raise LocalProcessConfigurationError("allowed path root identity has changed")


@dataclass(frozen=True, slots=True)
class _PinnedCommand:
    executable_fd: int
    working_directory_fd: int

    @property
    def executable_path(self) -> str:
        return f"/proc/self/fd/{self.executable_fd}"

    @property
    def working_directory_path(self) -> str:
        return f"/proc/self/fd/{self.working_directory_fd}"


@contextmanager
def _pin_configuration(configuration: LocalProcessAffordance) -> Iterator[_PinnedCommand]:
    if not Path("/proc/self/fd").is_dir():
        raise LocalProcessConfigurationError(
            "local-process/v1 requires Linux /proc/self/fd for fd-pinned spawn"
        )
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        executable_fd = os.open(configuration.executable, flags)
    except OSError as error:
        raise LocalProcessConfigurationError("configured executable cannot be pinned") from error
    try:
        try:
            working_directory_fd = os.open(
                configuration.working_directory,
                flags | os.O_DIRECTORY,
            )
        except OSError as error:
            raise LocalProcessConfigurationError(
                "configured working directory cannot be pinned"
            ) from error
        try:
            executable_stat = os.fstat(executable_fd)
            working_directory_stat = os.fstat(working_directory_fd)
            _require_pinned_permissions(
                executable_stat,
                label="executable",
                executable=True,
            )
            _require_pinned_permissions(
                working_directory_stat,
                label="working directory",
                executable=False,
            )
            if _identity(executable_stat) != configuration.executable_identity:
                raise LocalProcessConfigurationError("configured executable identity has changed")
            if file_descriptor_digest(executable_fd) != configuration.executable_digest:
                raise LocalProcessConfigurationError("configured executable content has changed")
            if _identity(working_directory_stat) != configuration.working_directory_identity:
                raise LocalProcessConfigurationError(
                    "configured working directory identity has changed"
                )
            yield _PinnedCommand(executable_fd, working_directory_fd)
        finally:
            os.close(working_directory_fd)
    finally:
        os.close(executable_fd)


def _require_pinned_permissions(
    metadata: os.stat_result,
    *,
    label: str,
    executable: bool,
) -> None:
    if metadata.st_uid not in {0, os.geteuid()}:
        raise LocalProcessConfigurationError(
            f"{label} must be owned by root or the runtime administrator"
        )
    if executable and not metadata.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        raise LocalProcessConfigurationError("configured executable is no longer executable")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise LocalProcessConfigurationError(f"{label} must not be group- or world-writable")
    if executable and metadata.st_mode & (stat.S_ISUID | stat.S_ISGID):
        raise LocalProcessConfigurationError(
            "executable must not carry setuid or setgid permission bits"
        )


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _empty_capture() -> StreamCapture:
    return StreamCapture(b"", 0, f"sha256:{hashlib.sha256(b'').hexdigest()}", True, False)


def _join_until(
    threads: tuple[threading.Thread, threading.Thread],
    deadline: float,
    monotonic: Callable[[], float],
) -> None:
    for thread in threads:
        remaining = max(0.0, deadline - monotonic())
        thread.join(remaining)


def _terminate_process_group(process: subprocess.Popen[bytes], grace_seconds: float) -> None:
    process_group = process.pid
    _signal_group(process_group, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while process.poll() is None and time.monotonic() < deadline:
        try:
            process.wait(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            continue
    if _group_exists(process_group):
        _signal_group(process_group, signal.SIGKILL)
    if process.poll() is None:
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired as error:
            raise UncertainExecutionError("process did not terminate after SIGKILL") from error
    _wait_for_group_exit(process_group, grace_seconds)


def _terminate_remaining_group(process_group: int, grace_seconds: float) -> None:
    if not _group_exists(process_group):
        return
    _signal_group(process_group, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while _group_exists(process_group) and time.monotonic() < deadline:
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    if _group_exists(process_group):
        _signal_group(process_group, signal.SIGKILL)
    _wait_for_group_exit(process_group, grace_seconds)


def _signal_group(process_group: int, requested_signal: signal.Signals) -> None:
    try:
        os.killpg(process_group, requested_signal)
    except ProcessLookupError:
        return
    except OSError as error:
        raise UncertainExecutionError(
            f"process group could not receive {requested_signal.name}"
        ) from error


def _group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_group_exit(process_group: int, grace_seconds: float) -> None:
    deadline = time.monotonic() + grace_seconds
    while _group_exists(process_group) and time.monotonic() < deadline:
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    if _group_exists(process_group):
        raise UncertainExecutionError("process-group termination could not be confirmed")
