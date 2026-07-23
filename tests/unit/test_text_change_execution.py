from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from blackcell.adapters.execution.text_changes import (
    AtomicTextFileEffects,
    TextChangeAdmission,
    TextChangeExecutionError,
    TextChangeExecutor,
    TextChangeFailureCode,
)
from blackcell.adapters.execution.worktree import (
    GitWorktreeLifecycle,
    WorktreeExecutionSpec,
    WorktreeLeaseIdentity,
)
from blackcell.kernel._json import bytes_digest
from blackcell.orchestration.alpha_changes import (
    AlphaChangeContractError,
    AlphaChangeProposal,
    AlphaFileChange,
    AlphaTextOperation,
)


@dataclass(frozen=True, slots=True)
class GitRepository:
    root: Path
    isolation_root: Path
    base_commit: str


class FailSecondEffect:
    def __init__(self) -> None:
        self.delegate = AtomicTextFileEffects()
        self.calls = 0

    def create(self, path: Path, data: bytes, *, mode: int) -> None:
        self._fail_once()
        self.delegate.create(path, data, mode=mode)

    def replace(self, path: Path, data: bytes, *, mode: int) -> None:
        self._fail_once()
        self.delegate.replace(path, data, mode=mode)

    def delete(self, path: Path) -> None:
        self._fail_once()
        self.delegate.delete(path)

    def _fail_once(self) -> None:
        self.calls += 1
        if self.calls == 2:
            raise OSError("injected effect failure")


def test_executor_applies_create_replace_delete_and_reports_exact_delta(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    spec = _spec(repository, max_changed_paths=3)
    lifecycle = GitWorktreeLifecycle()
    worktree = lifecycle.create(spec).worktree_path
    proposal = _proposal(
        AlphaFileChange(AlphaTextOperation.CREATE, "src/created.txt", None, "created\n"),
        AlphaFileChange(
            AlphaTextOperation.REPLACE,
            "src/replace.txt",
            bytes_digest(b"old\n"),
            "new\n",
        ),
        AlphaFileChange(
            AlphaTextOperation.DELETE,
            "src/delete.txt",
            bytes_digest(b"delete me\n"),
            None,
        ),
    )

    result = TextChangeExecutor(lifecycle).execute(spec, proposal, _admission(spec, proposal))

    assert (worktree / "src" / "created.txt").read_text() == "created\n"
    assert (worktree / "src" / "replace.txt").read_text() == "new\n"
    assert not (worktree / "src" / "delete.txt").exists()
    assert result.status == "applied"
    assert result.changed_paths == (
        "src/created.txt",
        "src/delete.txt",
        "src/replace.txt",
    )
    assert tuple(effect.operation for effect in result.effects) == (
        AlphaTextOperation.CREATE,
        AlphaTextOperation.DELETE,
        AlphaTextOperation.REPLACE,
    )
    assert result.effects[0].before_digest is None
    assert result.effects[0].after_digest == bytes_digest(b"created\n")
    assert result.effects[1].before_digest == bytes_digest(b"delete me\n")
    assert result.effects[1].after_digest is None
    assert result.result_digest.startswith("sha256:")
    inspection = lifecycle.inspect(spec)
    assert inspection.changed_paths == result.changed_paths
    assert not inspection.clean
    assert inspection.path_policy_compliant


def test_executor_preflights_all_operations_before_any_mutation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    spec = _spec(repository, max_changed_paths=2)
    lifecycle = GitWorktreeLifecycle()
    worktree = lifecycle.create(spec).worktree_path
    proposal = _proposal(
        AlphaFileChange(
            AlphaTextOperation.REPLACE,
            "src/delete.txt",
            bytes_digest(b"delete me\n"),
            "would have changed\n",
        ),
        AlphaFileChange(
            AlphaTextOperation.REPLACE,
            "src/replace.txt",
            "sha256:" + "9" * 64,
            "stale\n",
        ),
    )

    with pytest.raises(TextChangeExecutionError) as caught:
        TextChangeExecutor(lifecycle).execute(spec, proposal, _admission(spec, proposal))

    assert caught.value.code is TextChangeFailureCode.BEFORE_DIGEST_MISMATCH
    assert (worktree / "src" / "delete.txt").read_text() == "delete me\n"
    assert (worktree / "src" / "replace.txt").read_text() == "old\n"
    assert lifecycle.inspect(spec).changed_paths == ()


def test_executor_rejects_git_metadata_out_of_scope_and_symlink_targets(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    spec = _spec(repository, allowed_paths=("src",), max_changed_paths=1)
    lifecycle = GitWorktreeLifecycle()
    lifecycle.create(spec)

    with pytest.raises(AlphaChangeContractError):
        AlphaFileChange(AlphaTextOperation.CREATE, ".git/config", None, "forbidden\n")

    outside = _proposal(
        AlphaFileChange(
            AlphaTextOperation.REPLACE,
            "outside.txt",
            bytes_digest(b"outside\n"),
            "changed\n",
        )
    )
    with pytest.raises(TextChangeExecutionError) as outside_error:
        TextChangeExecutor(lifecycle).execute(spec, outside, _admission(spec, outside))
    assert outside_error.value.code is TextChangeFailureCode.PATH_POLICY_VIOLATION

    symlink = _proposal(
        AlphaFileChange(
            AlphaTextOperation.REPLACE,
            "src/link.txt",
            bytes_digest(b"old\n"),
            "changed\n",
        )
    )
    with pytest.raises(TextChangeExecutionError) as symlink_error:
        TextChangeExecutor(lifecycle).execute(spec, symlink, _admission(spec, symlink))
    assert symlink_error.value.code is TextChangeFailureCode.TARGET_NOT_REGULAR_TEXT

    missing_parent = _proposal(
        AlphaFileChange(
            AlphaTextOperation.CREATE,
            "src/missing/new.txt",
            None,
            "new\n",
        )
    )
    with pytest.raises(TextChangeExecutionError) as parent_error:
        TextChangeExecutor(lifecycle).execute(
            spec,
            missing_parent,
            _admission(spec, missing_parent),
        )
    assert parent_error.value.code is TextChangeFailureCode.TARGET_NOT_REGULAR_TEXT
    assert lifecycle.inspect(spec).changed_paths == ()


def test_executor_rolls_back_caught_partial_failure(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    spec = _spec(repository, max_changed_paths=2)
    lifecycle = GitWorktreeLifecycle()
    worktree = lifecycle.create(spec).worktree_path
    proposal = _proposal(
        AlphaFileChange(AlphaTextOperation.CREATE, "src/a-created.txt", None, "created\n"),
        AlphaFileChange(
            AlphaTextOperation.REPLACE,
            "src/replace.txt",
            bytes_digest(b"old\n"),
            "new\n",
        ),
    )

    with pytest.raises(TextChangeExecutionError) as caught:
        TextChangeExecutor(lifecycle, FailSecondEffect()).execute(
            spec,
            proposal,
            _admission(spec, proposal),
        )

    assert caught.value.code is TextChangeFailureCode.EFFECT_FAILED_ROLLED_BACK
    assert not (worktree / "src" / "a-created.txt").exists()
    assert (worktree / "src" / "replace.txt").read_text() == "old\n"
    assert not tuple(worktree.rglob(".blackcell-*.tmp"))
    assert lifecycle.inspect(spec).changed_paths == ()


def _repository(tmp_path: Path) -> GitRepository:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.name", "BlackCell Test")
    _git(root, "config", "user.email", "blackcell@example.invalid")
    (root / "src").mkdir()
    (root / "src" / "replace.txt").write_text("old\n")
    (root / "src" / "delete.txt").write_text("delete me\n")
    (root / "src" / "link.txt").symlink_to("replace.txt")
    (root / "outside.txt").write_text("outside\n")
    _git(root, "add", "src", "outside.txt")
    _git(root, "commit", "-m", "initial")
    return GitRepository(
        root=root.resolve(),
        isolation_root=(tmp_path / "isolated-worktrees").resolve(),
        base_commit=_git_text(root, "rev-parse", "HEAD"),
    )


def _spec(
    repository: GitRepository,
    *,
    allowed_paths: tuple[str, ...] = ("src",),
    max_changed_paths: int,
) -> WorktreeExecutionSpec:
    return WorktreeExecutionSpec(
        lease=WorktreeLeaseIdentity("run-1", "node-1", 1, 1, "worker-1"),
        repository_root=repository.root,
        isolation_root=repository.isolation_root,
        base_commit=repository.base_commit,
        allowed_paths=allowed_paths,
        max_changed_paths=max_changed_paths,
    )


def _proposal(*operations: AlphaFileChange) -> AlphaChangeProposal:
    return AlphaChangeProposal(
        proposal_id="proposal-1",
        evidence_digest="sha256:" + "1" * 64,
        operations=operations,
        summary="Apply bounded text changes.",
    )


def _admission(
    spec: WorktreeExecutionSpec,
    proposal: AlphaChangeProposal,
) -> TextChangeAdmission:
    return TextChangeAdmission(
        worktree_spec_digest=spec.digest,
        lease_digest=spec.lease.digest,
        evidence_digest=proposal.evidence_digest,
        proposal_digest=proposal.digest,
    )


def _git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
    return subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
    )


def _git_text(cwd: Path, *arguments: str) -> str:
    return _git(cwd, *arguments).stdout.decode("utf-8").strip()
