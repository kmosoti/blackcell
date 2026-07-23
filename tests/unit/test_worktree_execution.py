from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from blackcell.adapters.execution.worktree import (
    GitWorktreeLifecycle,
    WorktreeCommitEffect,
    WorktreeExecutionSpec,
    WorktreeFailureCode,
    WorktreeLeaseIdentity,
    WorktreeLifecycleError,
)
from blackcell.kernel._json import bytes_digest


@dataclass(frozen=True, slots=True)
class GitRepository:
    root: Path
    isolation_root: Path
    base_commit: str


def test_spec_rejects_invalid_authority_before_git(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    lease = _lease()

    cases: tuple[tuple[Callable[[], object], WorktreeFailureCode], ...] = (
        (
            lambda: WorktreeExecutionSpec(
                lease=lease,
                repository_root=Path("relative-repository"),
                isolation_root=repository.isolation_root,
                base_commit=repository.base_commit,
                allowed_paths=("src",),
                max_changed_paths=1,
            ),
            WorktreeFailureCode.INVALID_REPOSITORY,
        ),
        (
            lambda: WorktreeExecutionSpec(
                lease=lease,
                repository_root=repository.root,
                isolation_root=repository.root / "nested-isolation",
                base_commit=repository.base_commit,
                allowed_paths=("src",),
                max_changed_paths=1,
            ),
            WorktreeFailureCode.INVALID_ISOLATION_ROOT,
        ),
        (
            lambda: WorktreeExecutionSpec(
                lease=lease,
                repository_root=repository.root,
                isolation_root=repository.isolation_root,
                base_commit="f" * 39,
                allowed_paths=("src",),
                max_changed_paths=1,
            ),
            WorktreeFailureCode.INVALID_SPEC,
        ),
        (
            lambda: WorktreeExecutionSpec(
                lease=lease,
                repository_root=repository.root,
                isolation_root=repository.isolation_root,
                base_commit=repository.base_commit,
                allowed_paths=("src/.git/config",),
                max_changed_paths=1,
            ),
            WorktreeFailureCode.INVALID_SPEC,
        ),
        (
            lambda: WorktreeExecutionSpec(
                lease=lease,
                repository_root=repository.root,
                isolation_root=repository.isolation_root,
                base_commit=repository.base_commit,
                allowed_paths=("../escape",),
                max_changed_paths=1,
            ),
            WorktreeFailureCode.INVALID_SPEC,
        ),
        (
            lambda: WorktreeExecutionSpec(
                lease=lease,
                repository_root=repository.root,
                isolation_root=repository.isolation_root,
                base_commit=repository.base_commit,
                allowed_paths=(".git",),
                max_changed_paths=1,
            ),
            WorktreeFailureCode.INVALID_SPEC,
        ),
        (
            lambda: WorktreeExecutionSpec(
                lease=lease,
                repository_root=repository.root,
                isolation_root=repository.isolation_root,
                base_commit=repository.base_commit,
                allowed_paths=("src",),
                max_changed_paths=-1,
            ),
            WorktreeFailureCode.INVALID_SPEC,
        ),
    )
    for build, expected in cases:
        with pytest.raises(WorktreeLifecycleError) as caught:
            build()
        assert caught.value.code is expected
        assert str(caught.value) == expected.value

    with pytest.raises(WorktreeLifecycleError) as invalid_lease:
        WorktreeLeaseIdentity("run-1", "node-1", 0, 1, "worker-1")
    assert invalid_lease.value.code is WorktreeFailureCode.INVALID_SPEC

    unknown_base = WorktreeExecutionSpec(
        lease=lease,
        repository_root=repository.root,
        isolation_root=repository.isolation_root,
        base_commit="f" * 40,
        allowed_paths=("src",),
        max_changed_paths=1,
    )
    with pytest.raises(WorktreeLifecycleError) as missing_commit:
        GitWorktreeLifecycle().create(unknown_base)
    assert missing_commit.value.code is WorktreeFailureCode.BASE_COMMIT_NOT_FOUND
    assert not repository.isolation_root.exists()
    assert _git_text(repository.root, "branch", "--list", "blackcell/*") == ""


def test_validate_base_commit_uses_the_bounded_repository_contract(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    lifecycle = GitWorktreeLifecycle()

    lifecycle.validate_base_commit(repository.root, repository.base_commit)

    with pytest.raises(WorktreeLifecycleError) as missing:
        lifecycle.validate_base_commit(repository.root, "f" * 40)
    assert missing.value.code is WorktreeFailureCode.BASE_COMMIT_NOT_FOUND


def test_create_is_deterministic_and_idempotent(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    spec = _spec(repository)
    lifecycle = GitWorktreeLifecycle()

    created = lifecycle.create(spec)
    retried = lifecycle.create(spec)

    assert retried == created
    assert created.spec_digest == spec.digest
    assert created.lease_digest == spec.lease.digest
    assert created.worktree_path == spec.worktree_path
    assert created.branch_name == spec.branch_name
    assert created.base_commit == repository.base_commit
    assert created.head_commit == repository.base_commit
    assert created.changed_paths == ()
    assert created.clean
    assert created.path_policy_compliant
    assert spec.worktree_path.parent == repository.isolation_root
    assert stat.S_IMODE(repository.isolation_root.stat().st_mode) == 0o700
    worktrees = _git_text(repository.root, "worktree", "list", "--porcelain")
    assert worktrees.count("worktree ") == 2
    assert worktrees.count(str(spec.worktree_path)) == 1


def test_create_recovers_checkout_from_surviving_branch(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    spec = _spec(repository)
    lifecycle = GitWorktreeLifecycle()
    created = lifecycle.create(spec)

    _git(repository.root, "worktree", "remove", str(spec.worktree_path))
    assert not spec.worktree_path.exists()
    assert _git_text(repository.root, "rev-parse", spec.branch_name) == created.head_commit

    recovered = lifecycle.create(spec)

    assert recovered == created
    assert spec.worktree_path.is_dir()


def test_inspect_reports_changed_paths_and_budget_violations(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    spec = _spec(repository, allowed_paths=("src",), max_changed_paths=1)
    lifecycle = GitWorktreeLifecycle()
    worktree = lifecycle.create(spec).worktree_path
    (worktree / "src" / "new.py").write_text("VALUE = 1\n")
    (worktree / "outside.txt").write_text("outside\n")
    (worktree / "ignored").mkdir()
    (worktree / "ignored" / "private.txt").write_text("ignored but observable\n")

    inspection = lifecycle.inspect(spec)

    assert inspection.changed_paths == (
        "ignored/private.txt",
        "outside.txt",
        "src/new.py",
    )
    assert inspection.uncommitted_paths == inspection.changed_paths
    assert inspection.out_of_scope_paths == ("ignored/private.txt", "outside.txt")
    assert inspection.changed_path_limit_exceeded
    assert not inspection.clean
    assert not inspection.path_policy_compliant


def test_retain_preserves_failed_dirty_worktree(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    spec = _spec(repository, allowed_paths=(".",))
    lifecycle = GitWorktreeLifecycle()
    worktree = lifecycle.create(spec).worktree_path
    (worktree / "failure-evidence.txt").write_text("partial result\n")

    retained = lifecycle.retain(spec)

    assert retained.uncommitted_paths == ("failure-evidence.txt",)
    assert retained.path_policy_compliant
    assert worktree.is_dir()
    assert _git_text(repository.root, "rev-parse", spec.branch_name) == repository.base_commit


def test_remove_success_refuses_dirty_worktree(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    spec = _spec(repository)
    lifecycle = GitWorktreeLifecycle()
    worktree = lifecycle.create(spec).worktree_path
    (worktree / "src" / "unfinished.py").write_text("VALUE = 1\n")

    with pytest.raises(WorktreeLifecycleError) as caught:
        lifecycle.remove_success(spec)

    assert caught.value.code is WorktreeFailureCode.WORKTREE_DIRTY
    assert str(worktree) not in str(caught.value)
    assert worktree.is_dir()
    assert _git_text(repository.root, "rev-parse", spec.branch_name) == repository.base_commit


def test_commit_changes_creates_clean_policy_bound_head(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    spec = _spec(repository, allowed_paths=("src",), max_changed_paths=3)
    lifecycle = GitWorktreeLifecycle()
    worktree = lifecycle.create(spec).worktree_path
    value = b"VALUE = 2\n"
    new = b"NEW = True\n"
    (worktree / "src" / ".keep").unlink()
    (worktree / "src" / "value.py").write_bytes(value)
    (worktree / "src" / "new.py").write_bytes(new)

    committed = lifecycle.commit_changes(
        spec,
        effects=(
            WorktreeCommitEffect("src/.keep", None),
            WorktreeCommitEffect("src/new.py", bytes_digest(new)),
            WorktreeCommitEffect("src/value.py", bytes_digest(value)),
        ),
    )

    assert committed.clean
    assert committed.path_policy_compliant
    assert committed.head_commit != repository.base_commit
    assert committed.changed_paths == ("src/.keep", "src/new.py", "src/value.py")
    assert committed.uncommitted_paths == ()
    assert _git_text(worktree, "show", "-s", "--format=%an <%ae>", "HEAD") == (
        "BlackCell <blackcell@example.invalid>"
    )
    assert _git_text(worktree, "show", "-s", "--format=%s", "HEAD") == (
        "BlackCell alpha run-1/node-1 attempt 1"
    )
    assert lifecycle.commit_changes(spec) == committed


def test_commit_changes_requires_exact_effect_evidence_before_staging(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    spec = _spec(repository)
    lifecycle = GitWorktreeLifecycle()
    worktree = lifecycle.create(spec).worktree_path
    (worktree / "src" / "value.py").write_bytes(b"VALUE = 2\n")

    with pytest.raises(WorktreeLifecycleError) as missing:
        lifecycle.commit_changes(spec)
    with pytest.raises(WorktreeLifecycleError) as wrong_digest:
        lifecycle.commit_changes(
            spec,
            effects=(WorktreeCommitEffect("src/value.py", bytes_digest(b"VALUE = 3\n")),),
        )

    assert missing.value.code is WorktreeFailureCode.COMMIT_FAILED
    assert wrong_digest.value.code is WorktreeFailureCode.COMMIT_FAILED
    assert _git_text(worktree, "diff", "--cached", "--name-only") == ""
    assert _git_text(worktree, "rev-parse", "HEAD") == repository.base_commit


def test_commit_changes_force_stages_an_exact_ignored_effect(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    spec = _spec(repository, allowed_paths=("ignored",))
    lifecycle = GitWorktreeLifecycle()
    worktree = lifecycle.create(spec).worktree_path
    target = worktree / "ignored" / "result.txt"
    target.parent.mkdir()
    content = b"admitted ignored output\n"
    target.write_bytes(content)

    committed = lifecycle.commit_changes(
        spec,
        effects=(WorktreeCommitEffect("ignored/result.txt", bytes_digest(content)),),
    )

    assert committed.clean
    assert committed.changed_paths == ("ignored/result.txt",)
    assert _git(worktree, "show", "HEAD:ignored/result.txt").stdout == content


def test_commit_changes_rejects_git_attribute_blob_transformation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository.root / ".gitattributes").write_bytes(b"src/*.txt text eol=lf\n")
    _git(repository.root, "add", ".gitattributes")
    _git(repository.root, "commit", "-m", "add line-ending policy")
    repository = GitRepository(
        root=repository.root,
        isolation_root=repository.isolation_root,
        base_commit=_git_text(repository.root, "rev-parse", "HEAD"),
    )
    spec = _spec(repository)
    lifecycle = GitWorktreeLifecycle()
    worktree = lifecycle.create(spec).worktree_path
    content = b"first\r\nsecond\r\n"
    (worktree / "src" / "value.txt").write_bytes(content)

    with pytest.raises(WorktreeLifecycleError) as caught:
        lifecycle.commit_changes(
            spec,
            effects=(WorktreeCommitEffect("src/value.txt", bytes_digest(content)),),
        )

    assert caught.value.code is WorktreeFailureCode.COMMIT_FAILED
    assert _git_text(worktree, "rev-parse", "HEAD") == repository.base_commit
    assert _git(worktree, "show", ":src/value.txt").stdout == b"first\nsecond\n"


def test_remove_success_refuses_committed_path_policy_violation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    spec = _spec(repository, allowed_paths=("src",))
    lifecycle = GitWorktreeLifecycle()
    worktree = lifecycle.create(spec).worktree_path
    (worktree / "outside.txt").write_text("committed outside authority\n")
    _git(worktree, "add", "outside.txt")
    _git(worktree, "commit", "-m", "outside change")

    inspection = lifecycle.inspect(spec)
    assert inspection.clean
    assert inspection.out_of_scope_paths == ("outside.txt",)
    with pytest.raises(WorktreeLifecycleError) as caught:
        lifecycle.remove_success(spec)

    assert caught.value.code is WorktreeFailureCode.PATH_POLICY_VIOLATION
    assert worktree.is_dir()


def test_remove_success_removes_clean_checkout_and_keeps_branch(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    spec = _spec(repository)
    lifecycle = GitWorktreeLifecycle()
    worktree = lifecycle.create(spec).worktree_path
    (worktree / "src" / "completed.py").write_text("VALUE = 1\n")
    _git(worktree, "add", "src/completed.py")
    _git(worktree, "commit", "-m", "completed change")
    inspection = lifecycle.inspect(spec)
    assert inspection.clean
    assert inspection.changed_paths == ("src/completed.py",)
    assert inspection.head_commit != repository.base_commit

    removed = lifecycle.remove_success(spec)

    assert removed.disposition == "removed"
    assert removed.retained_head_commit == inspection.head_commit
    assert not worktree.exists()
    assert _git_text(repository.root, "rev-parse", spec.branch_name) == inspection.head_commit
    assert str(worktree) not in _git_text(repository.root, "worktree", "list", "--porcelain")


def test_remove_success_is_idempotent_after_effect_only_crash(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    spec = _spec(repository)
    lifecycle = GitWorktreeLifecycle()
    worktree = lifecycle.create(spec).worktree_path
    (worktree / "src" / "completed.py").write_text("VALUE = 1\n")
    _git(worktree, "add", "src/completed.py")
    _git(worktree, "commit", "-m", "completed change")
    expected_head = lifecycle.inspect(spec).head_commit

    removed = lifecycle.remove_success(spec, expected_head_commit=expected_head)
    recovered = lifecycle.remove_success(spec, expected_head_commit=expected_head)

    assert recovered == removed
    assert not worktree.exists()
    assert _git_text(repository.root, "rev-parse", spec.branch_name) == expected_head
    with pytest.raises(WorktreeLifecycleError) as wrong_head:
        lifecycle.remove_success(spec, expected_head_commit=repository.base_commit)
    assert wrong_head.value.code is WorktreeFailureCode.CLEANUP_FAILED


def test_fencing_identities_are_physically_disjoint(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    first = _spec(repository, fencing_token=1)
    second = _spec(repository, fencing_token=2)
    lifecycle = GitWorktreeLifecycle()

    first_snapshot = lifecycle.create(first)
    second_snapshot = lifecycle.create(second)

    assert first.digest != second.digest
    assert first.lease.digest != second.lease.digest
    assert first_snapshot.worktree_path != second_snapshot.worktree_path
    assert first_snapshot.branch_name != second_snapshot.branch_name
    assert first_snapshot.worktree_path.is_dir()
    assert second_snapshot.worktree_path.is_dir()

    lifecycle.remove_success(first)

    assert not first_snapshot.worktree_path.exists()
    assert second_snapshot.worktree_path.is_dir()
    assert lifecycle.inspect(second) == second_snapshot


def test_external_checkout_filters_are_refused_before_creation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    spec = _spec(repository)
    _git(repository.root, "config", "filter.untrusted.smudge", "external-command")

    with pytest.raises(WorktreeLifecycleError) as caught:
        GitWorktreeLifecycle().create(spec)

    assert caught.value.code is WorktreeFailureCode.UNSAFE_REPOSITORY_CONFIGURATION
    assert not repository.isolation_root.exists()
    assert _git_text(repository.root, "branch", "--list", "blackcell/*") == ""


def _repository(tmp_path: Path) -> GitRepository:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.name", "BlackCell Test")
    _git(root, "config", "user.email", "blackcell@example.invalid")
    (root / ".gitignore").write_text("ignored/\n")
    (root / "README.md").write_text("fixture\n")
    (root / "src").mkdir()
    (root / "src" / ".keep").write_text("")
    _git(root, "add", ".gitignore", "README.md", "src/.keep")
    _git(root, "commit", "-m", "initial")
    return GitRepository(
        root=root.resolve(),
        isolation_root=(tmp_path / "isolated-worktrees").resolve(),
        base_commit=_git_text(root, "rev-parse", "HEAD"),
    )


def _lease(*, fencing_token: int = 1) -> WorktreeLeaseIdentity:
    return WorktreeLeaseIdentity(
        run_id="run-1",
        node_id="node-1",
        attempt=1,
        fencing_token=fencing_token,
        worker_id="worker-1",
    )


def _spec(
    repository: GitRepository,
    *,
    fencing_token: int = 1,
    allowed_paths: tuple[str, ...] = ("src",),
    max_changed_paths: int = 10,
) -> WorktreeExecutionSpec:
    return WorktreeExecutionSpec(
        lease=_lease(fencing_token=fencing_token),
        repository_root=repository.root,
        isolation_root=repository.isolation_root,
        base_commit=repository.base_commit,
        allowed_paths=allowed_paths,
        max_changed_paths=max_changed_paths,
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
