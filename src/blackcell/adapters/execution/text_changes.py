"""Host-owned UTF-8 file effects for inert alpha change proposals.

No provider code or project command runs here. Every operation is preflighted against the exact
fresh worktree before the first mutation, and each file effect is atomic. This is still not an
operating-system process sandbox; acceptance commands require a separate A04 boundary.
"""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from blackcell.adapters.execution.worktree import (
    GitWorktreeLifecycle,
    WorktreeExecutionSpec,
    WorktreeInspection,
)
from blackcell.kernel import JsonInput
from blackcell.kernel._json import bytes_digest, canonical_json_bytes
from blackcell.orchestration.alpha_changes import (
    MAX_ALPHA_TEXT_CHANGE_RESULT_BYTES,
    AlphaChangeProposal,
    AlphaFileChange,
    AlphaTextOperation,
)

TEXT_CHANGE_ADMISSION_SCHEMA = "alpha-text-change-admission/v1"
TEXT_CHANGE_RESULT_SCHEMA = "alpha-text-change-result/v1"

_MAX_FILE_BYTES = 1024 * 1024


class TextChangeFailureCode(StrEnum):
    INVALID_ADMISSION = "invalid-text-change-admission"
    WORKTREE_NOT_FRESH = "text-change-worktree-not-fresh"
    OPERATION_LIMIT_EXCEEDED = "text-change-operation-limit-exceeded"
    PATH_POLICY_VIOLATION = "text-change-path-policy-violation"
    TARGET_CONFLICT = "text-change-target-conflict"
    TARGET_NOT_REGULAR_TEXT = "text-change-target-not-regular-text"
    BEFORE_DIGEST_MISMATCH = "text-change-before-digest-mismatch"
    EFFECT_FAILED_ROLLED_BACK = "text-change-effect-failed-rolled-back"
    EFFECT_EVIDENCE_MISMATCH = "text-change-effect-evidence-mismatch"
    EFFECT_UNCERTAIN = "text-change-effect-uncertain"


class TextChangeExecutionError(RuntimeError):
    """A content-free file-execution failure."""

    def __init__(self, code: TextChangeFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class TextChangeAdmission:
    worktree_spec_digest: str
    lease_digest: str
    evidence_digest: str
    proposal_digest: str
    schema_version: str = TEXT_CHANGE_ADMISSION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != TEXT_CHANGE_ADMISSION_SCHEMA or not all(
            _is_digest(value)
            for value in (
                self.worktree_spec_digest,
                self.lease_digest,
                self.evidence_digest,
                self.proposal_digest,
            )
        ):
            raise TextChangeExecutionError(TextChangeFailureCode.INVALID_ADMISSION)


@dataclass(frozen=True, slots=True)
class TextChangeEffect:
    operation: AlphaTextOperation
    path: str
    before_digest: str | None
    after_digest: str | None


@dataclass(frozen=True, slots=True)
class TextChangeExecutionResult:
    worktree_spec_digest: str
    lease_digest: str
    evidence_digest: str
    proposal_digest: str
    head_commit: str
    effects: tuple[TextChangeEffect, ...]
    changed_paths: tuple[str, ...]
    status: Literal["applied"] = "applied"
    schema_version: str = TEXT_CHANGE_RESULT_SCHEMA
    result_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            self.schema_version != TEXT_CHANGE_RESULT_SCHEMA
            or self.status != "applied"
            or len(self.head_commit) != 40
            or not all(character in "0123456789abcdef" for character in self.head_commit)
            or not all(
                _is_digest(value)
                for value in (
                    self.worktree_spec_digest,
                    self.lease_digest,
                    self.evidence_digest,
                    self.proposal_digest,
                )
            )
            or not self.effects
            or self.changed_paths != tuple(sorted(set(self.changed_paths)))
            or tuple(effect.path for effect in self.effects) != self.changed_paths
        ):
            raise TextChangeExecutionError(TextChangeFailureCode.EFFECT_EVIDENCE_MISMATCH)
        payload = canonical_json_bytes(text_change_result_payload(self))
        if len(payload) > MAX_ALPHA_TEXT_CHANGE_RESULT_BYTES:
            raise TextChangeExecutionError(TextChangeFailureCode.EFFECT_EVIDENCE_MISMATCH)
        object.__setattr__(self, "result_digest", bytes_digest(payload))


class TextFileEffects(Protocol):
    def create(self, path: Path, data: bytes, *, mode: int) -> None: ...

    def replace(self, path: Path, data: bytes, *, mode: int) -> None: ...

    def delete(self, path: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class AtomicTextFileEffects:
    """Apply one regular-file effect atomically within an already validated parent."""

    def create(self, path: Path, data: bytes, *, mode: int) -> None:
        temporary = _write_temporary(path.parent, data, mode)
        try:
            os.link(temporary, path, follow_symlinks=False)
            _fsync_directory(path.parent)
        finally:
            with suppress(OSError):
                temporary.unlink()

    def replace(self, path: Path, data: bytes, *, mode: int) -> None:
        temporary = _write_temporary(path.parent, data, mode)
        try:
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        finally:
            with suppress(OSError):
                temporary.unlink()

    def delete(self, path: Path) -> None:
        path.unlink()
        _fsync_directory(path.parent)


@dataclass(frozen=True, slots=True)
class _PlannedEffect:
    change: AlphaFileChange
    target: Path
    before: bytes | None = field(repr=False)
    before_mode: int | None
    after: bytes | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class TextChangeExecutor:
    lifecycle: GitWorktreeLifecycle
    effects: TextFileEffects = field(default_factory=AtomicTextFileEffects, repr=False)

    def execute(
        self,
        spec: WorktreeExecutionSpec,
        proposal: AlphaChangeProposal,
        admission: TextChangeAdmission,
    ) -> TextChangeExecutionResult:
        self._validate_binding(spec, proposal, admission)
        before = self.lifecycle.inspect(spec)
        if not before.clean or before.changed_paths:
            raise TextChangeExecutionError(TextChangeFailureCode.WORKTREE_NOT_FRESH)
        plans = self._preflight(spec, proposal)
        applied: list[_PlannedEffect] = []
        try:
            for plan in plans:
                applied.append(plan)
                self._apply(plan)
        except Exception as error:
            if self._rollback(applied, before, spec):
                raise TextChangeExecutionError(
                    TextChangeFailureCode.EFFECT_FAILED_ROLLED_BACK
                ) from error
            raise TextChangeExecutionError(TextChangeFailureCode.EFFECT_UNCERTAIN) from error

        try:
            after = self.lifecycle.inspect(spec)
            expected_paths = tuple(plan.change.path for plan in plans)
            if (
                after.head_commit != before.head_commit
                or after.changed_paths != expected_paths
                or after.uncommitted_paths != expected_paths
                or not after.path_policy_compliant
            ):
                raise TextChangeExecutionError(TextChangeFailureCode.EFFECT_EVIDENCE_MISMATCH)
        except Exception as error:
            if self._rollback(applied, before, spec):
                raise TextChangeExecutionError(
                    TextChangeFailureCode.EFFECT_EVIDENCE_MISMATCH
                ) from error
            raise TextChangeExecutionError(TextChangeFailureCode.EFFECT_UNCERTAIN) from error

        effects = tuple(
            TextChangeEffect(
                operation=plan.change.operation,
                path=plan.change.path,
                before_digest=None if plan.before is None else bytes_digest(plan.before),
                after_digest=None if plan.after is None else bytes_digest(plan.after),
            )
            for plan in plans
        )
        return TextChangeExecutionResult(
            worktree_spec_digest=spec.digest,
            lease_digest=spec.lease.digest,
            evidence_digest=proposal.evidence_digest,
            proposal_digest=proposal.digest,
            head_commit=after.head_commit,
            effects=effects,
            changed_paths=after.changed_paths,
        )

    @staticmethod
    def _validate_binding(
        spec: WorktreeExecutionSpec,
        proposal: AlphaChangeProposal,
        admission: TextChangeAdmission,
    ) -> None:
        if (
            not isinstance(spec, WorktreeExecutionSpec)
            or not isinstance(proposal, AlphaChangeProposal)
            or not isinstance(admission, TextChangeAdmission)
            or admission.worktree_spec_digest != spec.digest
            or admission.lease_digest != spec.lease.digest
            or admission.evidence_digest != proposal.evidence_digest
            or admission.proposal_digest != proposal.digest
        ):
            raise TextChangeExecutionError(TextChangeFailureCode.INVALID_ADMISSION)

    @staticmethod
    def _preflight(
        spec: WorktreeExecutionSpec,
        proposal: AlphaChangeProposal,
    ) -> tuple[_PlannedEffect, ...]:
        if len(proposal.operations) > spec.max_changed_paths:
            raise TextChangeExecutionError(TextChangeFailureCode.OPERATION_LIMIT_EXCEEDED)
        plans: list[_PlannedEffect] = []
        for change in proposal.operations:
            if not _path_is_allowed(change.path, spec.allowed_paths):
                raise TextChangeExecutionError(TextChangeFailureCode.PATH_POLICY_VIOLATION)
            target = spec.worktree_path.joinpath(*change.path.split("/"))
            _require_existing_regular_parent(spec.worktree_path, target.parent)
            exists = _lexists(target)
            if change.operation is AlphaTextOperation.CREATE:
                if exists:
                    raise TextChangeExecutionError(TextChangeFailureCode.TARGET_CONFLICT)
                before = None
                mode = None
            else:
                if not exists or target.is_symlink():
                    raise TextChangeExecutionError(TextChangeFailureCode.TARGET_NOT_REGULAR_TEXT)
                before, mode = _read_regular_text(target)
                if bytes_digest(before) != change.expected_digest:
                    raise TextChangeExecutionError(TextChangeFailureCode.BEFORE_DIGEST_MISMATCH)
            after = None if change.content is None else change.content.encode("utf-8")
            plans.append(_PlannedEffect(change, target, before, mode, after))
        return tuple(plans)

    def _apply(self, plan: _PlannedEffect) -> None:
        if plan.change.operation is AlphaTextOperation.CREATE:
            if plan.after is None:  # pragma: no cover - proposal contract invariant
                raise TextChangeExecutionError(TextChangeFailureCode.INVALID_ADMISSION)
            self.effects.create(plan.target, plan.after, mode=0o644)
        elif plan.change.operation is AlphaTextOperation.REPLACE:
            if plan.after is None or plan.before_mode is None:  # pragma: no cover
                raise TextChangeExecutionError(TextChangeFailureCode.INVALID_ADMISSION)
            self.effects.replace(plan.target, plan.after, mode=plan.before_mode)
        else:
            self.effects.delete(plan.target)

    def _rollback(
        self,
        applied: list[_PlannedEffect],
        before: WorktreeInspection,
        spec: WorktreeExecutionSpec,
    ) -> bool:
        try:
            for plan in reversed(applied):
                if plan.change.operation is AlphaTextOperation.CREATE:
                    if _lexists(plan.target):
                        if plan.target.is_symlink() or not plan.target.is_file():
                            return False
                        self.effects.delete(plan.target)
                else:
                    if plan.before is None or plan.before_mode is None:
                        return False
                    if _lexists(plan.target):
                        if plan.target.is_symlink() or not plan.target.is_file():
                            return False
                        self.effects.replace(plan.target, plan.before, mode=plan.before_mode)
                    else:
                        self.effects.create(plan.target, plan.before, mode=plan.before_mode)
            return self.lifecycle.inspect(spec) == before
        except Exception:
            return False


def text_change_result_payload(result: TextChangeExecutionResult) -> dict[str, JsonInput]:
    return {
        "schema_version": result.schema_version,
        "status": result.status,
        "worktree_spec_digest": result.worktree_spec_digest,
        "lease_digest": result.lease_digest,
        "evidence_digest": result.evidence_digest,
        "proposal_digest": result.proposal_digest,
        "head_commit": result.head_commit,
        "effects": [
            {
                "operation": effect.operation.value,
                "path": effect.path,
                "before_digest": effect.before_digest,
                "after_digest": effect.after_digest,
            }
            for effect in result.effects
        ],
        "changed_paths": list(result.changed_paths),
    }


def _require_existing_regular_parent(worktree_root: Path, parent: Path) -> None:
    if parent == worktree_root:
        return
    current = worktree_root
    try:
        relative_parts = parent.relative_to(worktree_root).parts
    except ValueError as error:  # pragma: no cover - validated proposal path invariant
        raise TextChangeExecutionError(TextChangeFailureCode.PATH_POLICY_VIOLATION) from error
    for part in relative_parts:
        current = current / part
        if not _lexists(current) or current.is_symlink():
            raise TextChangeExecutionError(TextChangeFailureCode.TARGET_NOT_REGULAR_TEXT)
        try:
            metadata = current.lstat()
        except OSError as error:
            raise TextChangeExecutionError(TextChangeFailureCode.TARGET_NOT_REGULAR_TEXT) from error
        if not stat.S_ISDIR(metadata.st_mode):
            raise TextChangeExecutionError(TextChangeFailureCode.TARGET_NOT_REGULAR_TEXT)


def _read_regular_text(path: Path) -> tuple[bytes, int]:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_FILE_BYTES:
            raise TextChangeExecutionError(TextChangeFailureCode.TARGET_NOT_REGULAR_TEXT)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = None
            data = handle.read(_MAX_FILE_BYTES + 1)
    except TextChangeExecutionError:
        raise
    except OSError as error:
        raise TextChangeExecutionError(TextChangeFailureCode.TARGET_NOT_REGULAR_TEXT) from error
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
    if len(data) > _MAX_FILE_BYTES:
        raise TextChangeExecutionError(TextChangeFailureCode.TARGET_NOT_REGULAR_TEXT)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TextChangeExecutionError(TextChangeFailureCode.TARGET_NOT_REGULAR_TEXT) from error
    if "\x00" in text:
        raise TextChangeExecutionError(TextChangeFailureCode.TARGET_NOT_REGULAR_TEXT)
    return data, stat.S_IMODE(metadata.st_mode)


def _write_temporary(parent: Path, data: bytes, mode: int) -> Path:
    temporary = parent / f".blackcell-{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        with suppress(OSError):
            temporary.unlink()
        raise
    return temporary


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _path_is_allowed(path: str, allowed_paths: tuple[str, ...]) -> bool:
    return any(
        allowed == "." or path == allowed or path.startswith(f"{allowed}/")
        for allowed in allowed_paths
    )


def _is_digest(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
        return False
    try:
        int(value.removeprefix("sha256:"), 16)
    except ValueError:
        return False
    return True


def _lexists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


__all__ = [
    "TEXT_CHANGE_ADMISSION_SCHEMA",
    "TEXT_CHANGE_RESULT_SCHEMA",
    "AtomicTextFileEffects",
    "TextChangeAdmission",
    "TextChangeEffect",
    "TextChangeExecutionError",
    "TextChangeExecutionResult",
    "TextChangeExecutor",
    "TextChangeFailureCode",
    "TextFileEffects",
    "text_change_result_payload",
]
