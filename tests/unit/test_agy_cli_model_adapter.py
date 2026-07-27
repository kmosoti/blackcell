from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from blackcell.adapters.models import (
    AGY_CLI_ADAPTER_ID,
    AGY_CLI_REQUIRED_VERSION,
    AgyCliAdapterError,
    AgyCliModelAdapter,
    AgyCliOutputError,
    AgyCliTimeoutError,
)
from blackcell.adapters.models.agy_cli import (
    AGY_CLI_PROVIDER_SCAFFOLD_RESERVE_TOKENS,
    estimate_agy_cli_input_tokens,
)
from blackcell.gateway import (
    DataClassification,
    GatewayBudget,
    GatewayProfile,
    LocalityPolicy,
    ModelCapability,
    ModelGateway,
    ModelRequest,
)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ("answer",),
    "properties": {"answer": {"type": "string"}},
}


class Runner:
    def __init__(
        self,
        *,
        response: object = None,
        version: str = AGY_CLI_REQUIRED_VERSION,
        stderr: str = "",
        returncode: int = 0,
        timeout: bool = False,
    ) -> None:
        self.response = {"answer": "ready"} if response is None else response
        self.version = version
        self.stderr = stderr
        self.returncode = returncode
        self.timeout = timeout
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.workspace_files: set[str] = set()
        self.request_payload: object = None

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, kwargs))
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, f"{self.version}\n", "")
        workspace = Path(kwargs["cwd"])
        if Path(command[0]).name == "git" and command[1:] == ["init", "--quiet"]:
            (workspace / ".git").mkdir()
            return subprocess.CompletedProcess(command, 0, "", "")
        if self.timeout:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        prompt = kwargs["input"]
        assert isinstance(prompt, str)
        delimited = prompt.split("BLACKCELL_CANONICAL_MODEL_REQUEST_BEGIN\n", 1)[1]
        canonical_request = delimited.rsplit(
            "\nBLACKCELL_CANONICAL_MODEL_REQUEST_END",
            1,
        )[0]
        self.request_payload = json.loads(canonical_request)
        self.workspace_files = {item.name for item in workspace.iterdir()}
        return subprocess.CompletedProcess(
            command,
            self.returncode,
            json.dumps(self.response),
            self.stderr,
        )


def test_agy_adapter_uses_pinned_plan_sandbox_and_stdin_print_boundary() -> None:
    runner = Runner()
    ticks = iter((10.0, 10.01, 10.02, 10.04))
    adapter = AgyCliModelAdapter(
        runner=runner,
        clock=lambda: next(ticks),
        timeout_ceiling_seconds=7,
    )
    request = _request(secret="never place me in argv", latency_ms=2_000)

    result = adapter.invoke(request, model_id="gemini-plan-model")

    assert adapter.adapter_id == AGY_CLI_ADAPTER_ID
    assert adapter.capabilities == {
        ModelCapability.REASON,
        ModelCapability.CODE,
        ModelCapability.REVIEW,
        ModelCapability.VERIFY,
    }
    assert adapter.local is False
    assert adapter.deterministic is False
    assert result.output == {"answer": "ready"}
    assert (result.input_tokens, result.output_tokens, result.cost_microusd) == (
        None,
        None,
        None,
    )
    assert result.latency_ms == 40

    assert runner.calls[0][0] == ["agy", "--version"]
    command, invocation = runner.calls[2]
    assert command == [
        "agy",
        "--mode",
        "plan",
        "--sandbox",
        "--model",
        "gemini-plan-model",
        "--effort",
        "high",
        "--print-timeout",
        "2s",
    ]
    assert "--print" not in command
    assert "--dangerously-skip-permissions" not in command
    assert "--add-dir" not in command
    assert all("never place me" not in token for token in command)
    assert all("blackcell-agy-model" not in token for token in command)
    assert all(Path(token).name != "gemini" for token in command)
    assert "never place me" in invocation["input"]
    assert "BLACKCELL_CANONICAL_MODEL_REQUEST_BEGIN" in invocation["input"]
    assert runner.request_payload == {
        "input": {"objective": "inspect", "private": "never place me in argv"},
        "output_schema": json.loads(json.dumps(SCHEMA)),
    }
    assert runner.workspace_files == {".git"}


def test_agy_adapter_integrates_with_schema_validating_gateway() -> None:
    runner = Runner()
    ticks = iter((1.0, 1.0, 1.0, 1.01))
    adapter = AgyCliModelAdapter(
        runner=runner,
        clock=lambda: next(ticks),
    )
    profile = GatewayProfile(
        "agy-reason",
        ModelCapability.REASON,
        adapter.adapter_id,
        "gemini-plan-model",
        0,
        False,
        False,
        DataClassification.PRIVATE,
        100,
        20,
        0,
    )

    result = ModelGateway((profile,), {adapter.adapter_id: adapter}).invoke(_request())

    assert result.response.output == {"answer": "ready"}
    assert result.response.input_tokens is None
    assert result.response.output_tokens is None
    assert result.response.cost_microusd is None


def test_agy_adapter_requires_exact_installed_version() -> None:
    runner = Runner(version="1.1.8")

    with pytest.raises(AgyCliAdapterError, match="version preflight"):
        AgyCliModelAdapter(runner=runner).invoke(_request(), model_id="gemini-plan-model")

    assert len(runner.calls) == 1


@pytest.mark.parametrize(
    ("response", "message"),
    (
        (("not", "an", "object"), "must be an object"),
        ("not-json", "must be an object"),
    ),
)
def test_agy_adapter_rejects_non_object_output(response: object, message: str) -> None:
    runner = Runner(response=response)
    ticks = iter((1.0, 1.0, 1.0, 1.01))

    with pytest.raises(AgyCliOutputError, match=message):
        AgyCliModelAdapter(
            runner=runner,
            clock=lambda: next(ticks),
        ).invoke(_request(), model_id="gemini-plan-model")


def test_agy_adapter_enforces_deadlines_and_byte_boundaries() -> None:
    unused = Runner()
    with pytest.raises(AgyCliTimeoutError, match="no admitted"):
        AgyCliModelAdapter(runner=unused).invoke(
            _request(latency_ms=0), model_id="gemini-plan-model"
        )
    assert unused.calls == []

    runner = Runner(timeout=True)
    ticks = iter((1.0, 1.0, 1.0))
    with pytest.raises(AgyCliTimeoutError, match="deadline"):
        AgyCliModelAdapter(
            runner=runner,
            clock=lambda: next(ticks),
        ).invoke(_request(latency_ms=500), model_id="gemini-plan-model")

    overflow = Runner()
    with pytest.raises(AgyCliOutputError, match="canonical model request"):
        AgyCliModelAdapter(
            runner=overflow,
            max_input_bytes=4,
        ).invoke(_request(), model_id="gemini-plan-model")
    assert overflow.calls == []


def test_agy_estimate_is_conservative_without_claiming_exact_usage() -> None:
    estimate = estimate_agy_cli_input_tokens(
        objective="Compile a bounded project plan.",
        context_character_budget=8_000,
    )

    assert AGY_CLI_PROVIDER_SCAFFOLD_RESERVE_TOKENS < estimate < 32_000
    with pytest.raises(ValueError, match="objective"):
        estimate_agy_cli_input_tokens(objective=" ", context_character_budget=8_000)


def test_agy_adapter_uses_pinned_executables() -> None:
    runner = Runner()
    ticks = iter((1.0, 1.0, 1.0, 1.01))
    agy = _executable("true")
    git = _executable("git")
    adapter = AgyCliModelAdapter(
        executable=agy,
        git_executable=git,
        runner=runner,
        clock=lambda: next(ticks),
    )

    adapter.invoke(_request(), model_id="gemini-plan-model")

    assert runner.calls[0][0][0] == str(agy)
    assert runner.calls[1][0][0] == str(git)
    assert runner.calls[2][0][0] == str(agy)


def _request(*, secret: str = "safe", latency_ms: int = 2_000) -> ModelRequest:
    return ModelRequest(
        "request:agy",
        ModelCapability.REASON,
        {"objective": "inspect", "private": secret},
        SCHEMA,
        DataClassification.PRIVATE,
        LocalityPolicy.REMOTE_ALLOWED,
        GatewayBudget(100, 20, latency_ms, 0),
        20,
        "correlation:agy",
        "run:agy",
        "node:planner",
    )


def _executable(name: str) -> Path:
    value = shutil.which(name)
    assert value is not None
    return Path(value).resolve(strict=True)
