from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from blackcell.adapters.bounded_process import (
    BoundedProcessResult,
    BoundedStreamCapture,
)
from blackcell.adapters.execution.bubblewrap import (
    BubblewrapAcceptanceRunner,
    BubblewrapExecutable,
    BubblewrapIsolationPolicy,
)
from blackcell.adapters.execution.worktree import (
    GitWorktreeLifecycle,
    WorktreeExecutionSpec,
    WorktreeLeaseIdentity,
)
from blackcell.kernel._json import bytes_digest
from blackcell.orchestration.alpha_acceptance import (
    AlphaAcceptanceCommand,
    AlphaAcceptanceError,
    AlphaAcceptanceFailureCode,
)

_PYTHON = Path("/usr/bin/python3")
_BWRAP = Path("/usr/bin/bwrap")
_PRLIMIT = Path("/usr/bin/prlimit")
_TRUE = Path("/usr/bin/true")


@dataclass(frozen=True, slots=True)
class GitRepository:
    root: Path
    isolation_root: Path
    base_commit: str


def test_isolated_command_returns_bounded_evidence_without_mutating_worktree(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    spec = _spec(repository)
    lifecycle = GitWorktreeLifecycle()
    worktree = lifecycle.create(spec).worktree_path
    (worktree / "host-result.txt").write_text("host-owned effect\n", encoding="utf-8")
    before = lifecycle.inspect(spec)
    runner = _local_runner(lifecycle)
    if runner is None:
        return
    command = _command(
        "bounded-output",
        "import sys;sys.stdout.buffer.write(b'ok\\x00');sys.stderr.write('warning')",
    )

    try:
        result = runner.run(command, spec)
    except AlphaAcceptanceError as error:
        _assert_platform_failed_closed(error)
        return

    assert result.passed
    assert result.return_code == 0
    assert result.expected_exit_code == 0
    assert result.command_digest == command.digest
    assert result.worktree_spec_digest == spec.digest
    assert result.inspection_before_digest == result.inspection_after_digest
    assert result.stdout.captured == b"ok\x00"
    assert result.stdout.size_bytes == 3
    assert result.stdout.digest == bytes_digest(b"ok\x00")
    assert result.stderr.captured == b"warning"
    assert "warning" not in repr(result)
    assert lifecycle.inspect(spec) == before

    expected_nonzero = runner.run(
        _command("expected-nonzero", "import sys;sys.exit(7)", expected_exit_code=7),
        spec,
    )
    assert expected_nonzero.passed
    assert expected_nonzero.return_code == 7


def test_isolation_hides_host_state_blocks_network_and_keeps_worktree_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_secret = tmp_path / "host-secret.txt"
    host_secret.write_text("must-not-be-readable\n", encoding="utf-8")
    repository = _repository(tmp_path, secret_target=host_secret)
    spec = _spec(repository)
    lifecycle = GitWorktreeLifecycle()
    lifecycle.create(spec)
    runner = _local_runner(lifecycle)
    if runner is None:
        return
    monkeypatch.setenv("BLACKCELL_HOST_SECRET", "ambient-secret")
    script = """
import json
import os
import pathlib
import socket

workspace = pathlib.Path('/workspace')
try:
    (workspace / 'README.md').write_text('sandbox mutation', encoding='utf-8')
except OSError:
    write_blocked = True
else:
    write_blocked = False

network = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
network.settimeout(0.2)
network_error = network.connect_ex(('1.1.1.1', 53))
network.close()
try:
    (workspace / '.git').read_bytes()
except OSError:
    git_metadata_absent = True
else:
    git_metadata_absent = False
print(json.dumps({
    'cwd': str(pathlib.Path.cwd()),
    'environment': dict(os.environ),
    'git_metadata_absent': git_metadata_absent,
    'host_etc_absent': not pathlib.Path('/etc/passwd').exists(),
    'host_home_absent': not pathlib.Path('/home').exists(),
    'network_error': network_error,
    'secret_link_absent': not (workspace / 'host-secret-link').exists(),
    'write_blocked': write_blocked,
}, sort_keys=True))
"""

    try:
        result = runner.run(_command("isolation-observation", script), spec)
    except AlphaAcceptanceError as error:
        _assert_platform_failed_closed(error)
        return

    observation = json.loads(result.stdout.captured)
    assert observation == {
        "cwd": "/workspace",
        "environment": {
            "HOME": "/tmp/blackcell-home",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
            "PWD": "/workspace",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": "/tmp",
        },
        "git_metadata_absent": True,
        "host_etc_absent": True,
        "host_home_absent": True,
        "network_error": 101,
        "secret_link_absent": True,
        "write_blocked": True,
    }


def test_isolation_rejects_invalid_authority_and_reports_typed_process_failures(
    tmp_path: Path,
) -> None:
    with pytest.raises(AlphaAcceptanceError) as invalid_executable:
        BubblewrapExecutable("python", tmp_path / "missing-runtime")
    assert invalid_executable.value.code is AlphaAcceptanceFailureCode.INVALID_POLICY

    repository = _repository(tmp_path)
    spec = _spec(repository)
    lifecycle = GitWorktreeLifecycle()
    lifecycle.create(spec)
    policy = _policy()
    if policy is None:
        return

    no_status = NoStatusTransport()
    unavailable = BubblewrapAcceptanceRunner(policy, lifecycle, no_status)
    with pytest.raises(AlphaAcceptanceError) as missing_status:
        unavailable.run(_command("missing-status", "pass"), spec)
    assert missing_status.value.code is AlphaAcceptanceFailureCode.ISOLATION_UNAVAILABLE
    assert no_status.calls == 1

    runner = BubblewrapAcceptanceRunner(policy, lifecycle)
    with pytest.raises(AlphaAcceptanceError) as disallowed:
        runner.run(
            AlphaAcceptanceCommand(
                check_id="disallowed",
                argv=("not-allowed", "--version"),
                expected_exit_code=0,
                timeout_seconds=1.0,
                stdout_limit_bytes=8,
                stderr_limit_bytes=8,
            ),
            spec,
        )
    assert disallowed.value.code is AlphaAcceptanceFailureCode.EXECUTABLE_NOT_ALLOWED

    try:
        runner.run(
            _command("timeout", "import time;time.sleep(30)", timeout_seconds=0.05),
            spec,
        )
    except AlphaAcceptanceError as error:
        if error.code is AlphaAcceptanceFailureCode.ISOLATION_UNAVAILABLE:
            return
        assert error.code is AlphaAcceptanceFailureCode.TIMED_OUT
    else:
        pytest.fail("an over-deadline command must fail")

    with pytest.raises(AlphaAcceptanceError) as oversized:
        runner.run(
            _command("oversized", "print('123456789',end='')", stdout_limit_bytes=8),
            spec,
        )
    assert oversized.value.code is AlphaAcceptanceFailureCode.OUTPUT_TOO_LARGE

    changed = MutatingStatusTransport(spec.worktree_path / "README.md")
    mutation_runner = BubblewrapAcceptanceRunner(policy, lifecycle, changed)
    with pytest.raises(AlphaAcceptanceError) as worktree_changed:
        mutation_runner.run(_command("changed", "pass"), spec)
    assert worktree_changed.value.code is AlphaAcceptanceFailureCode.WORKTREE_CHANGED
    assert changed.calls == 2


def test_isolated_cancellation_terminates_namespaced_descendants(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    spec = _spec(repository)
    lifecycle = GitWorktreeLifecycle()
    lifecycle.create(spec)
    runner = _local_runner(lifecycle)
    if runner is None:
        return
    marker = f"blackcell-isolated-child-{uuid.uuid4().hex}"
    script = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)',sys.argv[1]]);"
        "time.sleep(30)"
    )
    started = time.monotonic()

    def cancel_when_started() -> bool:
        return time.monotonic() - started >= 0.5

    try:
        runner.run(
            AlphaAcceptanceCommand(
                check_id="cancel-tree",
                argv=("python", "-c", script, marker),
                expected_exit_code=0,
                timeout_seconds=5.0,
                stdout_limit_bytes=1024,
                stderr_limit_bytes=1024,
            ),
            spec,
            cancel_requested=cancel_when_started,
        )
    except AlphaAcceptanceError as error:
        if error.code is AlphaAcceptanceFailureCode.ISOLATION_UNAVAILABLE:
            return
        assert error.code is AlphaAcceptanceFailureCode.CANCELED
    else:
        pytest.fail("a cancellation request must stop the isolated command")
    deadline = time.monotonic() + 1.0
    while _process_with_marker(marker) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _process_with_marker(marker)


class NoStatusTransport:
    def __init__(self) -> None:
        self.calls = 0

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
        self.calls += 1
        return _empty_process_result()


class MutatingStatusTransport(NoStatusTransport):
    def __init__(self, mutation_path: Path) -> None:
        super().__init__()
        self._mutation_path = mutation_path

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
        self.calls += 1
        if self.calls == 2:
            self._mutation_path.write_text("changed outside sandbox\n", encoding="utf-8")
        os.write(pass_fds[0], _valid_status())
        return _empty_process_result()


def _empty_process_result() -> BoundedProcessResult:
    empty = BoundedStreamCapture(b"", 0, True)
    return BoundedProcessResult(0, empty, empty)


def _valid_status() -> bytes:
    child = {"child-pid": 12345}
    for status_key, namespace in (
        ("cgroup-namespace", "cgroup"),
        ("ipc-namespace", "ipc"),
        ("mnt-namespace", "mnt"),
        ("net-namespace", "net"),
        ("pid-namespace", "pid"),
        ("uts-namespace", "uts"),
    ):
        child[status_key] = os.stat(f"/proc/self/ns/{namespace}").st_ino + 1
    return (json.dumps(child) + "\n" + json.dumps({"exit-code": 0}) + "\n").encode()


def _local_runner(lifecycle: GitWorktreeLifecycle) -> BubblewrapAcceptanceRunner | None:
    policy = _policy()
    if policy is None:
        return None
    return BubblewrapAcceptanceRunner(policy, lifecycle)


def _policy() -> BubblewrapIsolationPolicy | None:
    if sys.platform != "linux" or not all(
        path.is_file() for path in (_PYTHON, _BWRAP, _PRLIMIT, _TRUE)
    ):
        return None
    return BubblewrapIsolationPolicy((BubblewrapExecutable("python", _PYTHON),))


def _command(
    check_id: str,
    script: str,
    *,
    expected_exit_code: int = 0,
    timeout_seconds: float = 2.0,
    stdout_limit_bytes: int = 64 * 1024,
) -> AlphaAcceptanceCommand:
    return AlphaAcceptanceCommand(
        check_id=check_id,
        argv=("python", "-c", script),
        expected_exit_code=expected_exit_code,
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=64 * 1024,
    )


def _repository(tmp_path: Path, *, secret_target: Path | None = None) -> GitRepository:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.name", "BlackCell Test")
    _git(root, "config", "user.email", "blackcell@example.invalid")
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    if secret_target is not None:
        (root / "host-secret-link").symlink_to(secret_target)
    _git(root, "add", "README.md")
    if secret_target is not None:
        _git(root, "add", "host-secret-link")
    _git(root, "commit", "-m", "initial")
    return GitRepository(
        root=root.resolve(),
        isolation_root=(tmp_path / "isolated-worktrees").resolve(),
        base_commit=_git_text(root, "rev-parse", "HEAD"),
    )


def _spec(repository: GitRepository) -> WorktreeExecutionSpec:
    return WorktreeExecutionSpec(
        lease=WorktreeLeaseIdentity("run-1", "node-1", 1, 1, "worker-1"),
        repository_root=repository.root,
        isolation_root=repository.isolation_root,
        base_commit=repository.base_commit,
        allowed_paths=(".",),
        max_changed_paths=10,
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
    return _git(cwd, *arguments).stdout.decode().strip()


def _assert_platform_failed_closed(error: AlphaAcceptanceError) -> None:
    assert error.code is AlphaAcceptanceFailureCode.ISOLATION_UNAVAILABLE


def _process_with_marker(marker: str) -> bool:
    result = subprocess.run(
        ("pgrep", "-f", marker),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0
