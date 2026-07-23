"""Exclusive owner-only locks for alpha worker process roles."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path

from blackcell.config import RuntimePaths


class WorkerProcessRole(StrEnum):
    ALPHA_EXECUTION = "alpha-execution-worker"
    ALPHA_REVIEW = "alpha-review-worker"
    ALPHA_VERIFICATION = "alpha-verification-worker"


class WorkerProcessLockFailureCode(StrEnum):
    ALREADY_RUNNING = "worker-process-already-running"
    UNSAFE_LOCK = "unsafe-worker-process-lock"


class WorkerProcessLockError(RuntimeError):
    def __init__(self, code: WorkerProcessLockFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@contextmanager
def worker_process_lock(paths: RuntimePaths, role: WorkerProcessRole) -> Iterator[None]:
    """Hold one nonblocking role lock for the caller's complete serve lifetime."""

    descriptor = _acquire_worker_process_lock(paths, role)
    try:
        yield
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            raise WorkerProcessLockError(WorkerProcessLockFailureCode.UNSAFE_LOCK) from error


def _acquire_worker_process_lock(paths: RuntimePaths, role: WorkerProcessRole) -> int:
    if not isinstance(paths, RuntimePaths) or not isinstance(role, WorkerProcessRole):
        raise WorkerProcessLockError(WorkerProcessLockFailureCode.UNSAFE_LOCK)
    root = paths.data_root
    _require_owner_data_root(root)
    lock_path = root / f".{role.value}.lock"
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        try:
            descriptor = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            descriptor = os.open(lock_path, flags)
    except OSError as error:
        raise WorkerProcessLockError(WorkerProcessLockFailureCode.UNSAFE_LOCK) from error
    try:
        if created:
            os.fchmod(descriptor, 0o600)
        _require_owner_lock_file(lock_path, descriptor)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise WorkerProcessLockError(
                    WorkerProcessLockFailureCode.ALREADY_RUNNING
                ) from error
            raise WorkerProcessLockError(WorkerProcessLockFailureCode.UNSAFE_LOCK) from error
        _require_owner_lock_file(lock_path, descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _require_owner_data_root(root: Path) -> None:
    if not isinstance(root, Path) or not root.is_absolute():
        raise WorkerProcessLockError(WorkerProcessLockFailureCode.UNSAFE_LOCK)
    try:
        metadata = root.lstat()
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise WorkerProcessLockError(WorkerProcessLockFailureCode.UNSAFE_LOCK) from error
    if (
        resolved != root
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise WorkerProcessLockError(WorkerProcessLockFailureCode.UNSAFE_LOCK)


def _require_owner_lock_file(path: Path, descriptor: int) -> None:
    try:
        descriptor_metadata = os.fstat(descriptor)
        path_metadata = path.lstat()
    except OSError as error:
        raise WorkerProcessLockError(WorkerProcessLockFailureCode.UNSAFE_LOCK) from error
    if (
        not stat.S_ISREG(descriptor_metadata.st_mode)
        or not stat.S_ISREG(path_metadata.st_mode)
        or descriptor_metadata.st_uid != os.geteuid()
        or path_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(descriptor_metadata.st_mode) != 0o600
        or stat.S_IMODE(path_metadata.st_mode) != 0o600
        or (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
        != (path_metadata.st_dev, path_metadata.st_ino)
    ):
        raise WorkerProcessLockError(WorkerProcessLockFailureCode.UNSAFE_LOCK)


__all__ = [
    "WorkerProcessLockError",
    "WorkerProcessLockFailureCode",
    "WorkerProcessRole",
    "worker_process_lock",
]
