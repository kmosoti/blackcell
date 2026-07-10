from datetime import UTC, datetime
from pathlib import Path

import pytest

from blackcell.control import (
    ActionArgument,
    ActionProposal,
    BoundedReadOnlyExecutor,
    ExecutionRejected,
    PolicyDecision,
    PolicyFinding,
    PolicyOutcome,
    ProcessResult,
)

NOW = datetime(2026, 5, 1, tzinfo=UTC)


class _Runner:
    def __init__(self, output: bytes = b"ok\n") -> None:
        self.calls: list[tuple[str, ...]] = []
        self.output = output

    def run(self, argv: tuple[str, ...], *, cwd: Path, timeout_seconds: float) -> ProcessResult:
        self.calls.append(argv)
        return ProcessResult(0, self.output, b"")


def _proposal(affordance: str, *arguments: ActionArgument) -> ActionProposal:
    return ActionProposal(
        f"proposal:{affordance}",
        "context:1",
        affordance,
        tuple(arguments),
        (),
        "collect bounded evidence",
    )


def _allow(proposal: ActionProposal) -> PolicyDecision:
    return PolicyDecision(
        proposal.proposal_id,
        PolicyOutcome.ALLOW,
        (PolicyFinding("test", PolicyOutcome.ALLOW, "allowed", "test approval"),),
        NOW,
    )


def test_inspect_file_rejects_traversal_and_bounds_output(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("0123456789", encoding="utf-8")
    executor = BoundedReadOnlyExecutor(tmp_path, clock=lambda: NOW)
    traversal = _proposal("inspect_file", ActionArgument("path", "../secret"))

    with pytest.raises(ExecutionRejected, match="relative"):
        executor.execute(traversal, _allow(traversal))

    bounded = _proposal(
        "inspect_file", ActionArgument("path", "data.txt"), ActionArgument("max_bytes", 4)
    )
    result = executor.execute(bounded, _allow(bounded))

    assert result.outcome.output == "0123"
    assert result.outcome.truncated is True
    assert result.outcome.success is True


@pytest.mark.parametrize(
    "relative_path",
    (".env", ".env.local", ".git/config", ".blackcell/kernel.sqlite3", "client.pem"),
)
def test_inspect_file_rejects_sensitive_paths(tmp_path: Path, relative_path: str) -> None:
    target = tmp_path / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret", encoding="utf-8")
    executor = BoundedReadOnlyExecutor(tmp_path)
    proposal = _proposal("inspect_file", ActionArgument("path", relative_path))

    with pytest.raises(ExecutionRejected, match=r"protected|credential"):
        executor.execute(proposal, _allow(proposal))


def test_run_check_uses_only_declared_argv_and_rejects_injected_command(tmp_path: Path) -> None:
    runner = _Runner()
    executor = BoundedReadOnlyExecutor(
        tmp_path,
        check_commands={"unit": ("python", "-m", "pytest", "-q")},
        runner=runner,
        clock=lambda: NOW,
    )
    proposal = _proposal("run_check", ActionArgument("check", "unit"))

    result = executor.execute(proposal, _allow(proposal))

    assert result.outcome.success is True
    assert runner.calls == [("python", "-m", "pytest", "-q")]

    injection = _proposal(
        "run_check",
        ActionArgument("check", "unit"),
        ActionArgument("command", "rm -rf /"),
    )
    with pytest.raises(ExecutionRejected, match="unsupported arguments"):
        executor.execute(injection, _allow(injection))


def test_executor_rejects_non_allow_decision_and_shell_check_whitelist(tmp_path: Path) -> None:
    proposal = _proposal("git_status")
    denied = PolicyDecision(
        proposal.proposal_id,
        PolicyOutcome.DENY,
        (PolicyFinding("test", PolicyOutcome.DENY, "denied", "no"),),
        NOW,
    )
    executor = BoundedReadOnlyExecutor(tmp_path, runner=_Runner(), clock=lambda: NOW)

    with pytest.raises(ExecutionRejected, match="not allow"):
        executor.execute(proposal, denied)
    with pytest.raises(ValueError, match="cannot invoke a shell"):
        BoundedReadOnlyExecutor(tmp_path, check_commands={"bad": ("sh", "-c", "echo bad")})


def test_executor_caps_command_output(tmp_path: Path) -> None:
    runner = _Runner(b"x" * 100)
    executor = BoundedReadOnlyExecutor(
        tmp_path, runner=runner, max_output_bytes=16, clock=lambda: NOW
    )
    proposal = _proposal("git_status")

    result = executor.execute(proposal, _allow(proposal))

    assert len(result.outcome.output) == 16
    assert result.outcome.truncated is True
