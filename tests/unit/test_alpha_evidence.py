from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from blackcell.adapters.execution.evidence import (
    AlphaEvidenceCollector,
    AlphaEvidenceError,
    AlphaEvidenceFailureCode,
)
from blackcell.adapters.execution.worktree import (
    GitWorktreeLifecycle,
    WorktreeExecutionSpec,
    WorktreeLeaseIdentity,
)


@dataclass(frozen=True, slots=True)
class GitRepository:
    root: Path
    isolation_root: Path
    base_commit: str


def test_collector_returns_only_bounded_regular_utf8_files(tmp_path: Path) -> None:
    repository = _repository(
        tmp_path / "regular",
        {
            ".gitignore": "ignored/\n",
            "README.md": "fixture\n",
            "src/a.py": "A = 1\n",
            "src/nested/b.txt": "bounded\n",
        },
    )
    spec = _spec(repository, allowed_paths=(".", "src/a.py"))
    GitWorktreeLifecycle().create(spec)

    context = AlphaEvidenceCollector().collect(
        spec,
        objective="Update bounded source text.",
        constraints=("Do not change README.md.",),
    )

    assert tuple(item.path for item in context.files) == (
        ".gitignore",
        "README.md",
        "src/a.py",
        "src/nested/b.txt",
    )
    assert context.files[2].content == "A = 1\n"
    assert all(".git" not in Path(item.path).parts for item in context.files)
    assert context.base_commit == repository.base_commit
    assert context.allowed_paths == (".", "src/a.py")


def test_collector_rejects_symlinks_binary_and_limit_overflow(tmp_path: Path) -> None:
    symlink_repository = _repository(
        tmp_path / "symlink",
        {"src/value.txt": "value\n"},
        symlinks={"src/link.txt": "value.txt"},
    )
    binary_repository = _repository(
        tmp_path / "binary",
        {"src/data.bin": b"\xff\xfe"},
    )
    large_repository = _repository(
        tmp_path / "large",
        {"src/large.txt": b"x" * (256 * 1024 + 1)},
    )
    total_repository = _repository(
        tmp_path / "total",
        {f"src/{index}.txt": b"x" * (220 * 1024) for index in range(5)},
    )
    many_repository = _repository(
        tmp_path / "many",
        {f"src/{index:02d}.txt": "x\n" for index in range(65)},
    )
    cases = (
        (symlink_repository, AlphaEvidenceFailureCode.UNSAFE_ENTRY),
        (binary_repository, AlphaEvidenceFailureCode.INVALID_TEXT),
        (large_repository, AlphaEvidenceFailureCode.FILE_TOO_LARGE),
        (total_repository, AlphaEvidenceFailureCode.TOTAL_TOO_LARGE),
        (many_repository, AlphaEvidenceFailureCode.TOO_MANY_FILES),
    )
    collector = AlphaEvidenceCollector()
    lifecycle = GitWorktreeLifecycle()
    for repository, expected in cases:
        spec = _spec(repository, allowed_paths=("src",))
        lifecycle.create(spec)
        with pytest.raises(AlphaEvidenceError) as caught:
            collector.collect(spec, objective="Inspect bounded files.", constraints=())
        assert caught.value.code is expected
        assert "src" not in str(caught.value)


def _repository(
    root: Path,
    files: dict[str, str | bytes],
    *,
    symlinks: dict[str, str] | None = None,
) -> GitRepository:
    root.mkdir(parents=True)
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.name", "BlackCell Test")
    _git(root, "config", "user.email", "blackcell@example.invalid")
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)
    for relative, target in (symlinks or {}).items():
        link = root / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "initial")
    return GitRepository(
        root=root.resolve(),
        isolation_root=(root.parent / f"{root.name}-worktrees").resolve(),
        base_commit=_git_text(root, "rev-parse", "HEAD"),
    )


def _spec(
    repository: GitRepository,
    *,
    allowed_paths: tuple[str, ...],
) -> WorktreeExecutionSpec:
    return WorktreeExecutionSpec(
        lease=WorktreeLeaseIdentity("run-1", "node-1", 1, 1, "worker-1"),
        repository_root=repository.root,
        isolation_root=repository.isolation_root,
        base_commit=repository.base_commit,
        allowed_paths=allowed_paths,
        max_changed_paths=100,
    )


def _git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
    )


def _git_text(cwd: Path, *arguments: str) -> str:
    return _git(cwd, *arguments).stdout.decode("utf-8").strip()
