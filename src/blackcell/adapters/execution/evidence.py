"""Bounded host-owned evidence collection for execution change-provider calls."""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath

from blackcell.adapters.execution.worktree import (
    GitWorktreeLifecycle,
    WorktreeExecutionSpec,
    WorktreeLifecycleError,
)
from blackcell.kernel._json import bytes_digest
from blackcell.orchestration.changes import (
    MAX_EVIDENCE_BYTES,
    MAX_EVIDENCE_FILE_BYTES,
    MAX_EVIDENCE_FILES,
    ChangeContext,
    ChangeContractError,
    ExecutionEvidenceFile,
)


class ExecutionEvidenceFailureCode(StrEnum):
    INVALID_REQUEST = "invalid-execution-evidence-request"
    WORKTREE_UNAVAILABLE = "execution-evidence-worktree-unavailable"
    WORKTREE_NOT_FRESH = "execution-evidence-worktree-not-fresh"
    UNSAFE_ENTRY = "execution-evidence-unsafe-entry"
    FILE_TOO_LARGE = "execution-evidence-file-too-large"
    TOO_MANY_FILES = "execution-evidence-too-many-files"
    TOTAL_TOO_LARGE = "execution-evidence-total-too-large"
    INVALID_TEXT = "execution-evidence-invalid-text"
    CHANGED_DURING_READ = "execution-evidence-changed-during-read"


class ExecutionEvidenceError(RuntimeError):
    """A stable evidence failure that never contains file names or contents."""

    def __init__(self, code: ExecutionEvidenceFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class ExecutionEvidenceCollector:
    """Collect regular UTF-8 files under exact worktree path authority."""

    lifecycle: GitWorktreeLifecycle = field(default_factory=GitWorktreeLifecycle, repr=False)

    def collect(
        self,
        spec: WorktreeExecutionSpec,
        *,
        objective: str,
        constraints: tuple[str, ...],
    ) -> ChangeContext:
        if not isinstance(spec, WorktreeExecutionSpec) or not spec.allowed_paths:
            raise ExecutionEvidenceError(ExecutionEvidenceFailureCode.INVALID_REQUEST)
        try:
            inspection = self.lifecycle.inspect(spec)
        except WorktreeLifecycleError as error:
            raise ExecutionEvidenceError(
                ExecutionEvidenceFailureCode.WORKTREE_UNAVAILABLE
            ) from error
        if (
            not inspection.clean
            or inspection.changed_paths
            or inspection.head_commit != spec.base_commit
            or not inspection.path_policy_compliant
        ):
            raise ExecutionEvidenceError(ExecutionEvidenceFailureCode.WORKTREE_NOT_FRESH)

        collected: dict[str, ExecutionEvidenceFile] = {}
        total_bytes = 0
        for allowed_path in spec.allowed_paths:
            target = spec.worktree_path
            relative = PurePosixPath()
            if allowed_path != ".":
                relative = PurePosixPath(allowed_path)
                target = self._resolve_without_symlinks(spec.worktree_path, relative)
                if target is None:
                    continue
            total_bytes = self._visit(
                target,
                relative,
                collected=collected,
                total_bytes=total_bytes,
            )
        try:
            return ChangeContext(
                objective=objective,
                constraints=constraints,
                base_commit=spec.base_commit,
                allowed_paths=spec.allowed_paths,
                max_changed_paths=spec.max_changed_paths,
                files=tuple(collected.values()),
            )
        except ChangeContractError as error:
            raise ExecutionEvidenceError(ExecutionEvidenceFailureCode.INVALID_REQUEST) from error

    @staticmethod
    def _resolve_without_symlinks(root: Path, relative: PurePosixPath) -> Path | None:
        current = root
        for part in relative.parts:
            if part == ".git":
                raise ExecutionEvidenceError(ExecutionEvidenceFailureCode.UNSAFE_ENTRY)
            current = current / part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                return None
            except OSError as error:
                raise ExecutionEvidenceError(ExecutionEvidenceFailureCode.UNSAFE_ENTRY) from error
            if stat.S_ISLNK(metadata.st_mode):
                raise ExecutionEvidenceError(ExecutionEvidenceFailureCode.UNSAFE_ENTRY)
        return current

    def _visit(
        self,
        path: Path,
        relative: PurePosixPath,
        *,
        collected: dict[str, ExecutionEvidenceFile],
        total_bytes: int,
    ) -> int:
        if ".git" in relative.parts:
            return total_bytes
        try:
            metadata = path.lstat()
        except OSError as error:
            raise ExecutionEvidenceError(ExecutionEvidenceFailureCode.UNSAFE_ENTRY) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ExecutionEvidenceError(ExecutionEvidenceFailureCode.UNSAFE_ENTRY)
        if stat.S_ISDIR(metadata.st_mode):
            try:
                entries = sorted(os.scandir(path), key=lambda entry: entry.name)
            except OSError as error:
                raise ExecutionEvidenceError(ExecutionEvidenceFailureCode.UNSAFE_ENTRY) from error
            for entry in entries:
                child_relative = relative / entry.name
                total_bytes = self._visit(
                    Path(entry.path),
                    child_relative,
                    collected=collected,
                    total_bytes=total_bytes,
                )
            return total_bytes
        if not stat.S_ISREG(metadata.st_mode):
            raise ExecutionEvidenceError(ExecutionEvidenceFailureCode.UNSAFE_ENTRY)

        relative_path = relative.as_posix()
        if not relative_path or relative_path in collected:
            return total_bytes
        if len(collected) >= MAX_EVIDENCE_FILES:
            raise ExecutionEvidenceError(ExecutionEvidenceFailureCode.TOO_MANY_FILES)
        data = _read_stable_regular_file(path)
        if len(data) > MAX_EVIDENCE_FILE_BYTES:
            raise ExecutionEvidenceError(ExecutionEvidenceFailureCode.FILE_TOO_LARGE)
        total_bytes += len(data)
        if total_bytes > MAX_EVIDENCE_BYTES:
            raise ExecutionEvidenceError(ExecutionEvidenceFailureCode.TOTAL_TOO_LARGE)
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ExecutionEvidenceError(ExecutionEvidenceFailureCode.INVALID_TEXT) from error
        if "\x00" in content:
            raise ExecutionEvidenceError(ExecutionEvidenceFailureCode.INVALID_TEXT)
        collected[relative_path] = ExecutionEvidenceFile(
            path=relative_path,
            content=content,
            content_digest=bytes_digest(data),
        )
        return total_bytes


def _read_stable_regular_file(path: Path) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ExecutionEvidenceError(ExecutionEvidenceFailureCode.UNSAFE_ENTRY)
        if before.st_size > MAX_EVIDENCE_FILE_BYTES:
            raise ExecutionEvidenceError(ExecutionEvidenceFailureCode.FILE_TOO_LARGE)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = None
            data = handle.read(MAX_EVIDENCE_FILE_BYTES + 1)
            after = os.fstat(handle.fileno())
    except ExecutionEvidenceError:
        raise
    except OSError as error:
        raise ExecutionEvidenceError(ExecutionEvidenceFailureCode.UNSAFE_ENTRY) from error
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
    if len(data) > MAX_EVIDENCE_FILE_BYTES:
        raise ExecutionEvidenceError(ExecutionEvidenceFailureCode.FILE_TOO_LARGE)
    if _file_identity(before) != _file_identity(after) or len(data) != before.st_size:
        raise ExecutionEvidenceError(ExecutionEvidenceFailureCode.CHANGED_DURING_READ)
    return data


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


__all__ = [
    "ExecutionEvidenceCollector",
    "ExecutionEvidenceError",
    "ExecutionEvidenceFailureCode",
]
