from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Protocol

from blackcell.control.models import (
    ActionAttempt,
    ActionProposal,
    AffordanceDefinition,
    AttemptStatus,
    ExecutionResult,
    OutcomeObservation,
    PolicyDecision,
    PolicyOutcome,
    output_digest,
)

_SHELL_EXECUTABLES = frozenset(
    {"sh", "bash", "zsh", "fish", "cmd", "cmd.exe", "powershell", "pwsh"}
)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


class ProcessRunner(Protocol):
    def run(self, argv: tuple[str, ...], *, cwd: Path, timeout_seconds: float) -> ProcessResult: ...


class SubprocessRunner:
    def run(self, argv: tuple[str, ...], *, cwd: Path, timeout_seconds: float) -> ProcessResult:
        try:
            result = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return ProcessResult(
                124,
                _to_bytes(exc.stdout),
                _to_bytes(exc.stderr) or b"check timed out",
                timed_out=True,
            )
        except OSError as exc:
            return ProcessResult(127, b"", str(exc).encode())
        return ProcessResult(result.returncode, result.stdout, result.stderr)


class ExecutionRejected(ValueError):
    pass


class BoundedReadOnlyExecutor:
    """Execute three explicit read-only affordances; proposal data never becomes argv."""

    def __init__(
        self,
        repo_root: Path,
        *,
        affordances: tuple[AffordanceDefinition, ...] | None = None,
        check_commands: Mapping[str, tuple[str, ...]] | None = None,
        runner: ProcessRunner | None = None,
        max_output_bytes: int = 65_536,
        max_timeout_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._root = repo_root.resolve()
        definitions = affordances or default_read_only_affordances()
        self._affordances = {definition.name: definition for definition in definitions}
        if len(self._affordances) != len(definitions):
            raise ValueError("affordance names must be unique")
        if any(not definition.read_only or definition.mutates_state for definition in definitions):
            raise ValueError("bounded executor only accepts read-only affordances")
        self._checks = dict(check_commands or {})
        for name, command in self._checks.items():
            _validate_declared_command(name, command)
        if max_output_bytes <= 0 or max_timeout_seconds <= 0:
            raise ValueError("execution bounds must be positive")
        self._runner = runner or SubprocessRunner()
        self._max_output_bytes = max_output_bytes
        self._max_timeout_seconds = max_timeout_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    def execute(
        self, proposal: ActionProposal, decision: PolicyDecision
    ) -> ExecutionResult:
        if decision.proposal_id != proposal.proposal_id:
            raise ExecutionRejected("policy decision does not belong to this proposal")
        if decision.outcome is not PolicyOutcome.ALLOW:
            raise ExecutionRejected(f"policy outcome is {decision.outcome.value}, not allow")
        definition = self._affordances.get(proposal.affordance)
        if definition is None:
            raise ExecutionRejected(f"affordance is not declared: {proposal.affordance}")
        started = self._clock()
        if proposal.affordance == "inspect_file":
            success, raw_output, error, operation_truncated = self._inspect_file(proposal)
        elif proposal.affordance == "git_status":
            success, raw_output, error = self._git_status(proposal, definition)
            operation_truncated = False
        elif proposal.affordance == "run_check":
            success, raw_output, error = self._run_check(proposal, definition)
            operation_truncated = False
        else:
            raise ExecutionRejected(f"no bounded handler for {proposal.affordance}")
        completed = self._clock()
        attempt_id = _id("attempt", proposal.proposal_id, decision.decision_id, started.isoformat())
        bounded, executor_truncated = _truncate(raw_output, self._max_output_bytes)
        attempt = ActionAttempt(
            attempt_id=attempt_id,
            proposal_id=proposal.proposal_id,
            decision_id=decision.decision_id,
            affordance=proposal.affordance,
            status=AttemptStatus.SUCCEEDED if success else AttemptStatus.FAILED,
            started_at=started,
            completed_at=completed,
            error=error,
        )
        outcome = OutcomeObservation(
            outcome_id=_id("outcome", attempt_id, output_digest(bounded), success),
            attempt_id=attempt_id,
            observed_at=completed,
            success=success,
            output=bounded.decode("utf-8", errors="replace"),
            output_digest=output_digest(bounded),
            truncated=operation_truncated or executor_truncated,
        )
        return ExecutionResult(attempt, outcome)

    def _inspect_file(
        self, proposal: ActionProposal
    ) -> tuple[bool, bytes, str | None, bool]:
        _reject_extra_arguments(proposal, {"path", "max_bytes"})
        requested = proposal.argument("path")
        if not isinstance(requested, str) or not requested:
            raise ExecutionRejected("inspect_file requires a non-empty string path")
        requested_max = proposal.argument("max_bytes", self._max_output_bytes)
        if not isinstance(requested_max, int) or isinstance(requested_max, bool):
            raise ExecutionRejected("max_bytes must be an integer")
        if requested_max <= 0 or requested_max > self._max_output_bytes:
            raise ExecutionRejected("max_bytes exceeds the executor bound")
        target = _contained_file(self._root, requested)
        try:
            with target.open("rb") as handle:
                output = handle.read(requested_max + 1)
        except OSError as exc:
            return False, str(exc).encode(), str(exc), False
        return True, output[:requested_max], None, len(output) > requested_max

    def _git_status(
        self, proposal: ActionProposal, definition: AffordanceDefinition
    ) -> tuple[bool, bytes, str | None]:
        _reject_extra_arguments(proposal, set())
        result = self._runner.run(
            ("git", "-c", "color.ui=false", "status", "--porcelain=v1", "--branch"),
            cwd=self._root,
            timeout_seconds=min(definition.timeout_seconds, self._max_timeout_seconds),
        )
        output = result.stdout + (b"\n" + result.stderr if result.stderr else b"")
        error = None if result.returncode == 0 else _error_message(result)
        return result.returncode == 0, output, error

    def _run_check(
        self, proposal: ActionProposal, definition: AffordanceDefinition
    ) -> tuple[bool, bytes, str | None]:
        _reject_extra_arguments(proposal, {"check"})
        check = proposal.argument("check")
        if not isinstance(check, str) or check not in self._checks:
            raise ExecutionRejected("run_check requires a declared check name")
        result = self._runner.run(
            self._checks[check],
            cwd=self._root,
            timeout_seconds=min(definition.timeout_seconds, self._max_timeout_seconds),
        )
        output = result.stdout + (b"\n" + result.stderr if result.stderr else b"")
        error = None if result.returncode == 0 else _error_message(result)
        return result.returncode == 0, output, error


def default_read_only_affordances() -> tuple[AffordanceDefinition, ...]:
    return (
        AffordanceDefinition(
            "inspect_file",
            "Read a bounded file beneath the repository root.",
            read_only=True,
            evidence_action=True,
        ),
        AffordanceDefinition(
            "git_status",
            "Inspect repository status with a fixed git invocation.",
            read_only=True,
            evidence_action=True,
        ),
        AffordanceDefinition(
            "run_check",
            "Run one developer-declared check by name.",
            read_only=True,
            evidence_action=True,
            timeout_seconds=60.0,
        ),
    )


def _contained_file(root: Path, requested: str) -> Path:
    path = Path(requested)
    if path.is_absolute() or ".." in PurePath(path).parts:
        raise ExecutionRejected("path must be relative and cannot contain '..'")
    target = (root / path).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ExecutionRejected("path escapes the repository root") from exc
    if not target.is_file():
        raise ExecutionRejected("path does not identify a regular file")
    return target


def _reject_extra_arguments(proposal: ActionProposal, allowed: set[str]) -> None:
    supplied = {argument.name for argument in proposal.arguments}
    extras = supplied - allowed
    if extras:
        raise ExecutionRejected(f"unsupported arguments: {', '.join(sorted(extras))}")


def _validate_declared_command(name: str, command: tuple[str, ...]) -> None:
    if not name or not command or any(not isinstance(item, str) or not item for item in command):
        raise ValueError("declared checks require a name and non-empty argv")
    if Path(command[0]).name.casefold() in _SHELL_EXECUTABLES:
        raise ValueError("declared check commands cannot invoke a shell")


def _truncate(value: bytes, maximum: int) -> tuple[bytes, bool]:
    if len(value) <= maximum:
        return value, False
    return value[:maximum], True


def _error_message(result: ProcessResult) -> str:
    if result.timed_out:
        return "execution timed out"
    return f"process exited with status {result.returncode}"


def _to_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    return value if isinstance(value, bytes) else value.encode()


def _id(namespace: str, *parts: object) -> str:
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
    return f"{namespace}:{hashlib.sha256(encoded).hexdigest()}"
