from __future__ import annotations

import json
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Set
from pathlib import Path
from typing import Any, cast

from blackcell.gateway import AdapterResult, ModelCapability, ModelRequest
from blackcell.kernel import JsonValue
from blackcell.kernel._json import canonical_json_bytes

CODEX_CLI_ADAPTER_ID = "codex-cli"
_CAPABILITIES = frozenset(
    {
        ModelCapability.REASON,
        ModelCapability.CODE,
        ModelCapability.REVIEW,
        ModelCapability.VERIFY,
    }
)
_MODEL_INPUT_FILE = "model-input.json"
_OUTPUT_SCHEMA_FILE = "output-schema.json"
_RESPONSE_FILE = "model-response.json"
_PROMPT = (
    "Read model-input.json and return exactly one JSON object conforming to "
    "output-schema.json. Do not execute tools, modify project files, inspect credentials, "
    "or request additional authority."
)

RunCommand = Callable[..., subprocess.CompletedProcess[str]]
MonotonicClock = Callable[[], float]


class CodexCliAdapterError(RuntimeError):
    """The bounded host-model process failed without exposing provider content."""


class CodexCliOutputError(CodexCliAdapterError):
    """The host-model output violated its structural or byte boundary."""


class CodexCliTimeoutError(TimeoutError):
    """The host-model process exhausted the admitted request deadline."""


class CodexCliModelAdapter:
    """Invoke Codex CLI as a schema-only, read-only gateway model adapter.

    The model receives one canonical input document in an otherwise empty temporary Git
    workspace. Gateway policy continues to own route selection, classification, budgets,
    determinism, and final output-schema validation.
    """

    def __init__(
        self,
        *,
        timeout_ceiling_seconds: float = 120.0,
        max_input_bytes: int = 1_048_576,
        max_stdout_bytes: int = 1_048_576,
        max_stderr_bytes: int = 65_536,
        max_response_bytes: int = 1_048_576,
        runner: RunCommand = subprocess.run,
        clock: MonotonicClock = time.monotonic,
    ) -> None:
        if (
            isinstance(timeout_ceiling_seconds, bool)
            or not isinstance(timeout_ceiling_seconds, int | float)
            or timeout_ceiling_seconds <= 0
        ):
            raise ValueError("Codex CLI timeout ceiling must be positive")
        for name, value in (
            ("max_input_bytes", max_input_bytes),
            ("max_stdout_bytes", max_stdout_bytes),
            ("max_stderr_bytes", max_stderr_bytes),
            ("max_response_bytes", max_response_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._timeout_ceiling_seconds = float(timeout_ceiling_seconds)
        self._max_input_bytes = max_input_bytes
        self._max_stdout_bytes = max_stdout_bytes
        self._max_stderr_bytes = max_stderr_bytes
        self._max_response_bytes = max_response_bytes
        self._runner = runner
        self._clock = clock

    @property
    def adapter_id(self) -> str:
        return CODEX_CLI_ADAPTER_ID

    @property
    def capabilities(self) -> Set[ModelCapability]:
        return _CAPABILITIES

    @property
    def local(self) -> bool:
        return False

    @property
    def deterministic(self) -> bool:
        return False

    def invoke(self, request: ModelRequest, *, model_id: str) -> AdapterResult:
        _validate_model_id(model_id)
        deadline_seconds = min(
            self._timeout_ceiling_seconds,
            request.budget.max_latency_ms / 1000,
        )
        if deadline_seconds <= 0:
            raise CodexCliTimeoutError("Codex CLI request has no admitted execution time")

        input_bytes = canonical_json_bytes(request.input)
        schema_bytes = canonical_json_bytes(request.output_schema)
        if len(input_bytes) + len(schema_bytes) > self._max_input_bytes:
            raise CodexCliOutputError("Codex CLI model input and schema exceed their byte boundary")

        started = self._clock()
        with tempfile.TemporaryDirectory(prefix="blackcell-codex-model-") as directory:
            workspace = Path(directory)
            self._initialize_repository(workspace, deadline_seconds)
            input_path = workspace / _MODEL_INPUT_FILE
            schema_path = workspace / _OUTPUT_SCHEMA_FILE
            response_path = workspace / _RESPONSE_FILE
            _write_private(input_path, input_bytes)
            _write_private(schema_path, schema_bytes)
            _write_private(response_path, b"")

            remaining = deadline_seconds - max(0.0, self._clock() - started)
            if remaining <= 0:
                raise CodexCliTimeoutError("Codex CLI request exhausted its setup deadline")
            command = _command(workspace, schema_path, response_path, model_id)
            try:
                completed = self._runner(
                    command,
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    timeout=remaining,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise CodexCliTimeoutError("Codex CLI request exceeded its deadline") from error
            except OSError as error:
                raise CodexCliAdapterError("Codex CLI process could not be started") from error

            duration_seconds = max(0.0, self._clock() - started)
            if duration_seconds > deadline_seconds:
                raise CodexCliTimeoutError("Codex CLI request exceeded its deadline")
            stdout = _bounded_text(completed.stdout, self._max_stdout_bytes, "stdout")
            _bounded_text(completed.stderr, self._max_stderr_bytes, "stderr")
            if completed.returncode != 0:
                raise CodexCliAdapterError(
                    f"Codex CLI process exited with status {completed.returncode}"
                )

            output = _read_response(response_path, self._max_response_bytes)
            input_tokens, output_tokens = _usage(stdout)

        return AdapterResult(
            output=cast("Mapping[str, JsonValue]", output),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=round(duration_seconds * 1000),
            cost_microusd=0,
            deterministic=False,
        )

    def _initialize_repository(self, workspace: Path, deadline_seconds: float) -> None:
        try:
            completed = self._runner(
                ["git", "init", "--quiet"],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=min(deadline_seconds, 10.0),
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise CodexCliTimeoutError(
                "Codex CLI workspace initialization exceeded its deadline"
            ) from error
        except OSError as error:
            raise CodexCliAdapterError(
                "Codex CLI workspace initialization could not start"
            ) from error
        if completed.returncode != 0:
            raise CodexCliAdapterError("Codex CLI workspace initialization failed")


def _command(
    workspace: Path,
    schema_path: Path,
    response_path: Path,
    model_id: str,
) -> list[str]:
    return [
        "codex",
        "--ask-for-approval",
        "never",
        "--sandbox",
        "read-only",
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--json",
        "--ephemeral",
        "--cd",
        str(workspace),
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(response_path),
        "--model",
        model_id,
        _PROMPT,
    ]


def _validate_model_id(model_id: str) -> None:
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("Codex CLI model ID must not be empty")
    if any(ord(character) < 32 or ord(character) == 127 for character in model_id):
        raise ValueError("Codex CLI model ID contains a control character")
    if len(model_id.encode("utf-8")) > 256:
        raise ValueError("Codex CLI model ID exceeds its byte boundary")


def _write_private(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def _bounded_text(value: object, maximum_bytes: int, stream: str) -> str:
    if not isinstance(value, str):
        raise CodexCliOutputError(f"Codex CLI {stream} is not text")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise CodexCliOutputError(f"Codex CLI {stream} exceeds its byte boundary")
    return value


def _read_response(path: Path, maximum_bytes: int) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise CodexCliOutputError("Codex CLI emitted no regular structured response")
    try:
        if path.stat().st_size > maximum_bytes:
            raise CodexCliOutputError("Codex CLI structured response exceeds its byte boundary")
        raw = path.read_bytes()
    except CodexCliOutputError:
        raise
    except OSError as error:
        raise CodexCliOutputError("Codex CLI structured response could not be read") from error
    if len(raw) > maximum_bytes:
        raise CodexCliOutputError("Codex CLI structured response exceeds its byte boundary")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CodexCliOutputError("Codex CLI structured response is not valid JSON") from error
    if not isinstance(value, Mapping):
        raise CodexCliOutputError("Codex CLI structured response must be an object")
    return value


def _usage(stdout: str) -> tuple[int, int]:
    usage: tuple[int, int] | None = None
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue
        raw = event.get("usage")
        if not isinstance(raw, Mapping):
            continue
        input_tokens = _token(raw, "input_tokens", "prompt_tokens")
        output_tokens = _token(raw, "output_tokens", "completion_tokens")
        if input_tokens is not None and output_tokens is not None:
            usage = (input_tokens, output_tokens)
    if usage is None:
        raise CodexCliOutputError("Codex CLI response omitted exact token usage")
    return usage


def _token(value: Mapping[object, object], *keys: str) -> int | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            return candidate
    return None


__all__ = [
    "CODEX_CLI_ADAPTER_ID",
    "CodexCliAdapterError",
    "CodexCliModelAdapter",
    "CodexCliOutputError",
    "CodexCliTimeoutError",
]
