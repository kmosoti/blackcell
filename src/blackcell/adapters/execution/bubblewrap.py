"""Fail-closed Linux isolation for alpha acceptance commands.

This boundary provides fresh namespaces, a read-only project checkout, a cleared environment, no
host network namespace, capability removal, and POSIX resource limits. It does not claim a complete
hostile-code boundary: the initial alpha policy has no syscall filter or cgroup-v2 controller.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from blackcell.adapters.bounded_process import (
    BoundedProcessError,
    BoundedProcessFailureCode,
    BoundedProcessResult,
    BoundedProcessRunner,
)
from blackcell.adapters.execution.worktree import (
    GitWorktreeLifecycle,
    WorktreeExecutionSpec,
    WorktreeInspection,
    WorktreeLifecycleError,
    worktree_inspection_payload,
)
from blackcell.kernel._json import json_digest
from blackcell.orchestration.alpha_acceptance import (
    AlphaAcceptanceCommand,
    AlphaAcceptanceError,
    AlphaAcceptanceFailureCode,
    AlphaAcceptanceResult,
    AlphaAcceptanceStream,
)

BUBBLEWRAP_ISOLATION_POLICY_SCHEMA = "blackcell.bubblewrap-isolation-policy/v1"
BUBBLEWRAP_ACCEPTANCE_PROBE_TIMEOUT_SECONDS = 2

_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")
_SYSTEM_ROOTS = tuple(
    path for path in (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64")) if path.exists()
)
_VIRTUAL_ROOTS = (Path("/dev"), Path("/proc"), Path("/tmp"), Path("/workspace"))
_STATUS_LIMIT_BYTES = 16 * 1024
_NAMESPACE_STATUS_KEYS = {
    "cgroup-namespace": "cgroup",
    "ipc-namespace": "ipc",
    "mnt-namespace": "mnt",
    "net-namespace": "net",
    "pid-namespace": "pid",
    "uts-namespace": "uts",
}


@dataclass(frozen=True, slots=True)
class BubblewrapExecutable:
    """One host-owned alias bound to an exact canonical executable identity."""

    alias: str
    path: Path
    device: int = field(init=False)
    inode: int = field(init=False)
    size_bytes: int = field(init=False)
    modified_ns: int = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.alias, str) or _ALIAS.fullmatch(self.alias) is None:
            raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.INVALID_POLICY)
        path, metadata = _canonical_executable(self.path)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "device", metadata.st_dev)
        object.__setattr__(self, "inode", metadata.st_ino)
        object.__setattr__(self, "size_bytes", metadata.st_size)
        object.__setattr__(self, "modified_ns", metadata.st_mtime_ns)

    @property
    def digest(self) -> str:
        return json_digest(
            {
                "alias": self.alias,
                "path": str(self.path),
                "device": self.device,
                "inode": self.inode,
                "size_bytes": self.size_bytes,
                "modified_ns": self.modified_ns,
            }
        )

    def verify(self) -> None:
        path, metadata = _canonical_executable(self.path)
        if path != self.path or (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        ) != (self.device, self.inode, self.size_bytes, self.modified_ns):
            raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.INVALID_POLICY)


@dataclass(frozen=True, slots=True)
class BubblewrapIsolationPolicy:
    """Host policy for visible runtimes and bounded acceptance-command resources."""

    executables: tuple[BubblewrapExecutable, ...]
    runtime_roots: tuple[Path, ...] = ()
    address_space_limit_bytes: int = 1024 * 1024 * 1024
    cpu_limit_seconds: int = 60
    process_limit: int = 128
    open_file_limit: int = 128
    file_size_limit_bytes: int = 16 * 1024 * 1024
    tmpfs_limit_bytes: int = 64 * 1024 * 1024
    schema_version: Literal["blackcell.bubblewrap-isolation-policy/v1"] = (
        BUBBLEWRAP_ISOLATION_POLICY_SCHEMA
    )

    def __post_init__(self) -> None:
        if (
            self.schema_version != BUBBLEWRAP_ISOLATION_POLICY_SCHEMA
            or not isinstance(self.executables, tuple)
            or not self.executables
            or not all(isinstance(item, BubblewrapExecutable) for item in self.executables)
            or len({item.alias for item in self.executables}) != len(self.executables)
            or not isinstance(self.runtime_roots, tuple)
            or len(self.runtime_roots) > 32
        ):
            raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.INVALID_POLICY)

        roots = tuple(
            sorted((_canonical_runtime_root(path) for path in self.runtime_roots), key=str)
        )
        if len(set(roots)) != len(roots) or any(
            _paths_overlap(left, right)
            for index, left in enumerate(roots)
            for right in roots[index + 1 :]
        ):
            raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.INVALID_POLICY)
        object.__setattr__(self, "runtime_roots", roots)

        limits = (
            (self.address_space_limit_bytes, 64 * 1024 * 1024, 64 * 1024 * 1024 * 1024),
            (self.cpu_limit_seconds, 1, 600),
            (self.process_limit, 1, 4096),
            (self.open_file_limit, 16, 4096),
            (self.file_size_limit_bytes, 1, 1024 * 1024 * 1024),
            (self.tmpfs_limit_bytes, 1024 * 1024, 1024 * 1024 * 1024),
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum
            for value, minimum, maximum in limits
        ):
            raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.INVALID_POLICY)

        visible_roots = (*_SYSTEM_ROOTS, *roots)
        if any(
            not any(item.path.is_relative_to(root) for root in visible_roots)
            for item in self.executables
        ):
            raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.INVALID_POLICY)

    @property
    def digest(self) -> str:
        return json_digest(
            {
                "schema_version": self.schema_version,
                "executables": [
                    {"alias": executable.alias, "identity_digest": executable.digest}
                    for executable in self.executables
                ],
                "runtime_roots": [str(path) for path in self.runtime_roots],
                "address_space_limit_bytes": self.address_space_limit_bytes,
                "cpu_limit_seconds": self.cpu_limit_seconds,
                "process_limit": self.process_limit,
                "open_file_limit": self.open_file_limit,
                "file_size_limit_bytes": self.file_size_limit_bytes,
                "tmpfs_limit_bytes": self.tmpfs_limit_bytes,
                "network": "unshared-loopback-only",
                "worktree": "read-only-git-masked",
                "ambient_environment": "cleared",
                "capabilities": "all-dropped",
                "seccomp": "not-configured",
                "cgroup_limits": "not-configured",
            }
        )

    def executable(self, alias: str) -> BubblewrapExecutable:
        for executable in self.executables:
            if executable.alias == alias:
                return executable
        raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.EXECUTABLE_NOT_ALLOWED)


class BubblewrapProcessTransport(Protocol):
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
    ) -> BoundedProcessResult: ...


@dataclass(frozen=True, slots=True)
class BubblewrapAcceptanceRunner:
    policy: BubblewrapIsolationPolicy
    worktrees: GitWorktreeLifecycle = field(default_factory=GitWorktreeLifecycle, repr=False)
    transport: BubblewrapProcessTransport = field(default_factory=BoundedProcessRunner, repr=False)
    bubblewrap_executable: Path = Path("/usr/bin/bwrap")
    prlimit_executable: Path = Path("/usr/bin/prlimit")
    probe_executable: Path = Path("/usr/bin/true")

    def __post_init__(self) -> None:
        if sys.platform != "linux":
            raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.UNSUPPORTED_PLATFORM)
        if not isinstance(self.policy, BubblewrapIsolationPolicy):
            raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.INVALID_POLICY)
        for attribute in ("bubblewrap_executable", "prlimit_executable", "probe_executable"):
            path, _ = _canonical_executable(getattr(self, attribute))
            if not any(path.is_relative_to(root) for root in _SYSTEM_ROOTS):
                raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.INVALID_POLICY)
            object.__setattr__(self, attribute, path)

    def run(
        self,
        command: AlphaAcceptanceCommand,
        spec: WorktreeExecutionSpec,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> AlphaAcceptanceResult:
        if not isinstance(command, AlphaAcceptanceCommand) or not isinstance(
            spec, WorktreeExecutionSpec
        ):
            raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.INVALID_COMMAND)
        executable = self.policy.executable(command.argv[0])
        executable.verify()
        self._validate_mount_authority(spec)
        before = self._inspect(spec)
        if not before.path_policy_compliant:
            raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.WORKTREE_POLICY_VIOLATION)

        self._probe()
        process_error: AlphaAcceptanceError | None = None
        execution: tuple[BoundedProcessResult, int] | None = None
        try:
            execution = self._run_isolated(command, spec, executable, cancel_requested)
        except AlphaAcceptanceError as error:
            process_error = error

        after = self._inspect(spec)
        if after != before:
            raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.WORKTREE_CHANGED)
        if process_error is not None:
            raise process_error
        if execution is None:  # pragma: no cover - control-flow invariant
            raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.OUTPUT_INCOMPLETE)

        process_result, container_exit_code = execution
        stdout = _complete_stream(process_result, "stdout")
        stderr = _complete_stream(process_result, "stderr")
        inspection_digest = json_digest(worktree_inspection_payload(before))
        return AlphaAcceptanceResult(
            check_id=command.check_id,
            command_digest=command.digest,
            worktree_spec_digest=spec.digest,
            isolation_policy_digest=self.policy.digest,
            inspection_before_digest=inspection_digest,
            inspection_after_digest=inspection_digest,
            return_code=container_exit_code,
            expected_exit_code=command.expected_exit_code,
            passed=container_exit_code == command.expected_exit_code,
            stdout=stdout,
            stderr=stderr,
        )

    def _validate_mount_authority(self, spec: WorktreeExecutionSpec) -> None:
        protected = (spec.repository_root, spec.isolation_root, spec.worktree_path)
        if any(
            _paths_overlap(runtime_root, protected_root)
            for runtime_root in self.policy.runtime_roots
            for protected_root in protected
        ):
            raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.INVALID_POLICY)

    def _inspect(self, spec: WorktreeExecutionSpec) -> WorktreeInspection:
        try:
            return self.worktrees.inspect(spec)
        except WorktreeLifecycleError as error:
            raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.WORKTREE_UNAVAILABLE) from error

    def _probe(self) -> None:
        def argv_builder(status_fd: int) -> tuple[str, ...]:
            return (
                *self._base_argv(status_fd=status_fd),
                "--chdir",
                "/tmp",
                "--",
                str(self.probe_executable),
            )

        try:
            result, exit_code = self._launch(
                argv_builder,
                timeout_seconds=BUBBLEWRAP_ACCEPTANCE_PROBE_TIMEOUT_SECONDS,
                stdout_limit_bytes=1024,
                stderr_limit_bytes=16 * 1024,
                cancel_requested=None,
            )
        except AlphaAcceptanceError as error:
            raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.ISOLATION_UNAVAILABLE) from error
        if result.return_code != 0 or exit_code != 0:
            raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.ISOLATION_UNAVAILABLE)

    def _run_isolated(
        self,
        command: AlphaAcceptanceCommand,
        spec: WorktreeExecutionSpec,
        executable: BubblewrapExecutable,
        cancel_requested: Callable[[], bool] | None,
    ) -> tuple[BoundedProcessResult, int]:
        cpu_seconds = min(
            self.policy.cpu_limit_seconds,
            max(1, math.ceil(command.timeout_seconds)),
        )

        def argv_builder(status_fd: int) -> tuple[str, ...]:
            return (
                *self._base_argv(status_fd=status_fd),
                *self._runtime_mount_argv(),
                "--ro-bind",
                str(spec.worktree_path),
                "/workspace",
                "--ro-bind",
                "/dev/null",
                "/workspace/.git",
                "--chdir",
                "/workspace",
                "--",
                str(self.prlimit_executable),
                f"--as={self.policy.address_space_limit_bytes}",
                f"--cpu={cpu_seconds}",
                f"--nproc={self.policy.process_limit}",
                f"--nofile={self.policy.open_file_limit}",
                f"--fsize={self.policy.file_size_limit_bytes}",
                "--core=0",
                "--",
                str(executable.path),
                *command.argv[1:],
            )

        return self._launch(
            argv_builder,
            timeout_seconds=command.timeout_seconds,
            stdout_limit_bytes=command.stdout_limit_bytes,
            stderr_limit_bytes=command.stderr_limit_bytes,
            cancel_requested=cancel_requested,
        )

    def _base_argv(self, *, status_fd: int) -> tuple[str, ...]:
        mounts = tuple(
            token for root in _SYSTEM_ROOTS for token in ("--ro-bind", str(root), str(root))
        )
        return (
            str(self.bubblewrap_executable),
            "--unshare-user",
            "--unshare-all",
            "--disable-userns",
            "--assert-userns-disabled",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--hostname",
            "blackcell-alpha",
            *mounts,
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--size",
            str(self.policy.tmpfs_limit_bytes),
            "--tmpfs",
            "/tmp",
            "--dir",
            "/tmp/blackcell-home",
            "--chmod",
            "0700",
            "/tmp/blackcell-home",
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--setenv",
            "HOME",
            "/tmp/blackcell-home",
            "--setenv",
            "TMPDIR",
            "/tmp",
            "--setenv",
            "LANG",
            "C",
            "--setenv",
            "LC_ALL",
            "C",
            "--setenv",
            "PYTHONDONTWRITEBYTECODE",
            "1",
            "--json-status-fd",
            str(status_fd),
        )

    def _runtime_mount_argv(self) -> tuple[str, ...]:
        arguments: list[str] = []
        for parent in _runtime_mount_parents(self.policy.runtime_roots):
            arguments.extend(("--dir", str(parent)))
        for root in self.policy.runtime_roots:
            arguments.extend(("--ro-bind", str(root), str(root)))
        return tuple(arguments)

    def _launch(
        self,
        argv_builder: Callable[[int], tuple[str, ...]],
        *,
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        cancel_requested: Callable[[], bool] | None,
    ) -> tuple[BoundedProcessResult, int]:
        with tempfile.TemporaryFile() as status_stream:
            status_fd = status_stream.fileno()
            try:
                result = self.transport.run(
                    argv_builder(status_fd),
                    cwd=Path("/"),
                    timeout_seconds=timeout_seconds,
                    stdout_limit_bytes=stdout_limit_bytes,
                    stderr_limit_bytes=stderr_limit_bytes,
                    environment={},
                    cancel_requested=cancel_requested,
                    pass_fds=(status_fd,),
                )
            except BoundedProcessError as error:
                raise AlphaAcceptanceError(_map_process_failure(error.code)) from error
            status_stream.seek(0, os.SEEK_END)
            if status_stream.tell() > _STATUS_LIMIT_BYTES:
                raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.ISOLATION_UNAVAILABLE)
            status_stream.seek(0)
            status = status_stream.read()

        exit_code = _validate_status(status)
        if result.return_code != exit_code:
            raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.ISOLATION_UNAVAILABLE)
        return result, exit_code


def _canonical_executable(value: Path) -> tuple[Path, os.stat_result]:
    if not isinstance(value, Path) or not value.is_absolute():
        raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.INVALID_POLICY)
    try:
        path = value.resolve(strict=True)
        metadata = path.stat(follow_symlinks=False)
    except (OSError, RuntimeError) as error:
        raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.INVALID_POLICY) from error
    if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
        raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.INVALID_POLICY)
    return path, metadata


def _canonical_runtime_root(value: Path) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.INVALID_POLICY)
    try:
        path = value.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.INVALID_POLICY) from error
    if (
        not path.is_dir()
        or path == Path("/")
        or any(_paths_overlap(path, reserved) for reserved in (*_SYSTEM_ROOTS, *_VIRTUAL_ROOTS))
    ):
        raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.INVALID_POLICY)
    return path


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _runtime_mount_parents(roots: tuple[Path, ...]) -> tuple[Path, ...]:
    parents: set[Path] = set()
    for root in roots:
        current = root.parent
        while current != Path("/"):
            if not any(current == system_root for system_root in _SYSTEM_ROOTS):
                parents.add(current)
            current = current.parent
    return tuple(sorted(parents, key=lambda path: (len(path.parts), str(path))))


def _validate_status(value: bytes) -> int:
    if not isinstance(value, bytes) or not value or len(value) > _STATUS_LIMIT_BYTES:
        raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.ISOLATION_UNAVAILABLE)
    try:
        text = value.decode("utf-8")
        decoder = json.JSONDecoder()
        documents: list[object] = []
        index = 0
        while index < len(text):
            while index < len(text) and text[index].isspace():
                index += 1
            if index == len(text):
                break
            document, index = decoder.raw_decode(text, index)
            documents.append(document)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.ISOLATION_UNAVAILABLE) from error

    if (
        len(documents) != 2
        or not isinstance(documents[0], dict)
        or not isinstance(documents[1], dict)
        or "child-pid" not in documents[0]
        or "exit-code" in documents[0]
        or "exit-code" not in documents[1]
        or "child-pid" in documents[1]
    ):
        raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.ISOLATION_UNAVAILABLE)
    child = documents[0]
    exit_status = documents[1]
    child_pid = child.get("child-pid")
    if isinstance(child_pid, bool) or not isinstance(child_pid, int) or child_pid < 1:
        raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.ISOLATION_UNAVAILABLE)
    for status_key, namespace in _NAMESPACE_STATUS_KEYS.items():
        observed = child.get(status_key)
        try:
            host_namespace = os.stat(f"/proc/self/ns/{namespace}").st_ino
        except OSError as error:
            raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.ISOLATION_UNAVAILABLE) from error
        if (
            isinstance(observed, bool)
            or not isinstance(observed, int)
            or observed == host_namespace
        ):
            raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.ISOLATION_UNAVAILABLE)
    exit_code = exit_status.get("exit-code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or not 0 <= exit_code <= 255:
        raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.ISOLATION_UNAVAILABLE)
    return exit_code


def _complete_stream(
    result: BoundedProcessResult, name: Literal["stdout", "stderr"]
) -> AlphaAcceptanceStream:
    stream = getattr(result, name)
    if not stream.complete or stream.total_bytes != len(stream.captured):
        raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.OUTPUT_INCOMPLETE)
    return AlphaAcceptanceStream(stream.captured)


def _map_process_failure(code: BoundedProcessFailureCode) -> AlphaAcceptanceFailureCode:
    return {
        BoundedProcessFailureCode.INVALID_INVOCATION: AlphaAcceptanceFailureCode.INVALID_POLICY,
        BoundedProcessFailureCode.SPAWN_FAILED: AlphaAcceptanceFailureCode.SPAWN_FAILED,
        BoundedProcessFailureCode.CANCELED: AlphaAcceptanceFailureCode.CANCELED,
        BoundedProcessFailureCode.TIMED_OUT: AlphaAcceptanceFailureCode.TIMED_OUT,
        BoundedProcessFailureCode.OUTPUT_TOO_LARGE: AlphaAcceptanceFailureCode.OUTPUT_TOO_LARGE,
        BoundedProcessFailureCode.OUTPUT_INCOMPLETE: AlphaAcceptanceFailureCode.OUTPUT_INCOMPLETE,
    }[code]


__all__ = [
    "BUBBLEWRAP_ACCEPTANCE_PROBE_TIMEOUT_SECONDS",
    "BUBBLEWRAP_ISOLATION_POLICY_SCHEMA",
    "BubblewrapAcceptanceRunner",
    "BubblewrapExecutable",
    "BubblewrapIsolationPolicy",
]
