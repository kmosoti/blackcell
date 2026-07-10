from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar, cast

from blackcell.models.base import (
    ACTION_PROPOSAL_SCHEMA,
    DecisionResult,
    JsonObject,
    ModelExecutionError,
    ModelInvocation,
    ModelTimeoutError,
    ModelUsage,
    ProposalParseError,
    ProposalParser,
    action_proposal_from_mapping,
)

ProposalT = TypeVar("ProposalT")
RunCommand = Callable[..., subprocess.CompletedProcess[str]]
Clock = Callable[[], float]


class CodexExecModel[ProposalT]:
    """Read-only Codex CLI adapter for inert, schema-constrained proposals.

    The temporary repository contains only a serialized ContextFrame and the
    requested JSON schema (plus Git metadata). Codex receives no Blackcell tool
    registry, credentials, executor object, or writable project checkout.
    """

    FRAME_FILE = "context-frame.json"
    SCHEMA_FILE = "action-proposal.schema.json"
    RESPONSE_FILE = "action-proposal.json"

    def __init__(
        self,
        *,
        executable: str = "codex",
        model: str | None = None,
        timeout_seconds: float = 120.0,
        parser: ProposalParser[ProposalT] | None = None,
        runner: RunCommand = subprocess.run,
        clock: Clock = time.monotonic,
        extra_args: Sequence[str] = (),
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        _validate_extra_args(extra_args)
        self._executable = executable
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._parser = parser or cast(ProposalParser[ProposalT], action_proposal_from_mapping)
        self._runner = runner
        self._clock = clock
        self._extra_args = tuple(extra_args)

    @property
    def name(self) -> str:
        return "codex-exec"

    def decide(
        self,
        context_frame: Mapping[str, Any],
        *,
        output_schema: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> DecisionResult[ProposalT]:
        schema = output_schema or ACTION_PROPOSAL_SCHEMA
        frame_bytes = _canonical_json(context_frame, "context_frame")
        schema_bytes = _canonical_json(schema, "output_schema")
        invocation_id = correlation_id or str(uuid.uuid4())
        started = self._clock()

        with tempfile.TemporaryDirectory(prefix="blackcell-codex-") as directory:
            workspace = Path(directory)
            self._initialize_repository(workspace)
            frame_path = workspace / self.FRAME_FILE
            schema_path = workspace / self.SCHEMA_FILE
            response_path = workspace / self.RESPONSE_FILE
            frame_path.write_bytes(frame_bytes)
            schema_path.write_bytes(schema_bytes)

            command = self._build_command(workspace, schema_path, response_path)
            try:
                completed = self._runner(
                    command,
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise ModelTimeoutError(
                    f"Codex proposal timed out after {self._timeout_seconds:g} seconds"
                ) from error
            except OSError as error:
                raise ModelExecutionError(
                    f"failed to execute {self._executable!r}: {error}"
                ) from error

            duration_ms = (self._clock() - started) * 1000
            if completed.returncode != 0:
                raise ModelExecutionError(
                    f"Codex proposal failed with exit code {completed.returncode}: "
                    f"{_bounded_error(completed.stderr)}"
                )

            value, event_metadata, usage = _extract_response(completed.stdout, response_path)
            try:
                proposal = self._parser(value)
            except ProposalParseError:
                raise
            except (KeyError, TypeError, ValueError) as error:
                raise ProposalParseError(f"invalid action proposal: {error}") from error

        configuration: JsonObject = {
            "executable": self._executable,
            "sandbox": "read-only",
            "approval_policy": "never",
            "ephemeral": True,
            "timeout_seconds": self._timeout_seconds,
            "frame_sha256": hashlib.sha256(frame_bytes).hexdigest(),
            "schema_sha256": hashlib.sha256(schema_bytes).hexdigest(),
            "structured_output": True,
            "jsonl": True,
        }
        if self._model is not None:
            configuration["model"] = self._model
        response_metadata: JsonObject = {
            "exit_code": completed.returncode,
            "stdout_lines": len(completed.stdout.splitlines()),
            "stderr_chars": len(completed.stderr),
            **event_metadata,
        }
        return DecisionResult(
            proposal=proposal,
            invocation=ModelInvocation(
                provider="openai-codex-cli",
                model=self._model,
                invocation_id=invocation_id,
                replayed=False,
                duration_ms=duration_ms,
                configuration=configuration,
                response_metadata=response_metadata,
                usage=usage,
            ),
        )

    def _initialize_repository(self, workspace: Path) -> None:
        try:
            initialized = self._runner(
                ["git", "init", "--quiet"],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=min(self._timeout_seconds, 10.0),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ModelExecutionError(
                f"could not initialize isolated Git workspace: {error}"
            ) from error
        if initialized.returncode != 0:
            raise ModelExecutionError(
                f"could not initialize isolated Git workspace: {_bounded_error(initialized.stderr)}"
            )

    def _build_command(
        self,
        workspace: Path,
        schema_path: Path,
        response_path: Path,
    ) -> list[str]:
        command = [
            self._executable,
            "exec",
            "--ask-for-approval",
            "never",
            "--ignore-user-config",
            "--json",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--cd",
            str(workspace),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(response_path),
        ]
        if self._model is not None:
            command.extend(("--model", self._model))
        command.extend(self._extra_args)
        command.append(
            "Read context-frame.json. Propose exactly one action conforming to "
            "action-proposal.schema.json. Return JSON only. Do not execute the action, "
            "modify files, access credentials, or request additional authority."
        )
        return command


def _canonical_json(value: Mapping[str, Any], name: str) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode()
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be JSON serializable") from error


def _extract_response(
    stdout: str,
    response_path: Path,
) -> tuple[Mapping[str, Any], JsonObject, ModelUsage]:
    events: list[Mapping[str, Any]] = []
    malformed_lines = 0
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed_lines += 1
            continue
        if isinstance(event, Mapping):
            events.append(event)

    value: Any = None
    if response_path.is_file():
        try:
            value = json.loads(response_path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise ProposalParseError("Codex last-message file is not valid JSON") from error

    if value is None:
        for event in reversed(events):
            candidate = _proposal_candidate(event)
            if candidate is not None:
                value = candidate
                break

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ProposalParseError("Codex response text is not valid JSON") from error
    if not isinstance(value, Mapping):
        raise ProposalParseError("Codex emitted no structured action proposal")

    usage = _extract_usage(events)
    metadata: JsonObject = {
        "event_count": len(events),
        "malformed_jsonl_lines": malformed_lines,
    }
    return value, metadata, usage


def _proposal_candidate(event: Mapping[str, Any]) -> Any:
    if "action" in event and "arguments" in event:
        return event
    for key in ("response", "output", "result", "structured_output"):
        candidate = event.get(key)
        if isinstance(candidate, (Mapping, str)):
            return candidate
    item = event.get("item")
    if isinstance(item, Mapping):
        for key in ("structured_output", "output", "content", "text"):
            candidate = item.get(key)
            if isinstance(candidate, (Mapping, str)):
                return candidate
    message = event.get("message")
    if isinstance(message, Mapping):
        for key in ("content", "text"):
            candidate = message.get(key)
            if isinstance(candidate, (Mapping, str)):
                return candidate
    return None


def _extract_usage(events: Sequence[Mapping[str, Any]]) -> ModelUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    for event in events:
        raw = event.get("usage")
        if not isinstance(raw, Mapping):
            continue
        input_tokens = _token_value(raw, "input_tokens", "prompt_tokens") or input_tokens
        output_tokens = _token_value(raw, "output_tokens", "completion_tokens") or output_tokens
        cached_input_tokens = _token_value(raw, "cached_input_tokens") or cached_input_tokens
    return ModelUsage(input_tokens, output_tokens, cached_input_tokens)


def _token_value(value: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            return item
    return None


def _bounded_error(value: str, limit: int = 500) -> str:
    compact = " ".join(value.split())
    return compact[:limit] if compact else "no diagnostic output"


def _validate_extra_args(extra_args: Sequence[str]) -> None:
    """Allow presentation-only options; control-boundary flags are fixed."""

    index = 0
    while index < len(extra_args):
        argument = extra_args[index]
        if argument == "--color":
            if index + 1 >= len(extra_args) or extra_args[index + 1] != "never":
                raise ValueError("extra_args only supports '--color never'")
            index += 2
            continue
        if argument == "--color=never":
            index += 1
            continue
        if argument.split("=", 1)[0] in {
            "--sandbox",
            "--full-auto",
            "--dangerously-bypass-approvals-and-sandbox",
            "--ask-for-approval",
            "--config",
            "-a",
            "-c",
        }:
            raise ValueError("extra_args may not override the sandbox or approval boundary")
        raise ValueError(f"unsupported Codex extra argument: {argument}")
