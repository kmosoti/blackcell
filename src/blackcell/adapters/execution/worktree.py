"""Lease-bound Git worktree lifecycle for recoverable alpha execution.

This adapter gives an execution attempt a deterministic checkout and local branch. It detects
repository path-budget violations and preserves work on failure, but it is not an operating-system
sandbox: it does not contain processes, syscalls, network access, secrets, or writes through
symlinks. Callers must pair it with separately admitted file effects and an operating-system
process boundary before any untrusted command receives mutation authority.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from blackcell.adapters.bounded_process import (
    BoundedProcessError,
    BoundedProcessFailureCode,
    BoundedProcessResult,
    BoundedProcessRunner,
)
from blackcell.kernel import JsonInput
from blackcell.kernel._json import json_digest

WORKTREE_LEASE_SCHEMA = "blackcell.worktree-lease/v1"
WORKTREE_SPEC_SCHEMA = "blackcell.worktree-spec/v1"
WORKTREE_INSPECTION_SCHEMA = "blackcell.worktree-inspection/v1"
WORKTREE_REMOVAL_SCHEMA = "blackcell.worktree-removal/v1"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_COMMIT_ID = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_ALLOWED_PATHS = 256
_MAX_CHANGED_PATHS = 10_000
_MAX_REPOSITORY_PATH_CHARS = 4096
_GIT_TIMEOUT_SECONDS = 120.0
_GIT_STDOUT_LIMIT_BYTES = 16 * 1024 * 1024
_GIT_STDERR_LIMIT_BYTES = 64 * 1024
_EXTERNAL_FILTER_PATTERN = r"^filter\..*\.(clean|smudge|process)$"


class WorktreeFailureCode(StrEnum):
    INVALID_SPEC = "invalid-worktree-spec"
    INVALID_REPOSITORY = "invalid-worktree-repository"
    INVALID_ISOLATION_ROOT = "invalid-worktree-isolation-root"
    UNSAFE_REPOSITORY_CONFIGURATION = "unsafe-worktree-repository-configuration"
    BASE_COMMIT_NOT_FOUND = "worktree-base-commit-not-found"
    GIT_UNAVAILABLE = "worktree-git-unavailable"
    GIT_SPAWN_FAILED = "worktree-git-spawn-failed"
    GIT_TIMED_OUT = "worktree-git-timed-out"
    GIT_OUTPUT_TOO_LARGE = "worktree-git-output-too-large"
    GIT_OUTPUT_INCOMPLETE = "worktree-git-output-incomplete"
    GIT_COMMAND_FAILED = "worktree-git-command-failed"
    INVALID_GIT_OUTPUT = "invalid-worktree-git-output"
    WORKTREE_CONFLICT = "worktree-conflict"
    WORKTREE_NOT_FOUND = "worktree-not-found"
    WORKTREE_DIRTY = "worktree-dirty"
    PATH_POLICY_VIOLATION = "worktree-path-policy-violation"
    COMMIT_FAILED = "worktree-commit-failed"
    CLEANUP_FAILED = "worktree-cleanup-failed"


class WorktreeLifecycleError(RuntimeError):
    """A stable lifecycle failure that never includes paths, argv, or Git output."""

    def __init__(self, code: WorktreeFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class WorktreeLeaseIdentity:
    run_id: str
    node_id: str
    attempt: int
    fencing_token: int
    worker_id: str
    schema_version: Literal["blackcell.worktree-lease/v1"] = WORKTREE_LEASE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != WORKTREE_LEASE_SCHEMA:
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)
        for value in (self.run_id, self.node_id, self.worker_id):
            if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
                raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)
        for value in (self.attempt, self.fencing_token):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)

    @property
    def digest(self) -> str:
        return json_digest(
            {
                "schema_version": self.schema_version,
                "run_id": self.run_id,
                "node_id": self.node_id,
                "attempt": self.attempt,
                "fencing_token": self.fencing_token,
                "worker_id": self.worker_id,
            }
        )


@dataclass(frozen=True, slots=True)
class WorktreeExecutionSpec:
    lease: WorktreeLeaseIdentity
    repository_root: Path
    isolation_root: Path
    base_commit: str
    allowed_paths: tuple[str, ...]
    max_changed_paths: int
    schema_version: Literal["blackcell.worktree-spec/v1"] = WORKTREE_SPEC_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != WORKTREE_SPEC_SCHEMA or not isinstance(
            self.lease, WorktreeLeaseIdentity
        ):
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)
        repository_root = _canonical_repository_root(self.repository_root)
        isolation_root = _canonical_isolation_root(self.isolation_root)
        if repository_root.is_relative_to(isolation_root) or isolation_root.is_relative_to(
            repository_root
        ):
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_ISOLATION_ROOT)
        if not isinstance(self.base_commit, str) or _COMMIT_ID.fullmatch(self.base_commit) is None:
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)
        if (
            not isinstance(self.allowed_paths, tuple)
            or len(self.allowed_paths) > _MAX_ALLOWED_PATHS
        ):
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)
        normalized_paths = tuple(_normalize_policy_path(path) for path in self.allowed_paths)
        if len(normalized_paths) != len(set(normalized_paths)):
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)
        if (
            isinstance(self.max_changed_paths, bool)
            or not isinstance(self.max_changed_paths, int)
            or not 0 <= self.max_changed_paths <= _MAX_CHANGED_PATHS
        ):
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)
        object.__setattr__(self, "repository_root", repository_root)
        object.__setattr__(self, "isolation_root", isolation_root)
        object.__setattr__(self, "allowed_paths", tuple(sorted(normalized_paths)))

    @property
    def digest(self) -> str:
        return json_digest(
            {
                "schema_version": self.schema_version,
                "lease_digest": self.lease.digest,
                "repository_root": str(self.repository_root),
                "isolation_root": str(self.isolation_root),
                "base_commit": self.base_commit,
                "allowed_paths": list(self.allowed_paths),
                "max_changed_paths": self.max_changed_paths,
            }
        )

    @property
    def worktree_path(self) -> Path:
        return self.isolation_root / f"worktree-{self.digest.removeprefix('sha256:')}"

    @property
    def branch_name(self) -> str:
        return f"blackcell/alpha-worktree/{self.digest.removeprefix('sha256:')}"


@dataclass(frozen=True, slots=True)
class WorktreeInspection:
    spec_digest: str
    lease_digest: str
    worktree_path: Path
    branch_name: str
    base_commit: str
    head_commit: str
    allowed_paths: tuple[str, ...]
    max_changed_paths: int
    changed_paths: tuple[str, ...]
    uncommitted_paths: tuple[str, ...]
    out_of_scope_paths: tuple[str, ...]
    changed_path_limit_exceeded: bool
    schema_version: Literal["blackcell.worktree-inspection/v1"] = WORKTREE_INSPECTION_SCHEMA

    def __post_init__(self) -> None:
        if (
            self.schema_version != WORKTREE_INSPECTION_SCHEMA
            or _DIGEST.fullmatch(self.spec_digest) is None
            or _DIGEST.fullmatch(self.lease_digest) is None
            or not isinstance(self.worktree_path, Path)
            or not self.worktree_path.is_absolute()
            or not self.branch_name.startswith("blackcell/alpha-worktree/")
            or _COMMIT_ID.fullmatch(self.base_commit) is None
            or _COMMIT_ID.fullmatch(self.head_commit) is None
        ):
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
        for paths in (
            self.allowed_paths,
            self.changed_paths,
            self.uncommitted_paths,
            self.out_of_scope_paths,
        ):
            if paths != tuple(sorted(set(paths))):
                raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
        if any(_normalize_evidence_path(path) != path for path in self.changed_paths):
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
        if any(_normalize_evidence_path(path) != path for path in self.uncommitted_paths):
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
        try:
            normalized_allowed = tuple(
                sorted(_normalize_policy_path(path) for path in self.allowed_paths)
            )
        except WorktreeLifecycleError as error:
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT) from error
        if self.allowed_paths != normalized_allowed:
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
        if (
            isinstance(self.max_changed_paths, bool)
            or not isinstance(self.max_changed_paths, int)
            or not 0 <= self.max_changed_paths <= _MAX_CHANGED_PATHS
            or not isinstance(self.changed_path_limit_exceeded, bool)
        ):
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
        expected_out_of_scope = tuple(
            path for path in self.changed_paths if not _path_is_allowed(path, self.allowed_paths)
        )
        if self.out_of_scope_paths != expected_out_of_scope or self.changed_path_limit_exceeded != (
            len(self.changed_paths) > self.max_changed_paths
        ):
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)

    @property
    def clean(self) -> bool:
        return not self.uncommitted_paths

    @property
    def path_policy_compliant(self) -> bool:
        return not self.out_of_scope_paths and not self.changed_path_limit_exceeded


@dataclass(frozen=True, slots=True)
class WorktreeRemoval:
    spec_digest: str
    lease_digest: str
    worktree_path: Path
    branch_name: str
    retained_head_commit: str
    disposition: Literal["removed"] = "removed"
    schema_version: Literal["blackcell.worktree-removal/v1"] = WORKTREE_REMOVAL_SCHEMA

    def __post_init__(self) -> None:
        if (
            self.schema_version != WORKTREE_REMOVAL_SCHEMA
            or self.disposition != "removed"
            or _DIGEST.fullmatch(self.spec_digest) is None
            or _DIGEST.fullmatch(self.lease_digest) is None
            or not isinstance(self.worktree_path, Path)
            or not self.worktree_path.is_absolute()
            or not self.branch_name.startswith("blackcell/alpha-worktree/")
            or _COMMIT_ID.fullmatch(self.retained_head_commit) is None
        ):
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)


def worktree_execution_spec_payload(spec: WorktreeExecutionSpec) -> dict[str, JsonInput]:
    """Return the complete durable input needed to reopen an execution checkout."""

    _require_spec(spec)
    return {
        "schema_version": spec.schema_version,
        "lease_digest": spec.lease.digest,
        "lease": {
            "schema_version": spec.lease.schema_version,
            "run_id": spec.lease.run_id,
            "node_id": spec.lease.node_id,
            "attempt": spec.lease.attempt,
            "fencing_token": spec.lease.fencing_token,
            "worker_id": spec.lease.worker_id,
        },
        "repository_root": str(spec.repository_root),
        "isolation_root": str(spec.isolation_root),
        "base_commit": spec.base_commit,
        "allowed_paths": list(spec.allowed_paths),
        "max_changed_paths": spec.max_changed_paths,
    }


def worktree_execution_spec_from_mapping(value: Mapping[str, object]) -> WorktreeExecutionSpec:
    """Strictly restore a durable worktree specification and revalidate local authority."""

    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "lease_digest",
        "lease",
        "repository_root",
        "isolation_root",
        "base_commit",
        "allowed_paths",
        "max_changed_paths",
    }:
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)
    raw_lease = value.get("lease")
    if not isinstance(raw_lease, Mapping) or set(raw_lease) != {
        "schema_version",
        "run_id",
        "node_id",
        "attempt",
        "fencing_token",
        "worker_id",
    }:
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)
    raw_allowed = value.get("allowed_paths")
    if not isinstance(raw_allowed, Sequence) or isinstance(raw_allowed, str | bytes | bytearray):
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)
    if (
        raw_lease.get("schema_version") != WORKTREE_LEASE_SCHEMA
        or value.get("schema_version") != WORKTREE_SPEC_SCHEMA
    ):
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)
    allowed_paths = tuple(_payload_text(item) for item in raw_allowed)
    lease = WorktreeLeaseIdentity(
        run_id=_payload_text(raw_lease.get("run_id")),
        node_id=_payload_text(raw_lease.get("node_id")),
        attempt=_payload_integer(raw_lease.get("attempt")),
        fencing_token=_payload_integer(raw_lease.get("fencing_token")),
        worker_id=_payload_text(raw_lease.get("worker_id")),
    )
    if value.get("lease_digest") != lease.digest:
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)
    return WorktreeExecutionSpec(
        lease=lease,
        repository_root=Path(_payload_text(value.get("repository_root"))),
        isolation_root=Path(_payload_text(value.get("isolation_root"))),
        base_commit=_payload_text(value.get("base_commit")),
        allowed_paths=allowed_paths,
        max_changed_paths=_payload_integer(value.get("max_changed_paths")),
    )


def worktree_inspection_payload(inspection: WorktreeInspection) -> dict[str, JsonInput]:
    """Return content-free durable evidence for a worktree observation."""

    if not isinstance(inspection, WorktreeInspection):
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
    return {
        "schema_version": inspection.schema_version,
        "spec_digest": inspection.spec_digest,
        "lease_digest": inspection.lease_digest,
        "worktree_path": str(inspection.worktree_path),
        "branch_name": inspection.branch_name,
        "base_commit": inspection.base_commit,
        "head_commit": inspection.head_commit,
        "allowed_paths": list(inspection.allowed_paths),
        "max_changed_paths": inspection.max_changed_paths,
        "changed_paths": list(inspection.changed_paths),
        "uncommitted_paths": list(inspection.uncommitted_paths),
        "out_of_scope_paths": list(inspection.out_of_scope_paths),
        "changed_path_limit_exceeded": inspection.changed_path_limit_exceeded,
    }


def worktree_inspection_from_mapping(value: Mapping[str, object]) -> WorktreeInspection:
    """Strictly restore durable worktree evidence without invoking Git."""

    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "spec_digest",
        "lease_digest",
        "worktree_path",
        "branch_name",
        "base_commit",
        "head_commit",
        "allowed_paths",
        "max_changed_paths",
        "changed_paths",
        "uncommitted_paths",
        "out_of_scope_paths",
        "changed_path_limit_exceeded",
    }:
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
    if value.get("schema_version") != WORKTREE_INSPECTION_SCHEMA:
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
    return WorktreeInspection(
        spec_digest=_payload_text(
            value.get("spec_digest"), code=WorktreeFailureCode.INVALID_GIT_OUTPUT
        ),
        lease_digest=_payload_text(
            value.get("lease_digest"), code=WorktreeFailureCode.INVALID_GIT_OUTPUT
        ),
        worktree_path=Path(
            _payload_text(value.get("worktree_path"), code=WorktreeFailureCode.INVALID_GIT_OUTPUT)
        ),
        branch_name=_payload_text(
            value.get("branch_name"), code=WorktreeFailureCode.INVALID_GIT_OUTPUT
        ),
        base_commit=_payload_text(
            value.get("base_commit"), code=WorktreeFailureCode.INVALID_GIT_OUTPUT
        ),
        head_commit=_payload_text(
            value.get("head_commit"), code=WorktreeFailureCode.INVALID_GIT_OUTPUT
        ),
        allowed_paths=_payload_text_sequence(
            value.get("allowed_paths"),
            code=WorktreeFailureCode.INVALID_GIT_OUTPUT,
        ),
        max_changed_paths=_payload_integer(
            value.get("max_changed_paths"), code=WorktreeFailureCode.INVALID_GIT_OUTPUT
        ),
        changed_paths=_payload_text_sequence(
            value.get("changed_paths"),
            code=WorktreeFailureCode.INVALID_GIT_OUTPUT,
        ),
        uncommitted_paths=_payload_text_sequence(
            value.get("uncommitted_paths"),
            code=WorktreeFailureCode.INVALID_GIT_OUTPUT,
        ),
        out_of_scope_paths=_payload_text_sequence(
            value.get("out_of_scope_paths"),
            code=WorktreeFailureCode.INVALID_GIT_OUTPUT,
        ),
        changed_path_limit_exceeded=_payload_boolean(value.get("changed_path_limit_exceeded")),
    )


def worktree_removal_payload(removal: WorktreeRemoval) -> dict[str, JsonInput]:
    """Return durable evidence that a checkout was removed while its branch survived."""

    if not isinstance(removal, WorktreeRemoval):
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
    return {
        "schema_version": removal.schema_version,
        "spec_digest": removal.spec_digest,
        "lease_digest": removal.lease_digest,
        "worktree_path": str(removal.worktree_path),
        "branch_name": removal.branch_name,
        "retained_head_commit": removal.retained_head_commit,
        "disposition": removal.disposition,
    }


def worktree_removal_from_mapping(value: Mapping[str, object]) -> WorktreeRemoval:
    """Strictly restore durable successful-checkout removal evidence."""

    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "spec_digest",
        "lease_digest",
        "worktree_path",
        "branch_name",
        "retained_head_commit",
        "disposition",
    }:
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
    if (
        value.get("schema_version") != WORKTREE_REMOVAL_SCHEMA
        or value.get("disposition") != "removed"
    ):
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
    return WorktreeRemoval(
        spec_digest=_payload_text(
            value.get("spec_digest"), code=WorktreeFailureCode.INVALID_GIT_OUTPUT
        ),
        lease_digest=_payload_text(
            value.get("lease_digest"), code=WorktreeFailureCode.INVALID_GIT_OUTPUT
        ),
        worktree_path=Path(
            _payload_text(value.get("worktree_path"), code=WorktreeFailureCode.INVALID_GIT_OUTPUT)
        ),
        branch_name=_payload_text(
            value.get("branch_name"), code=WorktreeFailureCode.INVALID_GIT_OUTPUT
        ),
        retained_head_commit=_payload_text(
            value.get("retained_head_commit"), code=WorktreeFailureCode.INVALID_GIT_OUTPUT
        ),
        disposition="removed",
    )


class GitProcessTransport(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        environment: Mapping[str, str] | None = None,
    ) -> BoundedProcessResult: ...


@dataclass(frozen=True, slots=True)
class GitWorktreeLifecycle:
    """Create and reconcile deterministic worktrees without executing project commands."""

    git_executable: Path | None = None
    transport: GitProcessTransport = field(default_factory=BoundedProcessRunner, repr=False)

    def __post_init__(self) -> None:
        executable = self.git_executable
        if executable is None:
            discovered = shutil.which("git")
            if discovered is None:
                raise WorktreeLifecycleError(WorktreeFailureCode.GIT_UNAVAILABLE)
            executable = Path(discovered)
        if not isinstance(executable, Path) or not executable.is_absolute():
            raise WorktreeLifecycleError(WorktreeFailureCode.GIT_UNAVAILABLE)
        try:
            executable = executable.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise WorktreeLifecycleError(WorktreeFailureCode.GIT_UNAVAILABLE) from error
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise WorktreeLifecycleError(WorktreeFailureCode.GIT_UNAVAILABLE)
        object.__setattr__(self, "git_executable", executable)

    def create(self, spec: WorktreeExecutionSpec) -> WorktreeInspection:
        """Create or reopen the exact worktree named by an immutable execution spec."""

        _require_spec(spec)
        self._validate_repository(spec)
        _prepare_isolation_root(spec.isolation_root)
        worktree_path = spec.worktree_path
        if _lexists(worktree_path):
            return self.inspect(spec)

        branch_exists = self._branch_exists(spec)
        if branch_exists:
            result = self._git(
                spec,
                ("worktree", "add", str(worktree_path), spec.branch_name),
            )
        else:
            result = self._git(
                spec,
                (
                    "worktree",
                    "add",
                    "--no-track",
                    "-b",
                    spec.branch_name,
                    str(worktree_path),
                    spec.base_commit,
                ),
            )
        if result.return_code != 0:
            if _lexists(worktree_path):
                try:
                    return self.inspect(spec)
                except WorktreeLifecycleError as error:
                    raise WorktreeLifecycleError(WorktreeFailureCode.WORKTREE_CONFLICT) from error
            raise WorktreeLifecycleError(WorktreeFailureCode.WORKTREE_CONFLICT)
        return self.inspect(spec)

    def inspect(self, spec: WorktreeExecutionSpec) -> WorktreeInspection:
        """Read exact branch, head, dirtiness, and total base-relative path effects."""

        _require_spec(spec)
        self._validate_repository(spec)
        worktree_path = spec.worktree_path
        _require_worktree_directory(spec)

        actual_root = self._single_line(
            self._require_success(
                self._git(spec, ("-C", str(worktree_path), "rev-parse", "--show-toplevel"))
            )
        )
        try:
            resolved_root = Path(actual_root).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT) from error
        if resolved_root != worktree_path:
            raise WorktreeLifecycleError(WorktreeFailureCode.WORKTREE_CONFLICT)

        branch_result = self._git(
            spec,
            ("-C", str(worktree_path), "symbolic-ref", "--quiet", "--short", "HEAD"),
        )
        if branch_result.return_code != 0 or self._single_line(branch_result) != spec.branch_name:
            raise WorktreeLifecycleError(WorktreeFailureCode.WORKTREE_CONFLICT)

        head_result = self._git(spec, ("-C", str(worktree_path), "rev-parse", "HEAD"))
        if head_result.return_code != 0:
            raise WorktreeLifecycleError(WorktreeFailureCode.WORKTREE_CONFLICT)
        head_commit = self._single_line(head_result)
        if _COMMIT_ID.fullmatch(head_commit) is None:
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)

        ancestor = self._git(
            spec,
            (
                "-C",
                str(worktree_path),
                "merge-base",
                "--is-ancestor",
                spec.base_commit,
                head_commit,
            ),
        )
        if ancestor.return_code != 0:
            raise WorktreeLifecycleError(WorktreeFailureCode.WORKTREE_CONFLICT)

        base_relative = self._path_output(
            self._require_success(
                self._git(
                    spec,
                    (
                        "-C",
                        str(worktree_path),
                        "diff",
                        "--name-only",
                        "--no-renames",
                        "-z",
                        spec.base_commit,
                        "--",
                    ),
                )
            )
        )
        head_relative = self._path_output(
            self._require_success(
                self._git(
                    spec,
                    (
                        "-C",
                        str(worktree_path),
                        "diff",
                        "--name-only",
                        "--no-renames",
                        "-z",
                        "HEAD",
                        "--",
                    ),
                )
            )
        )
        untracked = self._path_output(
            self._require_success(
                self._git(
                    spec,
                    ("-C", str(worktree_path), "ls-files", "--others", "-z", "--"),
                )
            )
        )
        changed_paths = tuple(sorted(set(base_relative) | set(untracked)))
        uncommitted_paths = tuple(sorted(set(head_relative) | set(untracked)))
        out_of_scope = tuple(
            path for path in changed_paths if not _path_is_allowed(path, spec.allowed_paths)
        )
        return WorktreeInspection(
            spec_digest=spec.digest,
            lease_digest=spec.lease.digest,
            worktree_path=worktree_path,
            branch_name=spec.branch_name,
            base_commit=spec.base_commit,
            head_commit=head_commit,
            allowed_paths=spec.allowed_paths,
            max_changed_paths=spec.max_changed_paths,
            changed_paths=changed_paths,
            uncommitted_paths=uncommitted_paths,
            out_of_scope_paths=out_of_scope,
            changed_path_limit_exceeded=len(changed_paths) > spec.max_changed_paths,
        )

    def retain(self, spec: WorktreeExecutionSpec) -> WorktreeInspection:
        """Validate and retain failed or cancelled work without cleanup side effects."""

        return self.inspect(spec)

    def commit_changes(self, spec: WorktreeExecutionSpec) -> WorktreeInspection:
        """Commit an admitted worktree with fixed host identity and no repository hooks."""

        before = self.inspect(spec)
        if not before.path_policy_compliant:
            raise WorktreeLifecycleError(WorktreeFailureCode.PATH_POLICY_VIOLATION)
        if before.clean:
            return before

        staged = self._git(
            spec,
            ("-C", str(spec.worktree_path), "add", "--all", "--", "."),
        )
        if staged.return_code != 0:
            raise WorktreeLifecycleError(WorktreeFailureCode.COMMIT_FAILED)
        message = (
            f"BlackCell alpha {spec.lease.run_id}/{spec.lease.node_id} attempt {spec.lease.attempt}"
        )
        committed = self._git(
            spec,
            (
                "-C",
                str(spec.worktree_path),
                "-c",
                "user.name=BlackCell",
                "-c",
                "user.email=blackcell@example.invalid",
                "-c",
                "commit.gpgSign=false",
                "commit",
                "--no-verify",
                "--no-gpg-sign",
                "--cleanup=verbatim",
                f"--message={message}",
            ),
        )
        if committed.return_code != 0:
            raise WorktreeLifecycleError(WorktreeFailureCode.COMMIT_FAILED)

        after = self.inspect(spec)
        if (
            not after.clean
            or not after.path_policy_compliant
            or after.head_commit == before.head_commit
            or after.changed_paths != before.changed_paths
        ):
            raise WorktreeLifecycleError(WorktreeFailureCode.COMMIT_FAILED)
        return after

    def remove_success(
        self,
        spec: WorktreeExecutionSpec,
        *,
        expected_head_commit: str | None = None,
    ) -> WorktreeRemoval:
        """Idempotently remove a proven successful checkout while preserving its branch."""

        _require_spec(spec)
        if expected_head_commit is not None and _COMMIT_ID.fullmatch(expected_head_commit) is None:
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)
        if not _lexists(spec.worktree_path):
            if expected_head_commit is None:
                raise WorktreeLifecycleError(WorktreeFailureCode.WORKTREE_NOT_FOUND)
            return self._confirm_success_removed(spec, expected_head_commit)

        inspection = self.inspect(spec)
        if not inspection.clean:
            raise WorktreeLifecycleError(WorktreeFailureCode.WORKTREE_DIRTY)
        if not inspection.path_policy_compliant:
            raise WorktreeLifecycleError(WorktreeFailureCode.PATH_POLICY_VIOLATION)
        if expected_head_commit is not None and inspection.head_commit != expected_head_commit:
            raise WorktreeLifecycleError(WorktreeFailureCode.CLEANUP_FAILED)

        result = self._git(spec, ("worktree", "remove", str(spec.worktree_path)))
        if result.return_code != 0 or _lexists(spec.worktree_path):
            raise WorktreeLifecycleError(WorktreeFailureCode.CLEANUP_FAILED)
        return self._confirm_success_removed(spec, inspection.head_commit)

    def _confirm_success_removed(
        self,
        spec: WorktreeExecutionSpec,
        expected_head_commit: str,
    ) -> WorktreeRemoval:
        if _lexists(spec.worktree_path) or self._worktree_is_registered(spec):
            raise WorktreeLifecycleError(WorktreeFailureCode.CLEANUP_FAILED)
        branch_head = self._git(
            spec,
            ("rev-parse", "--verify", f"refs/heads/{spec.branch_name}^{{commit}}"),
        )
        if branch_head.return_code != 0 or self._single_line(branch_head) != expected_head_commit:
            raise WorktreeLifecycleError(WorktreeFailureCode.CLEANUP_FAILED)
        return WorktreeRemoval(
            spec_digest=spec.digest,
            lease_digest=spec.lease.digest,
            worktree_path=spec.worktree_path,
            branch_name=spec.branch_name,
            retained_head_commit=expected_head_commit,
        )

    def _worktree_is_registered(self, spec: WorktreeExecutionSpec) -> bool:
        result = self._require_success(self._git(spec, ("worktree", "list", "--porcelain", "-z")))
        payload = result.stdout.captured
        if not payload or not payload.endswith(b"\x00"):
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
        observed_worktree = False
        for entry in payload[:-1].split(b"\x00"):
            if not entry.startswith(b"worktree "):
                continue
            observed_worktree = True
            try:
                path = entry.removeprefix(b"worktree ").decode("utf-8")
            except UnicodeDecodeError as error:
                raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT) from error
            if not path or "\r" in path or "\n" in path or not Path(path).is_absolute():
                raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
            if path == str(spec.worktree_path):
                return True
        if not observed_worktree:
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
        return False

    def _validate_repository(self, spec: WorktreeExecutionSpec) -> None:
        root = self._git(spec, ("rev-parse", "--show-toplevel"))
        if root.return_code != 0:
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_REPOSITORY)
        try:
            resolved_root = Path(self._single_line(root)).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_REPOSITORY) from error
        if resolved_root != spec.repository_root:
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_REPOSITORY)

        commit = self._git(spec, ("cat-file", "-e", f"{spec.base_commit}^{{commit}}"))
        if commit.return_code != 0:
            raise WorktreeLifecycleError(WorktreeFailureCode.BASE_COMMIT_NOT_FOUND)

        filters = self._git(
            spec,
            (
                "config",
                "--local",
                "--includes",
                "--name-only",
                "--null",
                "--get-regexp",
                _EXTERNAL_FILTER_PATTERN,
            ),
        )
        if filters.return_code == 0:
            raise WorktreeLifecycleError(WorktreeFailureCode.UNSAFE_REPOSITORY_CONFIGURATION)
        if filters.return_code != 1:
            raise WorktreeLifecycleError(WorktreeFailureCode.GIT_COMMAND_FAILED)

    def _branch_exists(self, spec: WorktreeExecutionSpec) -> bool:
        result = self._git(
            spec,
            ("show-ref", "--verify", "--quiet", f"refs/heads/{spec.branch_name}"),
        )
        if result.return_code not in {0, 1}:
            raise WorktreeLifecycleError(WorktreeFailureCode.GIT_COMMAND_FAILED)
        return result.return_code == 0

    def _git(
        self,
        spec: WorktreeExecutionSpec,
        arguments: tuple[str, ...],
    ) -> BoundedProcessResult:
        executable = self.git_executable
        if executable is None:  # pragma: no cover - post-init invariant
            raise WorktreeLifecycleError(WorktreeFailureCode.GIT_UNAVAILABLE)
        argv = (
            str(executable),
            "--no-pager",
            "--literal-pathspecs",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            *arguments,
        )
        environment = {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "LC_ALL": "C",
        }
        try:
            result = self.transport.run(
                argv,
                cwd=spec.repository_root,
                timeout_seconds=_GIT_TIMEOUT_SECONDS,
                stdout_limit_bytes=_GIT_STDOUT_LIMIT_BYTES,
                stderr_limit_bytes=_GIT_STDERR_LIMIT_BYTES,
                environment=environment,
            )
        except BoundedProcessError as error:
            mapping = {
                BoundedProcessFailureCode.INVALID_INVOCATION: (
                    WorktreeFailureCode.GIT_COMMAND_FAILED
                ),
                BoundedProcessFailureCode.SPAWN_FAILED: WorktreeFailureCode.GIT_SPAWN_FAILED,
                BoundedProcessFailureCode.TIMED_OUT: WorktreeFailureCode.GIT_TIMED_OUT,
                BoundedProcessFailureCode.OUTPUT_TOO_LARGE: (
                    WorktreeFailureCode.GIT_OUTPUT_TOO_LARGE
                ),
                BoundedProcessFailureCode.OUTPUT_INCOMPLETE: (
                    WorktreeFailureCode.GIT_OUTPUT_INCOMPLETE
                ),
            }
            raise WorktreeLifecycleError(mapping[error.code]) from error
        for stream in (result.stdout, result.stderr):
            if not stream.complete or stream.total_bytes is None:
                raise WorktreeLifecycleError(WorktreeFailureCode.GIT_OUTPUT_INCOMPLETE)
            if stream.total_bytes != len(stream.captured):
                raise WorktreeLifecycleError(WorktreeFailureCode.GIT_OUTPUT_TOO_LARGE)
        return result

    @staticmethod
    def _single_line(result: BoundedProcessResult) -> str:
        try:
            value = result.stdout.captured.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT) from error
        if value.endswith("\n"):
            value = value[:-1]
        if not value or "\n" in value or "\r" in value or "\x00" in value:
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
        return value

    @staticmethod
    def _path_output(result: BoundedProcessResult) -> tuple[str, ...]:
        payload = result.stdout.captured
        if not payload:
            return ()
        if not payload.endswith(b"\x00"):
            raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
        paths: list[str] = []
        for raw_path in payload[:-1].split(b"\x00"):
            if not raw_path:
                raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
            try:
                path = raw_path.decode("utf-8")
            except UnicodeDecodeError as error:
                raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT) from error
            paths.append(_normalize_evidence_path(path))
        return tuple(paths)

    @staticmethod
    def _require_success(result: BoundedProcessResult) -> BoundedProcessResult:
        if result.return_code != 0:
            raise WorktreeLifecycleError(WorktreeFailureCode.GIT_COMMAND_FAILED)
        return result


def _require_spec(spec: WorktreeExecutionSpec) -> None:
    if not isinstance(spec, WorktreeExecutionSpec):
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)


def _canonical_repository_root(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_REPOSITORY)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_REPOSITORY) from error
    if resolved != path or not resolved.is_dir():
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_REPOSITORY)
    return resolved


def _canonical_isolation_root(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_ISOLATION_ROOT)
    try:
        if path.exists():
            resolved = path.resolve(strict=True)
            _require_owner_directory(resolved)
        else:
            parent = path.parent.resolve(strict=True)
            if not parent.is_dir() or parent / path.name != path:
                raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_ISOLATION_ROOT)
            resolved = path
    except (OSError, RuntimeError) as error:
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_ISOLATION_ROOT) from error
    if resolved != path:
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_ISOLATION_ROOT)
    return resolved


def _prepare_isolation_root(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        _require_owner_directory(path)
    except OSError as error:
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_ISOLATION_ROOT) from error


def _require_owner_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_ISOLATION_ROOT)


def _require_worktree_directory(spec: WorktreeExecutionSpec) -> None:
    path = spec.worktree_path
    if not _lexists(path):
        raise WorktreeLifecycleError(WorktreeFailureCode.WORKTREE_NOT_FOUND)
    if path.is_symlink():
        raise WorktreeLifecycleError(WorktreeFailureCode.WORKTREE_CONFLICT)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise WorktreeLifecycleError(WorktreeFailureCode.WORKTREE_CONFLICT) from error
    if resolved != path or not path.is_dir() or path.parent != spec.isolation_root:
        raise WorktreeLifecycleError(WorktreeFailureCode.WORKTREE_CONFLICT)


def _normalize_policy_path(value: str) -> str:
    if value == ".":
        return value
    return _normalize_repository_path(value)


def _normalize_repository_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_REPOSITORY_PATH_CHARS
        or "\x00" in value
        or "\\" in value
    ):
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or ".git" in path.parts
    ):
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_SPEC)
    return value


def _normalize_evidence_path(value: str) -> str:
    try:
        return _normalize_repository_path(value)
    except WorktreeLifecycleError as error:
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT) from error


def _path_is_allowed(path: str, allowed_paths: tuple[str, ...]) -> bool:
    return any(
        allowed == "." or path == allowed or path.startswith(f"{allowed}/")
        for allowed in allowed_paths
    )


def _payload_text(
    value: object, *, code: WorktreeFailureCode = WorktreeFailureCode.INVALID_SPEC
) -> str:
    if not isinstance(value, str):
        raise WorktreeLifecycleError(code)
    return value


def _payload_integer(
    value: object, *, code: WorktreeFailureCode = WorktreeFailureCode.INVALID_SPEC
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorktreeLifecycleError(code)
    return value


def _payload_text_sequence(
    value: object,
    *,
    code: WorktreeFailureCode,
) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise WorktreeLifecycleError(code)
    return tuple(_payload_text(item, code=code) for item in value)


def _payload_boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise WorktreeLifecycleError(WorktreeFailureCode.INVALID_GIT_OUTPUT)
    return value


def _lexists(path: Path) -> bool:
    return path.exists() or path.is_symlink()
