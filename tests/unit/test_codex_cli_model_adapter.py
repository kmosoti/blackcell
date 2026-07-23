from __future__ import annotations

import json
import shutil
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from blackcell.adapters.models import (
    CODEX_CLI_ADAPTER_ID,
    CodexCliAdapterError,
    CodexCliModelAdapter,
    CodexCliOutputError,
    CodexCliTimeoutError,
    GatewayDecisionAdapter,
)
from blackcell.adapters.models.codex_cli import (
    CODEX_CLI_PROVIDER_SCAFFOLD_RESERVE_TOKENS,
    estimate_codex_cli_input_tokens,
)
from blackcell.adapters.persistence.sqlite import SQLiteDecisionAttemptJournal
from blackcell.features.request_decision import (
    DecisionAffordance,
    DecisionBudget,
    DecisionCapability,
    DecisionClassification,
    DecisionLocality,
    DecisionPreparation,
    DecisionRequirements,
    DecisionSuccessRecord,
    RequestDecision,
    RequestDecisionHandler,
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

NOW = datetime(2026, 7, 17, 12, tzinfo=UTC)

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
        stdout: str | None = None,
        stderr: str = "",
        returncode: int = 0,
        timeout: bool = False,
    ) -> None:
        self.response = {"answer": "ready"} if response is None else response
        self.stdout = stdout or json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 41, "output_tokens": 9},
            }
        )
        self.stderr = stderr
        self.returncode = returncode
        self.timeout = timeout
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.workspace_files: set[str] = set()
        self.input_payload: object = None
        self.schema_payload: object = None
        self.private_modes: dict[str, int] = {}

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, kwargs))
        workspace = Path(kwargs["cwd"])
        if Path(command[0]).name == "git" and command[1] == "init":
            (workspace / ".git").mkdir()
            return subprocess.CompletedProcess(command, 0, "", "")
        if self.timeout:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        schema_path = workspace / "output-schema.json"
        response_path = Path(command[command.index("--output-last-message") + 1])
        prompt = kwargs["input"]
        assert isinstance(prompt, str)
        delimited = prompt.split("BLACKCELL_CANONICAL_MODEL_INPUT_BEGIN\n", 1)[1]
        canonical_input = delimited.rsplit(
            "\nBLACKCELL_CANONICAL_MODEL_INPUT_END",
            1,
        )[0]
        self.input_payload = json.loads(canonical_input)
        self.schema_payload = json.loads(schema_path.read_bytes())
        response_path.write_text(json.dumps(self.response), encoding="utf-8")
        self.workspace_files = {item.name for item in workspace.iterdir()}
        self.private_modes = {
            item.name: stat.S_IMODE(item.stat().st_mode) for item in (schema_path, response_path)
        }
        return subprocess.CompletedProcess(
            command,
            self.returncode,
            self.stdout,
            self.stderr,
        )


def test_codex_cli_adapter_uses_exact_isolated_read_only_boundary() -> None:
    runner = Runner()
    ticks = iter((10.0, 10.01, 10.03))
    adapter = CodexCliModelAdapter(
        timeout_ceiling_seconds=7,
        runner=runner,
        clock=lambda: next(ticks),
    )
    request = _request(secret="never place me in argv", latency_ms=2_000)

    result = adapter.invoke(request, model_id="gpt-test")

    assert adapter.adapter_id == CODEX_CLI_ADAPTER_ID
    assert adapter.capabilities == {
        ModelCapability.REASON,
        ModelCapability.CODE,
        ModelCapability.REVIEW,
        ModelCapability.VERIFY,
    }
    assert adapter.local is False
    assert adapter.deterministic is False
    assert result.output == {"answer": "ready"}
    assert (result.input_tokens, result.output_tokens, result.latency_ms) == (41, 9, 30)
    assert result.cost_microusd == 0
    assert result.deterministic is False

    command, invocation = runner.calls[1]
    assert command[:5] == [
        "codex",
        "--ask-for-approval",
        "never",
        "--sandbox",
        "read-only",
    ]
    assert command[5] == "exec"
    assert command[command.index("--model") + 1] == "gpt-test"
    assert {"--ignore-user-config", "--ignore-rules", "--json", "--ephemeral"} <= set(command)
    assert "--output-schema" in command
    assert "--output-last-message" in command
    assert command[-1] == "-"
    assert {
        "apps",
        "browser_use",
        "computer_use",
        "goals",
        "image_generation",
        "multi_agent",
        "multi_agent_v2",
        "shell_tool",
        "unified_exec",
    } <= {command[index + 1] for index, item in enumerate(command[:-1]) if item == "--disable"}
    assert all("never place me" not in item for item in command)
    assert "never place me" in invocation["input"]
    assert "BLACKCELL_CANONICAL_MODEL_INPUT_BEGIN" in invocation["input"]
    assert "BLACKCELL_CANONICAL_MODEL_INPUT_END" in invocation["input"]
    assert "model-input.json" not in invocation["input"]
    assert "output-schema.json" not in invocation["input"]
    assert "shell" not in invocation
    assert invocation["timeout"] == pytest.approx(1.99)
    assert runner.workspace_files == {
        ".git",
        "output-schema.json",
        "model-response.json",
    }
    assert runner.input_payload == request.input
    assert runner.schema_payload == json.loads(json.dumps(SCHEMA))
    assert runner.private_modes == {
        "output-schema.json": 0o600,
        "model-response.json": 0o600,
    }


def test_codex_cli_adapter_uses_pinned_executables_and_explicit_environment() -> None:
    runner = Runner()
    ticks = iter((10.0, 10.0, 10.01))
    git = _executable("git")
    codex = _executable("true")
    environment = {"CODEX_HOME": "/tmp/codex-test"}
    adapter = CodexCliModelAdapter(
        executable=codex,
        git_executable=git,
        environment=environment,
        runner=runner,
        clock=lambda: next(ticks),
    )

    adapter.invoke(_request(), model_id="gpt-test")

    assert runner.calls[0][0][0] == str(git)
    assert runner.calls[1][0][0] == str(codex)
    assert runner.calls[0][1]["env"] == environment
    assert runner.calls[1][1]["env"] == environment


def test_codex_cli_adapter_rejects_pinned_executable_identity_drift(tmp_path: Path) -> None:
    executable = tmp_path / "codex"
    shutil.copyfile(_executable("true"), executable)
    executable.chmod(0o755)
    runner = Runner()
    adapter = CodexCliModelAdapter(executable=executable, runner=runner)
    executable.chmod(0o775)

    with pytest.raises(CodexCliAdapterError, match="identity changed"):
        adapter.invoke(_request(), model_id="gpt-test")

    assert len(runner.calls) == 1


def test_codex_cli_adapter_integrates_with_gateway_policy() -> None:
    runner = Runner()
    ticks = iter((20.0, 20.0, 20.01))
    adapter = CodexCliModelAdapter(runner=runner, clock=lambda: next(ticks))
    profile = GatewayProfile(
        "codex-reason",
        ModelCapability.REASON,
        adapter.adapter_id,
        "gpt-test",
        0,
        False,
        False,
        DataClassification.INTERNAL,
        100,
        20,
        100,
    )
    gateway = ModelGateway((profile,), {adapter.adapter_id: adapter})

    result = gateway.invoke(_request())

    assert result.decision.adapter_id == CODEX_CLI_ADAPTER_ID
    assert result.decision.model_id == "gpt-test"
    assert result.response.output == {"answer": "ready"}
    assert result.response.deterministic is False


def test_codex_cli_transport_completes_the_durable_decision_stack(tmp_path: Path) -> None:
    response = {
        "proposal_id": "proposal:codex",
        "context_frame_id": "sha256:" + "1" * 64,
        "affordance": "inspect",
        "arguments": (),
        "rationale": "inspect the bounded repository context",
        "evidence_event_ids": (),
    }
    runner = Runner(response=response)
    ticks = iter((20.0, 20.0, 20.01))
    adapter = CodexCliModelAdapter(runner=runner, clock=lambda: next(ticks))
    profile = GatewayProfile(
        "codex-reason",
        ModelCapability.REASON,
        adapter.adapter_id,
        "gpt-test",
        0,
        False,
        False,
        DataClassification.PRIVATE,
        100,
        20,
        100,
    )
    gateway = GatewayDecisionAdapter(
        ModelGateway(
            (profile,),
            {adapter.adapter_id: adapter},
            clock=lambda: NOW,
        ),
        clock=lambda: NOW,
    )
    journal = SQLiteDecisionAttemptJournal(tmp_path / "decision-artifacts")
    handler = RequestDecisionHandler(gateway, journal, clock=lambda: NOW)
    request = _decision_request()

    preparation = handler.prepare(request)
    assert isinstance(preparation, DecisionPreparation)
    outcome = handler.handle(preparation)

    assert isinstance(outcome, DecisionSuccessRecord)
    assert outcome.response.proposal.proposal_id == "proposal:codex"
    assert outcome.usage.input_tokens == 41
    assert journal.get_terminal(request.request_id) == outcome


def test_codex_cli_adapter_enforces_zero_and_subprocess_deadlines() -> None:
    unused = Runner()
    with pytest.raises(CodexCliTimeoutError, match="no admitted"):
        CodexCliModelAdapter(runner=unused).invoke(_request(latency_ms=0), model_id="gpt-test")
    assert unused.calls == []

    runner = Runner(timeout=True)
    ticks = iter((1.0, 1.0))
    with pytest.raises(TimeoutError, match="deadline"):
        CodexCliModelAdapter(runner=runner, clock=lambda: next(ticks)).invoke(
            _request(latency_ms=500),
            model_id="gpt-test",
        )
    assert runner.calls[1][1]["timeout"] == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("adapter_kwargs", "runner_kwargs", "message"),
    (
        ({"max_stdout_bytes": 4}, {}, "stdout"),
        ({"max_stderr_bytes": 4}, {"stderr": "provider-secret"}, "stderr"),
        ({"max_response_bytes": 4}, {}, "structured response"),
        ({}, {"response": ("not", "an", "object")}, "must be an object"),
        ({}, {"stdout": json.dumps({"type": "turn.completed"})}, "token usage"),
    ),
)
def test_codex_cli_adapter_enforces_all_output_boundaries(
    adapter_kwargs: dict[str, int],
    runner_kwargs: dict[str, Any],
    message: str,
) -> None:
    runner = Runner(**runner_kwargs)
    ticks = iter((1.0, 1.0, 1.01))
    adapter = CodexCliModelAdapter(
        runner=runner,
        clock=lambda: next(ticks),
        **adapter_kwargs,  # ty: ignore[invalid-argument-type]
    )

    with pytest.raises(CodexCliOutputError, match=message):
        adapter.invoke(_request(), model_id="gpt-test")


def test_codex_cli_adapter_rejects_input_overflow_before_process_creation() -> None:
    runner = Runner()
    adapter = CodexCliModelAdapter(max_input_bytes=4, runner=runner)

    with pytest.raises(CodexCliOutputError, match="model input and schema"):
        adapter.invoke(_request(), model_id="gpt-test")

    assert runner.calls == []


def test_codex_cli_estimate_uses_explicit_envelope_and_scaffold_reserve() -> None:
    estimate = estimate_codex_cli_input_tokens(
        objective="Inspect repository readiness.",
        context_character_budget=8_000,
    )

    assert estimate > CODEX_CLI_PROVIDER_SCAFFOLD_RESERVE_TOKENS
    assert estimate < 32_000
    with pytest.raises(ValueError, match="objective"):
        estimate_codex_cli_input_tokens(objective=" ", context_character_budget=8_000)


def test_codex_cli_adapter_does_not_echo_provider_or_request_content_on_failure() -> None:
    runner = Runner(returncode=2, stderr="provider-secret-output")
    ticks = iter((1.0, 1.0, 1.01))
    adapter = CodexCliModelAdapter(runner=runner, clock=lambda: next(ticks))

    with pytest.raises(CodexCliAdapterError) as caught:
        adapter.invoke(_request(secret="request-secret-input"), model_id="gpt-test")

    assert "provider-secret-output" not in str(caught.value)
    assert "request-secret-input" not in str(caught.value)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    (
        ({"timeout_ceiling_seconds": 0}, "timeout ceiling"),
        ({"max_input_bytes": 0}, "max_input_bytes"),
        ({"max_stdout_bytes": True}, "max_stdout_bytes"),
    ),
)
def test_codex_cli_adapter_rejects_invalid_constructor_bounds(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        CodexCliModelAdapter(**kwargs)  # ty: ignore[invalid-argument-type]


def test_codex_cli_adapter_rejects_invalid_explicit_environment() -> None:
    with pytest.raises(ValueError, match="environment"):
        CodexCliModelAdapter(environment={"INVALID": "value\x00"})


def _request(*, secret: str = "safe", latency_ms: int = 2_000) -> ModelRequest:
    return ModelRequest(
        "request:1",
        ModelCapability.REASON,
        {"objective": "inspect", "private": secret},
        SCHEMA,
        DataClassification.INTERNAL,
        LocalityPolicy.REMOTE_ALLOWED,
        GatewayBudget(100, 20, latency_ms, 100),
        20,
        "correlation:1",
        "run:1",
        "node:planner",
        deterministic_required=False,
    )


def _executable(name: str) -> Path:
    value = shutil.which(name)
    assert value is not None
    return Path(value).resolve(strict=True)


def _decision_request() -> RequestDecision:
    return RequestDecision(
        DecisionRequirements(
            "decision:codex",
            "node:planner",
            DecisionCapability.REASON,
            DecisionClassification.PRIVATE,
            DecisionLocality.REMOTE_ALLOWED,
            DecisionBudget(100, 20, 2_000, 100),
            20,
            False,
            NOW,
        ),
        "run:codex",
        "run:codex",
        "event:context",
        "sha256:" + "1" * 64,
        "inspect project status",
        '{"status":"ready"}',
        (),
        (DecisionAffordance("inspect"),),
    )
