from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from blackcell.models import (
    ActionProposal,
    CodexExecModel,
    ModelExecutionError,
    ModelTimeoutError,
    ProposalParseError,
)


def _proposal_json(affordance: str = "inspect_file") -> dict[str, Any]:
    return {
        "proposal_id": "proposal-1",
        "context_frame_id": "frame-1",
        "affordance": affordance,
        "arguments": [{"name": "path", "value": "pyproject.toml"}],
        "expected_effects": [],
        "rationale": "Inspect the current file before any mutation.",
        "required_evidence": [],
        "evidence_ids": ["e-1"],
        "assertions": [],
        "schema_version": "action-proposal/v1",
    }


class SuccessfulRunner:
    def __init__(self) -> None:
        self.codex_command: list[str] | None = None
        self.workspace_files: set[str] = set()

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cwd = Path(kwargs["cwd"])
        if command[:2] == ["git", "init"]:
            (cwd / ".git").mkdir()
            return subprocess.CompletedProcess(command, 0, "", "")

        self.codex_command = command
        self.workspace_files = {path.name for path in cwd.iterdir() if path.name != ".git"}
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(_proposal_json()))
        stdout = "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": "t-1"}),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 41, "output_tokens": 9},
                    }
                ),
            )
        )
        return subprocess.CompletedProcess(command, 0, stdout, "warning")


def test_codex_exec_uses_isolated_read_only_structured_invocation() -> None:
    runner = SuccessfulRunner()
    ticks = iter((10.0, 10.025))
    model = CodexExecModel(
        model="gpt-test",
        timeout_seconds=7,
        runner=runner,
        clock=lambda: next(ticks),
    )

    result = model.decide({"objective": "inspect", "private": "never place me in argv"})

    assert isinstance(result.proposal, ActionProposal)
    assert result.proposal.affordance == "inspect_file"
    assert result.proposal.argument("path") == "pyproject.toml"
    assert result.proposal.evidence_ids == ("e-1",)
    assert runner.workspace_files == {model.FRAME_FILE, model.SCHEMA_FILE}
    assert runner.codex_command is not None
    assert runner.codex_command[:2] == ["codex", "exec"]
    assert "--json" in runner.codex_command
    assert "--ephemeral" in runner.codex_command
    assert "--ignore-user-config" in runner.codex_command
    assert runner.codex_command[runner.codex_command.index("--ask-for-approval") + 1] == "never"
    assert runner.codex_command[runner.codex_command.index("--sandbox") + 1] == "read-only"
    assert "--output-schema" in runner.codex_command
    assert all("never place me" not in argument for argument in runner.codex_command)
    assert result.invocation.duration_ms == pytest.approx(25)
    assert result.invocation.configuration["sandbox"] == "read-only"
    assert result.invocation.usage.input_tokens == 41
    assert result.invocation.usage.output_tokens == 9


def test_codex_exec_can_parse_structured_jsonl_without_output_file() -> None:
    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, "", "")
        proposal = _proposal_json("request_clarification")
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"type": "item.completed", "item": {"structured_output": proposal}}),
            "",
        )

    result = CodexExecModel(runner=runner).decide({"objective": "clarify"})

    assert result.proposal.affordance == "request_clarification"


def test_codex_exec_enforces_timeout() -> None:
    calls = 0

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(command, 0, "", "")
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    with pytest.raises(ModelTimeoutError, match="timed out"):
        CodexExecModel(runner=runner, timeout_seconds=1).decide({"objective": "inspect"})


def test_codex_exec_rejects_sandbox_override() -> None:
    with pytest.raises(ValueError, match="may not override"):
        CodexExecModel(extra_args=("--sandbox", "workspace-write"))


@pytest.mark.parametrize(
    ("returncode", "stdout", "error_type"),
    (
        (2, "", ModelExecutionError),
        (0, json.dumps({"type": "turn.completed"}), ProposalParseError),
    ),
)
def test_codex_exec_surfaces_execution_and_parse_failures(
    returncode: int, stdout: str, error_type: type[Exception]
) -> None:
    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, returncode, stdout, "provider failed")

    with pytest.raises(error_type):
        CodexExecModel(runner=runner).decide({"objective": "inspect"})
