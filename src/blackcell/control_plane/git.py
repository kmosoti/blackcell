import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GitState:
    branch: str | None
    head_oid: str | None
    upstream_ref: str | None
    upstream_oid: str | None
    dirty: bool

    @property
    def pushed(self) -> bool:
        return bool(self.upstream_ref and self.head_oid and self.head_oid == self.upstream_oid)


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    command: tuple[str, ...] | None
    passed: bool
    exit_code: int | None = None
    message: str | None = None


CHECK_COMMANDS: dict[str, tuple[str, ...]] = {
    "pytest": ("uv", "run", "pytest"),
    "ruff": ("uv", "run", "ruff", "check", "."),
    "ruff-format": ("uv", "run", "ruff", "format", "--check", "."),
    "ty": ("uv", "run", "ty", "check"),
}


def inspect_git_state(start: Path | None = None) -> GitState:
    cwd = start or Path.cwd()
    branch = _git_output(("git", "branch", "--show-current"), cwd=cwd)
    head_oid = _git_output(("git", "rev-parse", "HEAD"), cwd=cwd)
    upstream_ref = _git_output(
        ("git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"),
        cwd=cwd,
    )
    upstream_oid = _git_output(("git", "rev-parse", "@{u}"), cwd=cwd) if upstream_ref else None
    status = _git_output(("git", "status", "--porcelain", "--untracked-files=all"), cwd=cwd)
    return GitState(
        branch=branch or None,
        head_oid=head_oid or None,
        upstream_ref=upstream_ref or None,
        upstream_oid=upstream_oid or None,
        dirty=bool(status),
    )


def run_required_checks(
    names: tuple[str, ...],
    start: Path | None = None,
) -> tuple[CheckResult, ...]:
    cwd = start or Path.cwd()
    results: list[CheckResult] = []
    for name in names:
        command = CHECK_COMMANDS.get(name)
        if command is None:
            results.append(
                CheckResult(
                    name=name,
                    command=None,
                    passed=False,
                    message=f"unsupported required check: {name}",
                )
            )
            continue

        result = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)
        results.append(
            CheckResult(
                name=name,
                command=command,
                passed=result.returncode == 0,
                exit_code=result.returncode,
                message=None if result.returncode == 0 else _check_message(result),
            )
        )
    return tuple(results)


def _git_output(command: tuple[str, ...], *, cwd: Path) -> str | None:
    result = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _check_message(result: subprocess.CompletedProcess[str]) -> str:
    output = (result.stderr or result.stdout).strip()
    if not output:
        return f"command failed with exit code {result.returncode}"
    return output.splitlines()[-1]
