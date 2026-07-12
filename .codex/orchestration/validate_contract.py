#!/usr/bin/env python3
"""Validate BlackCell change specs, worker packets, and worker results."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

MAX_DOCUMENT_BYTES = 256 * 1024
SCHEMA_FILES = {
    "change-spec": "change-spec.schema.json",
    "worker-packet": "worker-packet.schema.json",
    "worker-result": "worker-result.schema.json",
}
DIRECT_VERIFICATION_TOOLS = frozenset(
    {"coverage", "hypothesis", "mutmut", "pytest", "rg", "ruff", "ty"}
)
READ_ONLY_GIT_COMMANDS = frozenset(
    {
        "blame",
        "diff",
        "grep",
        "log",
        "ls-files",
        "merge-base",
        "rev-parse",
        "show",
        "status",
    }
)
SHELL_EXECUTABLES = frozenset({"bash", "cmd", "fish", "powershell", "pwsh", "sh", "zsh"})


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


class DocumentLoadError(ValueError):
    def __init__(self, issue: ValidationIssue) -> None:
        super().__init__(issue.message)
        self.issue = issue


def validate_file(
    kind: str,
    document_path: Path,
    *,
    repo_root: Path,
    packet_path: Path | None = None,
) -> list[ValidationIssue]:
    """Validate one contract file and its linked contracts."""

    try:
        schema = _load_json(_schema_path(kind))
        document = _load_json(document_path)
    except DocumentLoadError as error:
        return [error.issue]

    issues: list[ValidationIssue] = []
    _validate_value(document, schema, "$", issues)
    if issues or not isinstance(document, dict):
        return issues

    if kind == "change-spec":
        _validate_change_spec(document, document_path, repo_root, issues)
    elif kind == "worker-packet":
        _validate_worker_packet(document, document_path, repo_root, issues)
    elif kind == "worker-result":
        if packet_path is None:
            issues.append(
                ValidationIssue(
                    "packet_required",
                    "--packet",
                    "worker-result validation requires its worker packet",
                )
            )
        else:
            _validate_worker_result(
                document,
                document_path,
                packet_path,
                repo_root,
                issues,
            )
    else:  # pragma: no cover - argparse and SCHEMA_FILES constrain this
        raise ValueError(f"unknown contract kind: {kind}")
    return issues


def _schema_path(kind: str) -> Path:
    try:
        filename = SCHEMA_FILES[kind]
    except KeyError as error:  # pragma: no cover - caller contract
        raise ValueError(f"unknown contract kind: {kind}") from error
    return Path(__file__).resolve().parent / filename


def _load_json(path: Path) -> Any:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise DocumentLoadError(
            ValidationIssue("io_error", str(path), f"cannot read JSON document: {error}")
        ) from error
    if size > MAX_DOCUMENT_BYTES:
        raise DocumentLoadError(
            ValidationIssue(
                "document_too_large",
                str(path),
                f"document exceeds {MAX_DOCUMENT_BYTES} bytes",
            )
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise DocumentLoadError(
            ValidationIssue("io_error", str(path), f"cannot read JSON document: {error}")
        ) from error
    except json.JSONDecodeError as error:
        raise DocumentLoadError(
            ValidationIssue(
                "invalid_json",
                str(path),
                f"invalid JSON at line {error.lineno}, column {error.colno}",
            )
        ) from error


def _validate_value(
    value: Any,
    schema: Mapping[str, Any],
    path: str,
    issues: list[ValidationIssue],
) -> None:
    if "const" in schema and not _json_equal(value, schema["const"]):
        _add(issues, "const", path, f"must equal {schema['const']!r}")
    if "enum" in schema and not any(_json_equal(value, item) for item in schema["enum"]):
        _add(issues, "enum", path, f"must be one of {schema['enum']!r}")

    expected_type = schema.get("type")
    if isinstance(expected_type, str) and not _matches_type(value, expected_type):
        _add(issues, "type", path, f"must be {expected_type}")
        return

    if isinstance(value, dict):
        _validate_object(value, schema, path, issues)
    elif isinstance(value, list):
        _validate_array(value, schema, path, issues)
    elif isinstance(value, str):
        _validate_string(value, schema, path, issues)
    elif isinstance(value, int) and not isinstance(value, bool):
        _validate_integer(value, schema, path, issues)


def _matches_type(value: Any, expected_type: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }.get(expected_type, False)


def _json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


def _validate_object(
    value: dict[str, Any],
    schema: Mapping[str, Any],
    path: str,
    issues: list[ValidationIssue],
) -> None:
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        _add(issues, "invalid_schema", path, "schema properties must be an object")
        return
    required = schema.get("required", [])
    if isinstance(required, list):
        for name in required:
            if isinstance(name, str) and name not in value:
                _add(issues, "required", f"{path}.{name}", "field is required")
    if schema.get("additionalProperties") is False:
        for name in sorted(set(value) - set(properties)):
            _add(issues, "unknown_field", f"{path}.{name}", "field is not declared")
    for name, item in value.items():
        item_schema = properties.get(name)
        if isinstance(item_schema, dict):
            _validate_value(item, item_schema, f"{path}.{name}", issues)


def _validate_array(
    value: list[Any],
    schema: Mapping[str, Any],
    path: str,
    issues: list[ValidationIssue],
) -> None:
    minimum = schema.get("minItems")
    maximum = schema.get("maxItems")
    if isinstance(minimum, int) and len(value) < minimum:
        _add(issues, "min_items", path, f"must contain at least {minimum} item(s)")
    if isinstance(maximum, int) and len(value) > maximum:
        _add(issues, "max_items", path, f"must contain at most {maximum} item(s)")
    if schema.get("uniqueItems") is True:
        encoded = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
        if len(encoded) != len(set(encoded)):
            _add(issues, "duplicate", path, "items must be unique")
    item_schema = schema.get("items")
    if isinstance(item_schema, dict):
        for index, item in enumerate(value):
            _validate_value(item, item_schema, f"{path}[{index}]", issues)


def _validate_string(
    value: str,
    schema: Mapping[str, Any],
    path: str,
    issues: list[ValidationIssue],
) -> None:
    minimum = schema.get("minLength")
    maximum = schema.get("maxLength")
    pattern = schema.get("pattern")
    if isinstance(minimum, int) and len(value) < minimum:
        _add(issues, "min_length", path, f"must contain at least {minimum} character(s)")
    if isinstance(maximum, int) and len(value) > maximum:
        _add(issues, "max_length", path, f"must contain at most {maximum} character(s)")
    if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
        _add(issues, "pattern", path, f"must match {pattern!r}")


def _validate_integer(
    value: int,
    schema: Mapping[str, Any],
    path: str,
    issues: list[ValidationIssue],
) -> None:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if isinstance(minimum, int) and value < minimum:
        _add(issues, "minimum", path, f"must be at least {minimum}")
    if isinstance(maximum, int) and value > maximum:
        _add(issues, "maximum", path, f"must be at most {maximum}")


def _validate_change_spec(
    document: dict[str, Any],
    source_path: Path,
    repo_root: Path,
    issues: list[ValidationIssue],
) -> None:
    work_id = document["work_id"]
    expected_path = Path("/tmp/blackcell-codex") / work_id / "change-spec.json"
    _require_exact_path(source_path, expected_path, "$", issues)
    for field in ("in_scope", "out_of_scope"):
        _validate_repo_path_list(document[field], f"$.{field}", repo_root, issues)
    overlap = sorted(set(document["in_scope"]) & set(document["out_of_scope"]))
    for path in overlap:
        _add(
            issues,
            "scope_conflict",
            "$.out_of_scope",
            f"path is also in scope: {path!r}",
        )


def _validate_worker_packet(
    document: dict[str, Any],
    source_path: Path,
    repo_root: Path,
    issues: list[ValidationIssue],
) -> None:
    work_id = document["work_id"]
    worker_id = document["worker_id"]
    work_root = Path("/tmp/blackcell-codex") / work_id
    expected_packet = work_root / "workers" / f"{worker_id}.json"
    expected_change_spec = work_root / "change-spec.json"
    expected_result_schema = repo_root / ".codex/orchestration/worker-result.schema.json"
    _require_exact_path(source_path, expected_packet, "$", issues)
    _require_exact_path(
        Path(document["change_spec_path"]),
        expected_change_spec,
        "$.change_spec_path",
        issues,
    )
    _require_exact_path(
        Path(document["result_schema_path"]),
        expected_result_schema,
        "$.result_schema_path",
        issues,
    )
    if not expected_result_schema.is_file():
        _add(
            issues,
            "missing_result_schema",
            "$.result_schema_path",
            "worker-result schema does not exist",
        )

    allowed = document["allowed_paths"]
    forbidden = document["forbidden_paths"]
    _validate_repo_path_list(allowed, "$.allowed_paths", repo_root, issues)
    _validate_repo_path_list(forbidden, "$.forbidden_paths", repo_root, issues)
    for index, required_read in enumerate(document["required_reads"]):
        path = required_read["path"]
        item_path = f"$.required_reads[{index}].path"
        if (
            _validate_repo_path(path, item_path, repo_root, issues)
            and not (repo_root / path).exists()
        ):
            _add(issues, "missing_required_read", item_path, "required path does not exist")
        _require_allowed(path, allowed, forbidden, item_path, repo_root, issues)
    for index, command in enumerate(document["verification_commands"]):
        _validate_verification_argv(
            command["argv"],
            f"$.verification_commands[{index}].argv",
            repo_root,
            issues,
        )

    change_spec_path = Path(document["change_spec_path"])
    linked_issues = validate_file("change-spec", change_spec_path, repo_root=repo_root)
    if linked_issues:
        _extend_linked(issues, "change_spec", linked_issues)
        return
    change_spec = _load_json(change_spec_path)
    if change_spec["work_id"] != work_id:
        _add(issues, "work_id_mismatch", "$.work_id", "does not match the change spec")
    _require_worker_scope(allowed, change_spec, repo_root, issues)

    if document["mode"] == "micro_edit":
        if not allowed:
            _add(
                issues,
                "micro_edit_scope",
                "$.allowed_paths",
                "micro-edit mode requires at least one allowed path",
            )
        if not change_spec["acceptance_criteria"]:
            _add(
                issues,
                "micro_edit_acceptance",
                "change_spec.acceptance_criteria",
                "micro-edit mode requires acceptance criteria",
            )
        if not document["verification_commands"]:
            _add(
                issues,
                "micro_edit_verification",
                "$.verification_commands",
                "micro-edit mode requires focused verification",
            )
    elif document["mode"] == "verify" and not document["verification_commands"]:
        _add(
            issues,
            "verify_verification",
            "$.verification_commands",
            "verify mode requires at least one verification command",
        )


def _validate_worker_result(
    document: dict[str, Any],
    source_path: Path,
    packet_path: Path,
    repo_root: Path,
    issues: list[ValidationIssue],
) -> None:
    linked_issues = validate_file("worker-packet", packet_path, repo_root=repo_root)
    if linked_issues:
        _extend_linked(issues, "worker_packet", linked_issues)
        return
    packet = _load_json(packet_path)
    change_spec = _load_json(Path(packet["change_spec_path"]))

    expected_result = (
        Path("/tmp/blackcell-codex") / packet["work_id"] / "results" / f"{packet['worker_id']}.json"
    )
    _require_exact_path(source_path, expected_result, "$", issues)
    for field in ("work_id", "worker_id"):
        if document[field] != packet[field]:
            _add(issues, f"{field}_mismatch", f"$.{field}", "does not match the worker packet")

    limits = packet["limits"]
    if len(document["observations"]) > limits["max_findings"]:
        _add(
            issues,
            "finding_limit",
            "$.observations",
            f"exceeds packet limit of {limits['max_findings']}",
        )
    if len(document["summary"]) > limits["max_summary_chars"]:
        _add(
            issues,
            "summary_limit",
            "$.summary",
            f"exceeds packet limit of {limits['max_summary_chars']} characters",
        )

    allowed = packet["allowed_paths"]
    forbidden = packet["forbidden_paths"]
    for observation_index, observation in enumerate(document["observations"]):
        for evidence_index, evidence in enumerate(observation["evidence"]):
            item_path = f"$.observations[{observation_index}].evidence[{evidence_index}]"
            path = evidence["path"]
            if _validate_repo_path(path, f"{item_path}.path", repo_root, issues):
                _validate_evidence_lines(
                    repo_root / path,
                    evidence["line_end"],
                    f"{item_path}.line_end",
                    issues,
                )
            _require_allowed(
                path,
                allowed,
                forbidden,
                f"{item_path}.path",
                repo_root,
                issues,
            )
            _require_change_scope(
                path,
                change_spec,
                f"{item_path}.path",
                repo_root,
                issues,
            )
            if evidence["line_end"] < evidence["line_start"]:
                _add(
                    issues,
                    "line_range",
                    f"{item_path}.line_end",
                    "must be greater than or equal to line_start",
                )

    for index, path in enumerate(document["changed_files"]):
        item_path = f"$.changed_files[{index}]"
        _validate_repo_path(path, item_path, repo_root, issues)
        _require_allowed(path, allowed, forbidden, item_path, repo_root, issues)
        _require_change_scope(path, change_spec, item_path, repo_root, issues)
    if packet["mode"] != "micro_edit" and document["changed_files"]:
        _add(
            issues,
            "writes_forbidden",
            "$.changed_files",
            f"{packet['mode']} mode may not report tracked-file changes",
        )

    declared = [tuple(item["argv"]) for item in packet["verification_commands"]]
    reported: list[tuple[str, ...]] = []
    for index, verification in enumerate(document["verification"]):
        argv = tuple(verification["argv"])
        reported.append(argv)
        if argv not in declared:
            _add(
                issues,
                "undeclared_verification",
                f"$.verification[{index}].argv",
                "command is not declared in the worker packet",
            )
        if document["status"] == "completed" and verification["exit_code"] != 0:
            _add(
                issues,
                "failed_verification",
                f"$.verification[{index}].exit_code",
                "completed results require successful verification",
            )
    if len(reported) != len(set(reported)):
        _add(issues, "duplicate_verification", "$.verification", "commands must be unique")
    if document["status"] == "completed":
        missing = [list(argv) for argv in declared if argv not in reported]
        if missing:
            _add(
                issues,
                "missing_verification",
                "$.verification",
                f"completed result omitted declared commands: {missing!r}",
            )


def _validate_repo_path_list(
    paths: list[str],
    field_path: str,
    repo_root: Path,
    issues: list[ValidationIssue],
) -> None:
    for index, path in enumerate(paths):
        _validate_repo_path(path, f"{field_path}[{index}]", repo_root, issues)


def _validate_repo_path(
    value: str,
    field_path: str,
    repo_root: Path,
    issues: list[ValidationIssue],
) -> bool:
    if value == ".":
        return True
    if "\\" in value or "\x00" in value or "\n" in value or "\r" in value:
        _add(issues, "invalid_path", field_path, "must be a clean POSIX repository path")
        return False
    components = value.split("/")
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or any(component in {"", ".", ".."} for component in components)
        or str(candidate) != value
    ):
        _add(
            issues,
            "invalid_path",
            field_path,
            "must be normalized, relative, and traversal-free",
        )
        return False
    resolved_root = repo_root.resolve()
    resolved_path = (resolved_root / value).resolve()
    if not resolved_path.is_relative_to(resolved_root):
        _add(issues, "path_escape", field_path, "resolves outside the repository")
        return False
    return True


def _validate_evidence_lines(
    path: Path,
    line_end: int,
    field_path: str,
    issues: list[ValidationIssue],
) -> None:
    if not path.is_file():
        _add(issues, "missing_evidence_path", field_path, "evidence path is not a file")
        return
    try:
        line_count = len(path.read_text(encoding="utf-8").splitlines())
    except OSError, UnicodeError:
        _add(issues, "unreadable_evidence", field_path, "evidence file is not UTF-8 text")
        return
    if line_end > line_count:
        _add(
            issues,
            "line_out_of_range",
            field_path,
            f"line exceeds evidence file length {line_count}",
        )


def _validate_verification_argv(
    argv: list[str],
    field_path: str,
    repo_root: Path,
    issues: list[ValidationIssue],
) -> None:
    if any(_looks_like_shell_syntax(argument) for argument in argv):
        _add(
            issues,
            "unsafe_verification",
            field_path,
            "verification argv must not contain shell syntax or control characters",
        )
        return

    executable = Path(argv[0]).name
    if executable in SHELL_EXECUTABLES:
        _add(
            issues,
            "unsafe_verification",
            field_path,
            "verification commands must not invoke a shell",
        )
        return

    if executable == "uv":
        try:
            run_index = argv.index("run", 1)
        except ValueError:
            _add(
                issues,
                "unsafe_verification",
                field_path,
                "worker verification may use uv only as a direct `uv ... run` wrapper",
            )
            return
        if run_index == len(argv) - 1:
            _add(
                issues,
                "unsafe_verification",
                field_path,
                "uv run must name a verification command",
            )
            return
        _validate_direct_verification(
            argv[run_index + 1 :],
            field_path,
            repo_root,
            issues,
        )
        return

    _validate_direct_verification(argv, field_path, repo_root, issues)


def _validate_direct_verification(
    argv: list[str],
    field_path: str,
    repo_root: Path,
    issues: list[ValidationIssue],
) -> None:
    executable = Path(argv[0]).name
    if executable in DIRECT_VERIFICATION_TOOLS:
        return
    if executable == "git" and len(argv) >= 2 and argv[1] in READ_ONLY_GIT_COMMANDS:
        return
    if executable in {"python", "python3"} and _is_safe_python_check(argv, repo_root):
        return
    if executable == "codex" and argv[1:] in (
        ["--strict-config", "--version"],
        ["--version"],
        ["--strict-config", "doctor", "--json"],
    ):
        return
    _add(
        issues,
        "unsafe_verification",
        field_path,
        "verification must be a direct test, linter, schema check, or read-only Git inspection",
    )


def _is_safe_python_check(argv: list[str], repo_root: Path) -> bool:
    if len(argv) < 2 or argv[1] == "-c":
        return False
    if argv[1] == "-m":
        return len(argv) >= 3 and argv[2] in {"json.tool", "pytest", "ruff"}

    script = Path(argv[1])
    validator = repo_root / ".codex/orchestration/validate_contract.py"
    if script.expanduser().resolve() == validator.resolve():
        return True
    return script.as_posix().endswith("/skills/.system/skill-creator/scripts/quick_validate.py")


def _looks_like_shell_syntax(argument: str) -> bool:
    if any(character in argument for character in ("\x00", "\n", "\r", "`")):
        return True
    if "$(" in argument:
        return True
    return argument in {"&", "&&", "|", "||", ";", "<", ">", ">>", "2>"}


def _require_worker_scope(
    allowed: list[str],
    change_spec: dict[str, Any],
    repo_root: Path,
    issues: list[ValidationIssue],
) -> None:
    in_scope = change_spec["in_scope"]
    out_of_scope = change_spec["out_of_scope"]
    for index, path in enumerate(allowed):
        field_path = f"$.allowed_paths[{index}]"
        if in_scope and not _path_in_scopes(path, in_scope, repo_root):
            _add(issues, "outside_change_scope", field_path, "is not within change-spec scope")
        if _path_in_scopes(path, out_of_scope, repo_root):
            _add(issues, "forbidden_change_scope", field_path, "is excluded by the change spec")


def _require_change_scope(
    path: str,
    change_spec: dict[str, Any],
    field_path: str,
    repo_root: Path,
    issues: list[ValidationIssue],
) -> None:
    in_scope = change_spec["in_scope"]
    if in_scope and not _path_in_scopes(path, in_scope, repo_root):
        _add(issues, "outside_change_scope", field_path, "is not within change-spec scope")
    if _path_in_scopes(path, change_spec["out_of_scope"], repo_root):
        _add(issues, "forbidden_change_scope", field_path, "is excluded by the change spec")


def _require_allowed(
    path: str,
    allowed: list[str],
    forbidden: list[str],
    field_path: str,
    repo_root: Path,
    issues: list[ValidationIssue],
) -> None:
    if not _path_in_scopes(path, allowed, repo_root):
        _add(issues, "outside_worker_scope", field_path, "is not within allowed_paths")
    if _path_in_scopes(path, forbidden, repo_root):
        _add(issues, "forbidden_worker_scope", field_path, "is within forbidden_paths")


def _path_in_scopes(path: str, scopes: list[str], repo_root: Path) -> bool:
    path_parts = _canonical_repo_parts(path, repo_root)
    if path_parts is None:
        return False
    for scope in scopes:
        scope_parts = _canonical_repo_parts(scope, repo_root)
        if scope_parts is None:
            continue
        if path_parts[: len(scope_parts)] == scope_parts:
            return True
    return False


def _canonical_repo_parts(value: str, repo_root: Path) -> tuple[str, ...] | None:
    resolved_root = repo_root.resolve()
    try:
        relative = (resolved_root / value).resolve().relative_to(resolved_root)
    except ValueError:
        return None
    return tuple(relative.parts)


def _require_exact_path(
    actual: Path,
    expected: Path,
    field_path: str,
    issues: list[ValidationIssue],
) -> None:
    if actual.expanduser().resolve() != expected.expanduser().resolve():
        _add(issues, "noncanonical_path", field_path, f"must resolve to {expected}")


def _extend_linked(
    issues: list[ValidationIssue],
    label: str,
    linked_issues: list[ValidationIssue],
) -> None:
    for issue in linked_issues:
        issues.append(ValidationIssue(issue.code, f"{label}:{issue.path}", issue.message))


def _add(
    issues: list[ValidationIssue],
    code: str,
    path: str,
    message: str,
) -> None:
    issues.append(ValidationIssue(code, path, message))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=tuple(SCHEMA_FILES))
    parser.add_argument("document", type=Path)
    parser.add_argument(
        "--packet",
        type=Path,
        help="Worker packet required when validating a worker result.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Repository root used for path-scope validation.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    if not (repo_root / ".git").exists():
        issues = [
            ValidationIssue(
                "invalid_repo_root",
                str(repo_root),
                "repository root must contain .git",
            )
        ]
    else:
        issues = validate_file(
            args.kind,
            args.document,
            repo_root=repo_root,
            packet_path=args.packet,
        )
    payload = {
        "valid": not issues,
        "kind": args.kind,
        "path": str(args.document),
        "errors": [issue.as_dict() for issue in issues],
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
