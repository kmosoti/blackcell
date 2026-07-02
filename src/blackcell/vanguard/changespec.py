from __future__ import annotations

import json
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from json import JSONDecodeError
from pathlib import Path
from typing import Any, cast

from blackcell.control_plane.models import (
    AgentIssueContext,
    ValidationLevel,
    ValidationMessage,
    ValidationResult,
)
from blackcell.vanguard.models import (
    ChangeSpec,
    ExecutorScope,
    QACommand,
    QAPlan,
    TemplateRecord,
    VerificationPlan,
)

DEFAULT_QA_COMMANDS: tuple[str, ...] = (
    "uv run ruff format --check .",
    "uv run ruff check .",
    "uv run pytest",
    "uv run ty check",
)

DEFAULT_ESCALATION_RULES: tuple[str, ...] = (
    "Stop and ask before expanding executor scope beyond allowed_files.",
    "Stop and ask before running any command that can mutate remote state.",
    "Treat candidate_invariants as evidence until reviewed into behavior_contract.",
)

GH_READ_ONLY_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "issue": frozenset({"list", "status", "view"}),
    "pr": frozenset({"checks", "diff", "list", "status", "view"}),
}


def draft_changespec_from_agent_context(agent_context: AgentIssueContext) -> ChangeSpec:
    return ChangeSpec(
        change_id=agent_context.key,
        issue_key=agent_context.key,
        intent=agent_context.title,
        non_goals=(),
        candidate_invariants=agent_context.context,
        behavior_contract=agent_context.change_spec,
        preserved_contracts=agent_context.definition_of_done,
        acceptance_criteria=agent_context.acceptance_criteria,
        verification=VerificationPlan(required=DEFAULT_QA_COMMANDS),
        executor_scope=ExecutorScope(
            allowed_files=agent_context.areas_of_responsibility,
            forbidden=(
                "GitHub issues, ProjectV2 fields, pull requests, and remote status transitions",
                "Git commits, pushes, merges, and issue-closing operations",
            ),
        ),
        escalation_rules=DEFAULT_ESCALATION_RULES,
    )


def changespec_from_mapping(data: Mapping[str, Any]) -> ChangeSpec:
    return ChangeSpec(
        change_id=_string(data, "change_id", "$", default=""),
        issue_key=_string(data, "issue_key", "$", default=""),
        intent=_string(data, "intent", "$", default=""),
        non_goals=_strings(data, "non_goals", "$", default=()),
        candidate_invariants=_strings(data, "candidate_invariants", "$", default=()),
        behavior_contract=_strings(data, "behavior_contract", "$", default=()),
        preserved_contracts=_strings(data, "preserved_contracts", "$", default=()),
        acceptance_criteria=_strings(data, "acceptance_criteria", "$", default=()),
        verification=_verification_plan(
            _mapping(data, "verification", "$", default={}), "$.verification"
        ),
        executor_scope=_executor_scope(
            _mapping(data, "executor_scope", "$", default={}), "$.executor_scope"
        ),
        escalation_rules=_strings(data, "escalation_rules", "$", default=()),
    )


def load_changespec(path: Path) -> ChangeSpec:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("ChangeSpec JSON must be an object")
    result = validate_changespec_mapping(raw)
    if not result.valid:
        codes = ", ".join(error.code for error in result.errors)
        raise ValueError(f"ChangeSpec is invalid: {codes}")
    return changespec_from_mapping(raw)


def validate_changespec_file(path: Path) -> ValidationResult:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except JSONDecodeError as error:
        return ValidationResult.from_messages(
            (
                _error(
                    "invalid_json",
                    f"could not parse ChangeSpec JSON: {error.msg}",
                    "$",
                ),
            )
        )

    return validate_changespec_mapping(raw)


def validate_changespec_mapping(data: object) -> ValidationResult:
    if not isinstance(data, Mapping):
        return ValidationResult.from_messages(
            (_error("invalid_changespec", "ChangeSpec JSON must be an object", "$"),)
        )

    messages: list[ValidationMessage] = []
    _validate_text(
        data.get("change_id"),
        "missing_change_id",
        "change_id must be non-empty",
        "$.change_id",
        messages,
    )
    _validate_text(
        data.get("issue_key"),
        "missing_issue_key",
        "issue_key must be non-empty",
        "$.issue_key",
        messages,
    )
    _validate_text(
        data.get("intent"), "missing_intent", "intent must be non-empty", "$.intent", messages
    )
    _validate_non_empty_strings(
        data.get("acceptance_criteria", ()),
        empty_code="empty_acceptance_criteria",
        type_code="invalid_acceptance_criteria",
        empty_message="acceptance_criteria must be non-empty",
        path="$.acceptance_criteria",
        messages=messages,
    )

    executor_scope = data.get("executor_scope", {})
    if isinstance(executor_scope, Mapping):
        _validate_non_empty_strings(
            executor_scope.get("allowed_files", ()),
            empty_code="empty_allowed_files",
            type_code="invalid_allowed_files",
            empty_message="executor_scope.allowed_files must be non-empty",
            path="$.executor_scope.allowed_files",
            messages=messages,
        )
    else:
        messages.append(
            _error(
                "invalid_executor_scope",
                "executor_scope must be an object",
                "$.executor_scope",
            )
        )

    verification = data.get("verification", {})
    if isinstance(verification, Mapping):
        _validate_verification_commands(
            verification.get("required", ()),
            path="$.verification.required",
            messages=messages,
        )
        _validate_verification_commands(
            verification.get("conditional", ()),
            path="$.verification.conditional",
            messages=messages,
        )
    else:
        messages.append(
            _error("invalid_verification", "verification must be an object", "$.verification")
        )

    if messages:
        return ValidationResult.from_messages(messages)

    try:
        spec = changespec_from_mapping(cast("Mapping[str, Any]", data))
    except ValueError as error:
        return ValidationResult.from_messages((_error("invalid_changespec", str(error), "$"),))

    return validate_changespec(spec)


def validate_changespec(spec: ChangeSpec) -> ValidationResult:
    messages: list[ValidationMessage] = []
    _validate_text(
        spec.change_id,
        "missing_change_id",
        "change_id must be non-empty",
        "$.change_id",
        messages,
    )
    _validate_text(
        spec.issue_key,
        "missing_issue_key",
        "issue_key must be non-empty",
        "$.issue_key",
        messages,
    )
    _validate_text(spec.intent, "missing_intent", "intent must be non-empty", "$.intent", messages)
    if not spec.acceptance_criteria:
        messages.append(
            _error(
                "empty_acceptance_criteria",
                "acceptance_criteria must be non-empty",
                "$.acceptance_criteria",
            )
        )
    if not spec.executor_scope.allowed_files:
        messages.append(
            _error(
                "empty_allowed_files",
                "executor_scope.allowed_files must be non-empty",
                "$.executor_scope.allowed_files",
            )
        )
    for path, command in _verification_commands(spec):
        reason = mutating_command_reason(command)
        if reason:
            messages.append(_error("mutating_verification_command", reason, path))

    return ValidationResult.from_messages(messages)


def read_changespec_file(path: Path) -> tuple[ChangeSpec | None, ValidationResult]:
    result = validate_changespec_file(path)
    if not result.valid:
        return None, result
    return load_changespec(path), result


def plan_qa(spec: ChangeSpec) -> QAPlan:
    result = validate_changespec(spec)
    if not result.valid:
        codes = ", ".join(error.code for error in result.errors)
        raise ValueError(f"ChangeSpec is invalid: {codes}")

    commands = tuple(
        QACommand(
            name=f"{'required' if required else 'conditional'}-{index}",
            command=command,
            required=required,
            mutating=False,
        )
        for required, commands in (
            (True, spec.verification.required),
            (False, spec.verification.conditional),
        )
        for index, command in enumerate(commands, start=1)
    )
    return QAPlan(change_id=spec.change_id, issue_key=spec.issue_key, commands=commands)


def render_templates() -> tuple[TemplateRecord, ...]:
    return (
        TemplateRecord(
            name="evidence-draft",
            title="Evidence Draft",
            body=(
                "Record source observations, paths, and command output. Keep candidate "
                "invariants separate from approved behavior_contract entries."
            ),
        ),
        TemplateRecord(
            name="qa-plan",
            title="QA Plan",
            body=(
                "List required and conditional verification commands. Commands must be "
                "read-only and must not use fix, snapshot update, commit, push, merge, "
                "issue-closing, or --apply modes."
            ),
        ),
        TemplateRecord(
            name="read-only-review",
            title="Read-only Review Guidance",
            body=(
                "Review evidence and ChangeSpec coverage without mutating GitHub, ProjectV2, "
                "pull requests, issue state, or local git history."
            ),
        ),
    )


def mutating_command_reason(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "verification command must be shell-parseable"

    normalized = _without_runner(tokens)
    if not normalized:
        return "verification command must be explicit"

    program = normalized[0]
    if program == "ruff":
        if len(normalized) > 1 and normalized[1] == "check" and _has_ruff_fix_flag(normalized):
            return "ruff check fix-mode commands cannot be reviewer verification"
        if len(normalized) > 1 and normalized[1] == "format" and "--check" not in normalized:
            return "ruff format verification must include --check"

    if "pytest" in normalized and _contains_snapshot_update(normalized):
        return "snapshot-update pytest commands are mutating verification"

    if program == "git" and len(normalized) > 1 and normalized[1] in {"commit", "push", "merge"}:
        return f"git {normalized[1]} is not allowed in reviewer verification"

    if program == "gh" and len(normalized) > 2:
        namespace = normalized[1]
        command = normalized[2]
        read_only_commands = GH_READ_ONLY_SUBCOMMANDS.get(namespace)
        if read_only_commands is not None and command not in read_only_commands:
            return f"gh {namespace} {command} mutates or may mutate remote GitHub state"

    if program == "blackcell" and "--apply" in normalized:
        return "blackcell --apply commands mutate remote workflow state"

    return None


def _verification_plan(data: Mapping[str, Any], path: str) -> VerificationPlan:
    return VerificationPlan(
        required=_strings(data, "required", path, default=()),
        conditional=_strings(data, "conditional", path, default=()),
    )


def _executor_scope(data: Mapping[str, Any], path: str) -> ExecutorScope:
    return ExecutorScope(
        allowed_files=_strings(data, "allowed_files", path, default=()),
        forbidden=_strings(data, "forbidden", path, default=()),
    )


def _verification_commands(spec: ChangeSpec) -> tuple[tuple[str, str], ...]:
    required = tuple(
        (f"$.verification.required[{index}]", command)
        for index, command in enumerate(spec.verification.required)
    )
    conditional = tuple(
        (f"$.verification.conditional[{index}]", command)
        for index, command in enumerate(spec.verification.conditional)
    )
    return (*required, *conditional)


def _validate_verification_commands(
    value: object,
    *,
    path: str,
    messages: list[ValidationMessage],
) -> None:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        messages.append(
            _error(
                "invalid_verification_command",
                "verification commands must be a sequence of explicit strings",
                path,
            )
        )
        return

    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, str) or not item.strip():
            messages.append(
                _error(
                    "invalid_verification_command",
                    "verification commands must be explicit strings",
                    item_path,
                )
            )
            continue
        reason = mutating_command_reason(item)
        if reason:
            messages.append(_error("mutating_verification_command", reason, item_path))


def _validate_non_empty_strings(
    value: object,
    *,
    empty_code: str,
    type_code: str,
    empty_message: str,
    path: str,
    messages: list[ValidationMessage],
) -> None:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        messages.append(_error(type_code, f"{path} must be a sequence of strings", path))
        return
    if not value:
        messages.append(_error(empty_code, empty_message, path))
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            messages.append(
                _error(type_code, f"{path} entries must be non-empty strings", f"{path}[{index}]")
            )


def _validate_text(
    value: object,
    code: str,
    message: str,
    path: str,
    messages: list[ValidationMessage],
) -> None:
    if not isinstance(value, str) or not value.strip():
        messages.append(_error(code, message, path))


def _mapping(
    data: Mapping[str, Any],
    key: str,
    path: str,
    *,
    default: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    value = data.get(key, default)
    if not isinstance(value, Mapping):
        raise ValueError(f"{path}.{key} must be an object")
    return value


def _string(
    data: Mapping[str, Any],
    key: str,
    path: str,
    *,
    default: str | None = None,
) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{path}.{key} must be a string")
    return value


def _strings(
    data: Mapping[str, Any],
    key: str,
    path: str,
    *,
    default: Sequence[str] | None = None,
) -> tuple[str, ...]:
    value = data.get(key, default)
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise ValueError(f"{path}.{key} must be a sequence of strings")
    if not all(isinstance(item, str) for item in value):
        raise ValueError(f"{path}.{key} must be a sequence of strings")
    return tuple(value)


def _without_runner(tokens: list[str]) -> list[str]:
    normalized = list(tokens)
    if len(normalized) >= 3 and normalized[0] == "uv" and normalized[1] == "run":
        normalized = normalized[2:]
    return normalized


def _contains_snapshot_update(tokens: Sequence[str]) -> bool:
    snapshot_flags = {
        "--snapshot-update",
        "--update-snapshots",
        "--accept-snapshots",
        "--snapshot-update=1",
    }
    return any(token in snapshot_flags or "snapshot-update" in token for token in tokens)


def _has_ruff_fix_flag(tokens: Sequence[str]) -> bool:
    return any(
        token in {"--fix", "--fix-only"}
        or token.startswith("--fix=")
        or token.startswith("--fix-only=")
        for token in tokens
    )


def _error(code: str, message: str, path: str) -> ValidationMessage:
    return ValidationMessage(
        level=ValidationLevel.ERROR,
        code=code,
        message=message,
        path=path,
    )


def changespec_to_mapping(spec: ChangeSpec) -> dict[str, Any]:
    return asdict(spec)
