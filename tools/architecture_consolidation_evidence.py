#!/usr/bin/env python3
"""Generate and verify source-bound architecture-consolidation evidence."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
import uuid
from collections import Counter, deque
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import quote

MANIFEST_PATH = Path("release/architecture-consolidation/verification-manifest.json")
DECISION_PATH = Path("docs/decisions/architecture-consolidation/ac07-final-evidence.json")
SBOM_PATH = Path("release/architecture-consolidation/blackcell-architecture-consolidation.cdx.json")
BASELINE_PATH = Path("docs/decisions/architecture-consolidation/ac00-baseline.json")
BLACKCELL_PLAN_PATH = Path("blackcell.plan.yaml")
PROGRAM_PLAN_PATH = Path("refactor-consolidation.plan.yaml")
ARCHITECTURE_TEST_PATH = Path("tests/architecture/test_dependencies.py")
AC00_BASELINE_SHA = "eb05e034f3e6bdcef83167df6dcfdc8a1eaf06f0"
IMPLEMENTATION_BASE_SHA = "1a249d8aaa1f5f230c8492ab249ea06d255f24ee"
EXCLUDED_OUTPUTS = (MANIFEST_PATH, DECISION_PATH, SBOM_PATH)

JsonObject = dict[str, Any]

RECORD_PAIRS = (
    (
        ("GatewayBudget", "src/blackcell/gateway/models.py", "GatewayBudget"),
        (
            "DecisionBudget",
            "src/blackcell/features/request_decision/models.py",
            "DecisionBudget",
        ),
    ),
    (
        ("RoutingDecision", "src/blackcell/gateway/models.py", "RoutingDecision"),
        (
            "DecisionRoute",
            "src/blackcell/features/request_decision/models.py",
            "DecisionRoute",
        ),
    ),
    (
        (
            "DecisionProposal",
            "src/blackcell/features/request_decision/models.py",
            "DecisionProposal",
        ),
        ("ActionProposal", "src/blackcell/control/models.py", "ActionProposal"),
    ),
    (
        (
            "blackcell.context.SignalPacket",
            "src/blackcell/context/signals.py",
            "SignalPacket",
        ),
        (
            "blackcell.features.derive_signal_packet.SignalPacket",
            "src/blackcell/features/derive_signal_packet/models.py",
            "SignalPacket",
        ),
    ),
)

EXPECTED_VERIFICATION_COMMANDS: tuple[JsonObject, ...] = (
    {
        "id": "focused-architecture",
        "argv": [
            "uv",
            "run",
            "python",
            "tools/run_pytest.py",
            "tests/architecture/test_dependencies.py",
            "tests/unit/test_architecture_consolidation_evidence.py",
            "tests/unit/test_architecture_consolidation_decision.py",
            "tests/unit/test_ci_workflow.py",
            "tests/unit/test_release_evidence.py",
            "tests/unit/test_refactor_consolidation_plan.py",
            "-q",
        ],
    },
    {"id": "ruff-format", "argv": ["uv", "run", "ruff", "format", "--check", "."]},
    {"id": "ruff-check", "argv": ["uv", "run", "ruff", "check", "."]},
    {"id": "types", "argv": ["uv", "run", "ty", "check"]},
    {
        "id": "full-suite",
        "argv": [
            "uv",
            "run",
            "python",
            "tools/run_pytest.py",
            "--cov=blackcell",
            "--cov-report=term-missing",
        ],
    },
    {
        "id": "rootless-podman",
        "environment": {"BLACKCELL_RUN_PODMAN_TESTS": "1"},
        "argv": [
            "uv",
            "run",
            "python",
            "tools/run_pytest.py",
            "tests/integration/test_podman_runtime.py",
        ],
    },
)

REQUIRED_AC07_RULES: tuple[JsonObject, ...] = (
    {
        "id": "construction-confined-to-approved-owners",
        "test": (
            "tests/architecture/test_dependencies.py::"
            "test_concrete_runtime_construction_stays_at_approved_sites"
        ),
    },
    {
        "id": "production-isolated-from-compatibility-and-experiments",
        "test": (
            "tests/architecture/test_dependencies.py::"
            "test_production_runtime_does_not_import_compatibility_or_experiments"
        ),
    },
    {
        "id": "operator-store-non-reach-through",
        "test": (
            "tests/architecture/test_dependencies.py::"
            "test_repository_runtime_composition_is_owned_by_bootstrap"
        ),
    },
    {
        "id": "replay-isolated-from-live-paths",
        "test": (
            "tests/architecture/test_dependencies.py::"
            "test_replay_slice_cannot_reach_live_models_or_actions"
        ),
    },
)


class ConsolidationEvidenceError(RuntimeError):
    """Raised when consolidation evidence cannot be produced safely."""


@dataclass(frozen=True, slots=True)
class GitBlob:
    mode: str
    object_id: str
    path: str
    payload: bytes


def _json_bytes(value: object, *, pretty: bool = True) -> bytes:
    if pretty:
        text = json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True)
    else:
        text = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return f"{text}\n".encode()


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _run_git(repo_root: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            input=input_bytes,
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise ConsolidationEvidenceError("cannot execute Git") from error
    if completed.returncode != 0:
        detail = os.fsdecode(completed.stderr).strip()
        suffix = f": {detail}" if detail else ""
        raise ConsolidationEvidenceError(f"Git command failed{suffix}")
    return completed.stdout


def _require_commit(repo_root: Path, source_sha: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ConsolidationEvidenceError(
            "source and base revisions must be lowercase forty-character commit IDs"
        )
    resolved = os.fsdecode(
        _run_git(repo_root, "rev-parse", "--verify", f"{source_sha}^{{commit}}")
    ).strip()
    if resolved != source_sha:
        raise ConsolidationEvidenceError(f"revision is not the requested commit: {source_sha}")
    return resolved


def _require_ancestor(
    repo_root: Path,
    ancestor: str,
    descendant: str,
    *,
    error_message: str = "ratified implementation base is not an ancestor of the source commit",
) -> None:
    try:
        completed = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise ConsolidationEvidenceError("cannot validate Git ancestry") from error
    if completed.returncode == 1:
        raise ConsolidationEvidenceError(error_message)
    if completed.returncode != 0:
        detail = os.fsdecode(completed.stderr).strip()
        suffix = f": {detail}" if detail else ""
        raise ConsolidationEvidenceError(f"cannot validate Git ancestry{suffix}")


def _head_commit(repo_root: Path) -> str:
    resolved = os.fsdecode(_run_git(repo_root, "rev-parse", "--verify", "HEAD^{commit}")).strip()
    return _require_commit(repo_root, resolved)


def _require_source_ancestor_of_head(repo_root: Path, source_sha: str) -> str:
    head_sha = _head_commit(repo_root)
    _require_ancestor(
        repo_root,
        source_sha,
        head_sha,
        error_message="recorded source commit is not an ancestor of HEAD",
    )
    return head_sha


def _batch_blobs(repo_root: Path, object_ids: tuple[str, ...]) -> dict[str, bytes]:
    if not object_ids:
        return {}
    requested = tuple(dict.fromkeys(object_ids))
    response = _run_git(
        repo_root,
        "cat-file",
        "--batch",
        input_bytes=b"".join(f"{object_id}\n".encode() for object_id in requested),
    )
    cursor = 0
    payloads: dict[str, bytes] = {}
    for expected_id in requested:
        header_end = response.find(b"\n", cursor)
        if header_end < 0:
            raise ConsolidationEvidenceError("Git cat-file returned a truncated header")
        header = response[cursor:header_end].decode("ascii", errors="strict").split()
        cursor = header_end + 1
        if len(header) != 3 or header[0] != expected_id or header[1] != "blob":
            raise ConsolidationEvidenceError(f"unexpected Git object response for {expected_id}")
        size = int(header[2])
        payload_end = cursor + size
        if payload_end >= len(response) or response[payload_end : payload_end + 1] != b"\n":
            raise ConsolidationEvidenceError("Git cat-file returned a truncated blob")
        payloads[expected_id] = response[cursor:payload_end]
        cursor = payload_end + 1
    if cursor != len(response):
        raise ConsolidationEvidenceError("Git cat-file returned trailing data")
    return payloads


def _source_tree(repo_root: Path, source_sha: str) -> tuple[GitBlob, ...]:
    _require_commit(repo_root, source_sha)
    raw_tree = _run_git(repo_root, "ls-tree", "-r", "-z", "--full-tree", source_sha)
    entries: list[tuple[str, str, str]] = []
    for raw_entry in raw_tree.split(b"\0"):
        if not raw_entry:
            continue
        metadata, separator, raw_path = raw_entry.partition(b"\t")
        fields = metadata.decode("ascii", errors="strict").split()
        if not separator or len(fields) != 3:
            raise ConsolidationEvidenceError("Git ls-tree returned an invalid entry")
        mode, object_type, object_id = fields
        if object_type != "blob":
            continue
        try:
            path = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ConsolidationEvidenceError("Git material paths must be UTF-8") from error
        normalized = PurePosixPath(path)
        if normalized.is_absolute() or normalized.as_posix() != path or ".." in normalized.parts:
            raise ConsolidationEvidenceError(f"Git returned an unsafe material path: {path}")
        if not re.fullmatch(r"[0-7]{6}", mode):
            raise ConsolidationEvidenceError(f"Git returned an invalid mode for {path}")
        entries.append((mode, object_id, path))

    payloads = _batch_blobs(repo_root, tuple(object_id for _, object_id, _ in entries))
    return tuple(
        GitBlob(mode=mode, object_id=object_id, path=path, payload=payloads[object_id])
        for mode, object_id, path in sorted(entries, key=lambda item: item[2].encode("utf-8"))
    )


def _blob_map(tree: tuple[GitBlob, ...]) -> dict[str, GitBlob]:
    return {blob.path: blob for blob in tree}


def _source_candidate_from_tree(source_sha: str, tree: tuple[GitBlob, ...]) -> JsonObject:
    excluded = {path.as_posix() for path in EXCLUDED_OUTPUTS}
    materials = [
        {
            "mode": blob.mode,
            "path": blob.path,
            "sha256": _sha256_bytes(blob.payload),
            "size": len(blob.payload),
        }
        for blob in tree
        if blob.path not in excluded
    ]
    canonical_document = {
        "materials": materials,
        "schema_version": "architecture-consolidation-materials/v1",
    }
    canonical_bytes = _json_bytes(canonical_document, pretty=False)
    return {
        "candidate_id": _sha256_bytes(canonical_bytes),
        "canonical_document_sha256": _sha256_bytes(canonical_bytes),
        "excluded_outputs": [path.as_posix() for path in EXCLUDED_OUTPUTS],
        "material_count": len(materials),
        "materials": materials,
        "source_sha": source_sha,
    }


def _source_candidate(repo_root: Path, source_sha: str) -> JsonObject:
    return _source_candidate_from_tree(source_sha, _source_tree(repo_root, source_sha))


def _python_sources(tree: tuple[GitBlob, ...]) -> dict[str, str]:
    sources: dict[str, str] = {}
    for blob in tree:
        if not blob.path.startswith("src/blackcell/") or not blob.path.endswith(".py"):
            continue
        try:
            sources[blob.path] = blob.payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ConsolidationEvidenceError(f"Python source must be UTF-8: {blob.path}") from error
    return sources


def _module_name(path: str) -> str:
    relative = PurePosixPath(path).relative_to("src").with_suffix("")
    return ".".join(relative.parts)


def _resolved_imports_from(importer: str, node: ast.ImportFrom) -> tuple[str, ...]:
    base: list[str] = []
    if node.level:
        package = importer.split(".")[:-1]
        retained = len(package) - node.level + 1
        if retained >= 0:
            base = package[:retained]
    if node.module:
        base.extend(node.module.split("."))
    module = ".".join(base)
    imported = [module] if module else []
    imported.extend(
        ".".join((*base, *name.name.split("."))) for name in node.names if name.name != "*"
    )
    return tuple(dict.fromkeys(imported))


def _record_fields(source: str, symbol: str, path: str) -> list[str] | None:
    tree = ast.parse(source, filename=path)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == symbol:
            return [
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            ]
    return None


def _record_similarity(sources: dict[str, str]) -> JsonObject:
    observations: list[JsonObject] = []
    unavailable: list[str] = []
    for left, right in RECORD_PAIRS:
        left_name, left_path, left_symbol = left
        right_name, right_path, right_symbol = right
        left_source = sources.get(left_path)
        right_source = sources.get(right_path)
        left_fields = (
            _record_fields(left_source, left_symbol, left_path) if left_source is not None else None
        )
        right_fields = (
            _record_fields(right_source, right_symbol, right_path)
            if right_source is not None
            else None
        )
        if left_fields is None or right_fields is None:
            unavailable.extend(
                name
                for name, fields in ((left_name, left_fields), (right_name, right_fields))
                if fields is None
            )
            continue
        left_set = set(left_fields)
        right_set = set(right_fields)
        shared = [field for field in left_fields if field in right_set]
        union = left_set | right_set
        observations.append(
            {
                "fields_by_record": {left_name: left_fields, right_name: right_fields},
                "jaccard_fraction": f"{len(left_set & right_set)}/{len(union)}",
                "records": [left_name, right_name],
                "shared_fields": shared,
            }
        )
    return {
        "advisory_only": True,
        "method": "Compare declared annotated fields for the four AC00 record pairs.",
        "observations": observations,
        "threshold": None,
        "unavailable_records": sorted(set(unavailable)),
    }


def _protocol_entries(path: str, source: str) -> list[JsonObject]:
    module_tree = ast.parse(source, filename=path)
    classes = {node.name: node for node in module_tree.body if isinstance(node, ast.ClassDef)}
    protocol_names: set[str] = set()

    def is_protocol(name: str, visiting: frozenset[str] = frozenset()) -> bool:
        if name in protocol_names:
            return True
        if name in visiting:
            return False
        node = classes.get(name)
        if node is None:
            return False
        bases = {_terminal_name(base) for base in node.bases}
        if "Protocol" in bases or any(
            base in classes and is_protocol(base, visiting | {name}) for base in bases
        ):
            protocol_names.add(name)
            return True
        return False

    for name in classes:
        is_protocol(name)

    def effective_members(name: str, visiting: frozenset[str] = frozenset()) -> set[str]:
        if name in visiting:
            return set()
        node = classes[name]
        members = {
            item.name
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for base in {_terminal_name(item) for item in node.bases}:
            if base in protocol_names:
                members.update(effective_members(base, visiting | {name}))
        return members

    entries: list[JsonObject] = []
    for name in sorted(protocol_names):
        node = classes[name]
        declared = {
            item.name
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        entries.append(
            {
                "bases": sorted({_terminal_name(base) for base in node.bases}),
                "declared_members": len(declared),
                "effective_members": len(effective_members(name)),
                "path": path,
                "symbol": name,
            }
        )
    return entries


def _protocol_breadth(sources: dict[str, str]) -> JsonObject:
    entries = [
        entry
        for path, source in sorted(sources.items())
        for entry in _protocol_entries(path, source)
    ]
    return {
        "advisory_only": True,
        "method": "Count declared and transitive same-module Protocol method members.",
        "observations": entries,
        "threshold": None,
    }


def _terminal_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _terminal_name(node.value)
    return ""


def _import_breadth(sources: dict[str, str]) -> JsonObject:
    observations: list[JsonObject] = []
    for path, source in sorted(sources.items()):
        importer = _module_name(path)
        roots: set[str] = set()
        tree = ast.parse(source, filename=path)
        for node in ast.walk(tree):
            imported: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                imported = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported = _resolved_imports_from(importer, node)
            for name in imported:
                parts = name.split(".")
                if len(parts) >= 2 and parts[0] == "blackcell":
                    roots.add(parts[1])
        if roots:
            observations.append(
                {"distinct_roots": len(roots), "path": path, "roots": sorted(roots)}
            )
    return {
        "advisory_only": True,
        "method": "Count distinct imported blackcell top-level roots per production module.",
        "observations": observations,
        "threshold": None,
    }


def _module_size(sources: dict[str, str]) -> JsonObject:
    return {
        "advisory_only": True,
        "method": "Count physical UTF-8 source lines per production Python module.",
        "observations": [
            {"lines": len(source.splitlines()), "path": path}
            for path, source in sorted(sources.items())
        ],
        "threshold": None,
    }


def _constructor_fan_in(sources: dict[str, str]) -> JsonObject:
    class_names: set[str] = set()
    trees: dict[str, ast.Module] = {}
    for path, source in sorted(sources.items()):
        tree = ast.parse(source, filename=path)
        trees[path] = tree
        class_names.update(node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef))
    calls: Counter[str] = Counter()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _terminal_name(node.func)
                if name in class_names:
                    calls[name] += 1
    return {
        "advisory_only": True,
        "method": (
            "Count production call expressions whose terminal name matches a production class."
        ),
        "observations": [
            {"production_call_sites": calls[name], "symbol": name} for name in sorted(calls)
        ],
        "threshold": None,
    }


def _co_change(repo_root: Path, source_sha: str) -> JsonObject:
    raw_log = os.fsdecode(
        _run_git(
            repo_root,
            "log",
            "--no-merges",
            "--format=__COMMIT__%H",
            "--name-only",
            source_sha,
            "--",
            "src/blackcell",
        )
    )
    commits: list[set[str]] = []
    current: set[str] | None = None
    for line in raw_log.splitlines():
        if line.startswith("__COMMIT__"):
            current = set()
            commits.append(current)
        elif line.startswith("src/blackcell/") and current is not None:
            parts = PurePosixPath(line).parts
            if len(parts) >= 3:
                current.add(parts[2])
    pair_counts: Counter[tuple[str, str]] = Counter()
    for roots in commits:
        pair_counts.update(combinations(sorted(roots), 2))
    return {
        "advisory_only": True,
        "limitations": [
            "history can contain broad migrations and removed package roots",
            "co-change does not distinguish causal coupling from coordinated delivery",
            "results are advisory and have no pass threshold",
        ],
        "method": (
            "Count each unordered top-level src/blackcell package pair once per non-merge commit."
        ),
        "observations": [
            {"commits": pair_counts[pair], "left": pair[0], "right": pair[1]}
            for pair in sorted(pair_counts)
        ],
        "source_commits": len(commits),
        "threshold": None,
    }


def _advisory_report_from_tree(
    repo_root: Path,
    source_sha: str,
    tree: tuple[GitBlob, ...],
) -> JsonObject:
    sources = _python_sources(tree)
    return {
        "advisory_only": True,
        "measurements": {
            "constructor_fan_in": _constructor_fan_in(sources),
            "import_breadth": _import_breadth(sources),
            "module_size": _module_size(sources),
            "package_co_change": _co_change(repo_root, source_sha),
            "protocol_breadth": _protocol_breadth(sources),
            "record_similarity": _record_similarity(sources),
        },
        "schema_version": "architecture-consolidation-advisory/v1",
        "source_sha": source_sha,
    }


def _advisory_report(repo_root: Path, source_sha: str) -> JsonObject:
    return _advisory_report_from_tree(repo_root, source_sha, _source_tree(repo_root, source_sha))


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _dependency_names(package: JsonObject) -> tuple[str, ...]:
    raw = package.get("dependencies", [])
    if not isinstance(raw, list):
        raise ConsolidationEvidenceError("uv.lock package dependencies must be a list")
    result: list[str] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ConsolidationEvidenceError("uv.lock contains an invalid dependency")
        result.append(_canonical_name(item["name"]))
    return tuple(result)


def _project_dependencies(project: JsonObject) -> set[str]:
    project_table = project.get("project")
    if not isinstance(project_table, dict) or not isinstance(
        project_table.get("dependencies"), list
    ):
        raise ConsolidationEvidenceError("pyproject.toml has no runtime dependency list")
    result: set[str] = set()
    for requirement in project_table["dependencies"]:
        if not isinstance(requirement, str):
            raise ConsolidationEvidenceError("pyproject runtime dependency must be a string")
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
        if match is None:
            raise ConsolidationEvidenceError(f"cannot parse runtime dependency: {requirement}")
        result.add(_canonical_name(match.group(0)))
    return result


def _dependency_closure(tree: tuple[GitBlob, ...]) -> JsonObject:
    blobs = _blob_map(tree)
    try:
        project = tomllib.loads(blobs["pyproject.toml"].payload.decode("utf-8"))
        lock = tomllib.loads(blobs["uv.lock"].payload.decode("utf-8"))
    except (KeyError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConsolidationEvidenceError(
            "cannot read dependency inputs from source commit"
        ) from error
    raw_packages = lock.get("package")
    if not isinstance(raw_packages, list):
        raise ConsolidationEvidenceError("uv.lock has no package inventory")
    packages: dict[str, JsonObject] = {}
    for value in raw_packages:
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            raise ConsolidationEvidenceError("uv.lock contains an invalid package")
        package = cast("JsonObject", value)
        name = _canonical_name(package["name"])
        if name in packages:
            raise ConsolidationEvidenceError(f"uv.lock has an ambiguous package: {name}")
        packages[name] = package
    root = packages.get("blackcell")
    if root is None:
        raise ConsolidationEvidenceError("uv.lock has no blackcell root package")
    direct_project = _project_dependencies(project)
    direct_locked = set(_dependency_names(root))
    if direct_project != direct_locked:
        raise ConsolidationEvidenceError("pyproject and uv.lock runtime dependencies differ")

    selected: dict[str, JsonObject] = {"blackcell": root}
    pending = deque(sorted(direct_locked))
    while pending:
        name = pending.popleft()
        if name in selected:
            continue
        package = packages.get(name)
        if package is None:
            raise ConsolidationEvidenceError(f"locked runtime dependency is missing: {name}")
        selected[name] = package
        pending.extend(_dependency_names(package))
    components = [
        {
            "dependencies": sorted(
                child for child in set(_dependency_names(selected[name])) if child in selected
            ),
            "name": name,
            "source": selected[name].get("source", {}),
            "version": str(selected[name].get("version", "")),
        }
        for name in sorted(selected)
    ]
    document = {
        "components": components,
        "direct_dependencies": sorted(direct_project),
        "schema_version": "architecture-consolidation-runtime-closure/v1",
    }
    return {**document, "sha256": _sha256_bytes(_json_bytes(document, pretty=False))}


def _dependency_comparison(
    base_sha: str,
    base_tree: tuple[GitBlob, ...],
    source_sha: str,
    source_tree: tuple[GitBlob, ...],
) -> JsonObject:
    base = _dependency_closure(base_tree)
    source = _dependency_closure(source_tree)
    changed = base["sha256"] != source["sha256"]
    return {
        "base": {"component_count": len(base["components"]), "sha256": base["sha256"]},
        "base_sha": base_sha,
        "changed": changed,
        "sbom": {
            "generated": changed,
            "path": SBOM_PATH.as_posix() if changed else None,
            "reason": (
                "locked production dependency closure changed"
                if changed
                else "locked production dependency closure is unchanged"
            ),
        },
        "source": {
            "component_count": len(source["components"]),
            "sha256": source["sha256"],
        },
        "source_sha": source_sha,
    }


def _purl(component: JsonObject) -> str:
    return (
        f"pkg:pypi/{quote(str(component['name']), safe='._~-')}"
        f"@{quote(str(component['version']), safe='._~-')}"
    )


def _build_sbom(source_sha: str, closure: JsonObject, committed_at: str) -> JsonObject:
    components_by_name = {
        str(item["name"]): item for item in cast("list[JsonObject]", closure["components"])
    }
    root = components_by_name["blackcell"]
    component_items = [
        {
            "bom-ref": _purl(component),
            "name": name,
            "purl": _purl(component),
            "scope": "required",
            "type": "library",
            "version": component["version"],
        }
        for name, component in sorted(components_by_name.items())
        if name != "blackcell"
    ]
    dependencies = [
        {
            "dependsOn": sorted(
                _purl(components_by_name[child])
                for child in cast("list[str]", component["dependencies"])
            ),
            "ref": _purl(component),
        }
        for _, component in sorted(components_by_name.items())
    ]
    serial = uuid.uuid5(uuid.NAMESPACE_URL, f"blackcell:architecture-consolidation:{source_sha}")
    return {
        "$schema": "https://cyclonedx.org/schema/bom-1.7.schema.json",
        "bomFormat": "CycloneDX",
        "components": component_items,
        "dependencies": dependencies,
        "metadata": {
            "component": {
                "bom-ref": _purl(root),
                "name": "blackcell",
                "purl": _purl(root),
                "type": "application",
                "version": root["version"],
            },
            "timestamp": committed_at,
            "tools": {
                "components": [
                    {
                        "name": "blackcell-architecture-consolidation-evidence",
                        "type": "application",
                        "version": "1",
                    }
                ]
            },
        },
        "serialNumber": f"urn:uuid:{serial}",
        "specVersion": "1.7",
        "version": 1,
    }


def _commit_timestamp(repo_root: Path, source_sha: str) -> str:
    value = os.fsdecode(_run_git(repo_root, "show", "-s", "--format=%cI", source_sha)).strip()
    if not value:
        raise ConsolidationEvidenceError("source commit has no committer timestamp")
    return value


def _load_json_blob(tree: tuple[GitBlob, ...], path: Path) -> JsonObject:
    try:
        value = json.loads(_blob_map(tree)[path.as_posix()].payload)
    except (KeyError, json.JSONDecodeError) as error:
        raise ConsolidationEvidenceError(f"cannot read source JSON: {path}") from error
    if not isinstance(value, dict):
        raise ConsolidationEvidenceError(f"source JSON must be an object: {path}")
    return cast("JsonObject", value)


def _plan_implementation_base(tree: tuple[GitBlob, ...]) -> str:
    blobs = _blob_map(tree)
    observed: list[str] = []
    pattern = re.compile(r"(?m)^  implementation_base_sha: ([0-9a-f]{40})$")
    for path in (BLACKCELL_PLAN_PATH, PROGRAM_PLAN_PATH):
        try:
            source = blobs[path.as_posix()].payload.decode("utf-8", errors="strict")
        except (KeyError, UnicodeDecodeError) as error:
            raise ConsolidationEvidenceError(f"cannot read source plan: {path}") from error
        matches = pattern.findall(source)
        if len(matches) != 1:
            raise ConsolidationEvidenceError(
                f"source plan must declare one implementation_base_sha: {path}"
            )
        observed.append(matches[0])
    if len(set(observed)) != 1:
        raise ConsolidationEvidenceError("source plans disagree on implementation_base_sha")
    return observed[0]


def _ratified_implementation_base(
    repo_root: Path,
    source_tree: tuple[GitBlob, ...],
    source_sha: str,
) -> str:
    if _plan_implementation_base(source_tree) != IMPLEMENTATION_BASE_SHA:
        raise ConsolidationEvidenceError(
            "source implementation_base_sha differs from the ratified program baseline"
        )
    _require_commit(repo_root, IMPLEMENTATION_BASE_SHA)
    _require_ancestor(repo_root, IMPLEMENTATION_BASE_SHA, source_sha)
    return IMPLEMENTATION_BASE_SHA


def _verification_results(path: Path, source_sha: str) -> list[JsonObject]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConsolidationEvidenceError("cannot read verification results") from error
    if not isinstance(value, dict):
        raise ConsolidationEvidenceError("verification results must be an object")
    if set(value) != {"commands", "schema_version", "source_sha"}:
        raise ConsolidationEvidenceError("verification results keys do not match the closed schema")
    if value.get("schema_version") != "architecture-consolidation-verification-results/v1":
        raise ConsolidationEvidenceError("unsupported verification-results schema")
    if value.get("source_sha") != source_sha:
        raise ConsolidationEvidenceError("verification results bind a different source commit")
    raw_commands = value.get("commands")
    if not isinstance(raw_commands, list):
        raise ConsolidationEvidenceError("verification results have no command list")
    if len(raw_commands) != len(EXPECTED_VERIFICATION_COMMANDS):
        raise ConsolidationEvidenceError(
            "verification command count does not match the closed contract"
        )
    by_id: dict[str, JsonObject] = {}
    expected_keys = {"argv", "environment", "exit_code", "id", "status", "summary"}
    for item in raw_commands:
        if not isinstance(item, dict) or set(item) != expected_keys:
            raise ConsolidationEvidenceError(
                "every verification command must be a closed-schema object"
            )
        identifier = item.get("id")
        if not isinstance(identifier, str):
            raise ConsolidationEvidenceError("verification command ID must be a string")
        if identifier in by_id:
            raise ConsolidationEvidenceError(f"duplicate verification command ID: {identifier}")
        by_id[identifier] = cast("JsonObject", item)
    if set(by_id) != {item["id"] for item in EXPECTED_VERIFICATION_COMMANDS}:
        raise ConsolidationEvidenceError(
            "verification command IDs do not match the closed contract"
        )
    normalized: list[JsonObject] = []
    for expected in EXPECTED_VERIFICATION_COMMANDS:
        observed = by_id[expected["id"]]
        if observed.get("argv") != expected["argv"]:
            raise ConsolidationEvidenceError(f"verification argv drift: {expected['id']}")
        if observed.get("environment", {}) != expected.get("environment", {}):
            raise ConsolidationEvidenceError(f"verification environment drift: {expected['id']}")
        if observed.get("status") != "pass" or observed.get("exit_code") != 0:
            raise ConsolidationEvidenceError(f"verification did not pass: {expected['id']}")
        if not isinstance(observed.get("summary"), str) or not observed["summary"].strip():
            raise ConsolidationEvidenceError(f"verification summary is missing: {expected['id']}")
        normalized.append(
            {
                "argv": expected["argv"],
                "environment": expected.get("environment", {}),
                "exit_code": 0,
                "id": expected["id"],
                "status": "pass",
                "summary": observed["summary"],
            }
        )
    return normalized


def _historical_runtime_materials(tree: tuple[GitBlob, ...]) -> list[JsonObject]:
    return [
        {
            "mode": blob.mode,
            "path": blob.path,
            "sha256": _sha256_bytes(blob.payload),
            "size": len(blob.payload),
        }
        for blob in tree
        if blob.path.startswith("docs/decisions/runtime-v1/")
        or blob.path.startswith("release/runtime-v1/")
    ]


def _historical_runtime_inventory(
    baseline_sha: str,
    baseline_tree: tuple[GitBlob, ...],
    source_sha: str,
    source_tree: tuple[GitBlob, ...],
) -> JsonObject:
    baseline = _historical_runtime_materials(baseline_tree)
    source = _historical_runtime_materials(source_tree)
    if source != baseline:
        raise ConsolidationEvidenceError(
            "historical runtime-v1 evidence differs from the AC00-bound inventory"
        )
    inventory_digest = _sha256_bytes(_json_bytes(source, pretty=False))
    return {
        "baseline_inventory_sha256": inventory_digest,
        "baseline_sha": baseline_sha,
        "changed": False,
        "material_count": len(source),
        "source_inventory_sha256": inventory_digest,
        "source_sha": source_sha,
        "status": "historical-read-only",
        "verification_claim_reused": False,
    }


def _architecture_binary_gate(tree: tuple[GitBlob, ...]) -> JsonObject:
    blobs = _blob_map(tree)
    try:
        source = blobs[ARCHITECTURE_TEST_PATH.as_posix()].payload.decode("utf-8", errors="strict")
    except (KeyError, UnicodeDecodeError) as error:
        raise ConsolidationEvidenceError("cannot read the architecture binary gate") from error
    module = ast.parse(source, filename=ARCHITECTURE_TEST_PATH.as_posix())
    tests = sorted(
        node.name
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    )
    required = {str(item["test"]).split("::", maxsplit=1)[1] for item in REQUIRED_AC07_RULES}
    missing = sorted(required - set(tests))
    if missing:
        raise ConsolidationEvidenceError(f"required AC07 architecture rules are missing: {missing}")
    return {
        "path": ARCHITECTURE_TEST_PATH.as_posix(),
        "test_count": len(tests),
        "tests": tests,
    }


def _build_documents(
    repo_root: Path,
    source_sha: str,
    verification_commands: list[JsonObject],
) -> tuple[bytes, bytes, bytes | None]:
    source_tree = _source_tree(repo_root, source_sha)
    baseline = _load_json_blob(source_tree, BASELINE_PATH)
    baseline_sha = str(cast("JsonObject", baseline["source"])["base_sha"])
    if baseline_sha != AC00_BASELINE_SHA:
        raise ConsolidationEvidenceError(
            "AC00 baseline decision differs from the ratified source baseline"
        )
    _require_ancestor(repo_root, baseline_sha, source_sha)
    baseline_tree = _source_tree(repo_root, baseline_sha)
    base_sha = _ratified_implementation_base(repo_root, source_tree, source_sha)
    base_tree = _source_tree(repo_root, base_sha)
    candidate = _source_candidate_from_tree(source_sha, source_tree)
    committed_at = _commit_timestamp(repo_root, source_sha)
    advisory_before = _advisory_report_from_tree(repo_root, baseline_sha, baseline_tree)
    advisory_after = _advisory_report_from_tree(repo_root, source_sha, source_tree)
    dependency = _dependency_comparison(base_sha, base_tree, source_sha, source_tree)
    source_closure = _dependency_closure(source_tree)
    binary_gate = _architecture_binary_gate(source_tree)
    sbom_bytes = (
        _json_bytes(_build_sbom(source_sha, source_closure, committed_at))
        if dependency["changed"]
        else None
    )
    advisory_document = {
        "after": advisory_after,
        "before": advisory_before,
        "policy": "All six measurements are advisory; no value is a CI threshold.",
    }
    advisory_digest = _sha256_bytes(_json_bytes(advisory_document, pretty=False))
    manifest: JsonObject = {
        "schema_version": "architecture-consolidation-verification-manifest/v1",
        "program": {
            "base_sha": base_sha,
            "branch": "refactor/consolidation",
            "candidate_id": candidate["candidate_id"],
            "issue_number": 71,
            "publication_performed": False,
            "source_committed_at": committed_at,
            "source_sha": source_sha,
            "status": "evidence-complete-unpublished",
        },
        "source": {
            "canonical_document": {
                "schema_version": "architecture-consolidation-materials/v1",
                "materials": "<path-sorted material records>",
            },
            "canonical_encoding": (
                "json.dumps(..., ensure_ascii=True, sort_keys=True, "
                "separators=(',', ':')) plus one LF, encoded as UTF-8"
            ),
            "excluded_outputs": candidate["excluded_outputs"],
            "material_count": candidate["material_count"],
            "materials": candidate["materials"],
            "materials_digest": candidate["candidate_id"],
            "selection": "every git ls-tree blob at source_sha except excluded_outputs",
        },
        "architecture_fitness": {
            "advisory": advisory_document,
            "advisory_sha256": advisory_digest,
            "binary_gate": binary_gate,
            "required_ac07_rules": list(REQUIRED_AC07_RULES),
        },
        "dependency_closure": dependency,
        "historical_runtime_v1": _historical_runtime_inventory(
            baseline_sha, baseline_tree, source_sha, source_tree
        ),
        "verification": {"commands": verification_commands, "status": "pass"},
        "boundaries": {
            "artifact_schema_changed": False,
            "event_grammar_changed": False,
            "historical_runtime_v1_evidence_changed": False,
            "public_cli_or_http_behavior_changed": False,
            "runtime_v1_verification_claim_reused": False,
        },
    }
    decision: JsonObject = {
        "schema_version": "architecture-consolidation-decision/v1",
        "work_package": "AC07",
        "issue_number": 71,
        "base_sha": base_sha,
        "source_sha": source_sha,
        "decision": "accept",
        "boundary": "binary architecture rules and advisory evolutionary-coupling evidence",
        "candidate_id": candidate["candidate_id"],
        "evidence": {
            "manifest": MANIFEST_PATH.as_posix(),
            "manifest_schema": manifest["schema_version"],
            "material_count": candidate["material_count"],
        },
        "architecture_fitness": {
            "binary_gate": binary_gate,
            "required_ac07_rules": list(REQUIRED_AC07_RULES),
        },
        "advisory": {
            "measurements": sorted(advisory_after["measurements"]),
            "report_sha256": advisory_digest,
            "thresholds": None,
        },
        "dependency_closure": dependency,
        "preserved_invariants": [
            "authoritative-event-ledger",
            "artifact-integrity-and-identity",
            "historical-live-free-replay",
            "policy-before-execution",
            "journaled-execution-and-reconciliation",
            "scheduler-fencing-and-recovery",
            "fail-closed-evidence-semantics",
            "runtime-v1-historical-evidence-immutability",
        ],
        "explicitly_unchanged": [
            "event grammar and artifact schemas",
            "CLI and HTTP behavior",
            "database schema and scheduler behavior",
            "authority, replay, durability, and recovery semantics",
        ],
        "verification": {
            "commands": [
                {"id": item["id"], "status": item["status"]} for item in verification_commands
            ],
            "status": "pass",
        },
    }
    return _json_bytes(manifest), _json_bytes(decision), sbom_bytes


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.unlink(missing_ok=True)
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ConsolidationEvidenceError(f"cannot write generated evidence: {path}") from error


def _generate(
    repo_root: Path,
    source_sha: str,
    verification_results: Path,
) -> JsonObject:
    _require_commit(repo_root, source_sha)
    commands = _verification_results(verification_results, source_sha)
    manifest_bytes, decision_bytes, sbom_bytes = _build_documents(repo_root, source_sha, commands)
    if sbom_bytes is None and (repo_root / SBOM_PATH).exists():
        raise ConsolidationEvidenceError(
            "stale consolidation SBOM exists for an unchanged dependency closure"
        )
    if sbom_bytes is not None:
        _write_atomic(repo_root / SBOM_PATH, sbom_bytes)
    _write_atomic(repo_root / MANIFEST_PATH, manifest_bytes)
    _write_atomic(repo_root / DECISION_PATH, decision_bytes)
    manifest = cast("JsonObject", json.loads(manifest_bytes))
    return {
        "candidate_id": cast("JsonObject", manifest["program"])["candidate_id"],
        "decision": DECISION_PATH.as_posix(),
        "manifest": MANIFEST_PATH.as_posix(),
        "material_count": cast("JsonObject", manifest["source"])["material_count"],
        "sbom": SBOM_PATH.as_posix() if sbom_bytes is not None else None,
        "schema_version": "architecture-consolidation-evidence-result/v1",
        "status": "generated",
    }


def _read_json(path: Path) -> JsonObject:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConsolidationEvidenceError(f"cannot read generated evidence: {path}") from error
    if not isinstance(value, dict):
        raise ConsolidationEvidenceError(f"generated evidence must be an object: {path}")
    return cast("JsonObject", value)


def _verify(repo_root: Path) -> JsonObject:
    manifest = _read_json(repo_root / MANIFEST_PATH)
    program = manifest.get("program")
    verification = manifest.get("verification")
    if not isinstance(program, dict) or not isinstance(verification, dict):
        raise ConsolidationEvidenceError("verification manifest is incomplete")
    source_sha = str(program.get("source_sha", ""))
    _require_commit(repo_root, source_sha)
    _require_source_ancestor_of_head(repo_root, source_sha)
    commands = verification.get("commands")
    if not isinstance(commands, list):
        raise ConsolidationEvidenceError("verification manifest has no command results")
    temporary_results = {
        "commands": commands,
        "schema_version": "architecture-consolidation-verification-results/v1",
        "source_sha": source_sha,
    }
    temporary_path = repo_root / MANIFEST_PATH.parent / ".verification-results.tmp"
    _write_atomic(temporary_path, _json_bytes(temporary_results))
    try:
        normalized = _verification_results(temporary_path, source_sha)
    finally:
        temporary_path.unlink(missing_ok=True)
    manifest_bytes, decision_bytes, sbom_bytes = _build_documents(repo_root, source_sha, normalized)
    expected = (
        (MANIFEST_PATH, manifest_bytes),
        (DECISION_PATH, decision_bytes),
    )
    for relative, payload in expected:
        try:
            observed = (repo_root / relative).read_bytes()
        except OSError as error:
            raise ConsolidationEvidenceError(
                f"generated evidence is missing: {relative}"
            ) from error
        if observed != payload:
            raise ConsolidationEvidenceError(f"generated evidence drift: {relative}")
    if sbom_bytes is None:
        if (repo_root / SBOM_PATH).exists():
            raise ConsolidationEvidenceError("unexpected consolidation SBOM for unchanged closure")
    elif (
        not (repo_root / SBOM_PATH).is_file() or (repo_root / SBOM_PATH).read_bytes() != sbom_bytes
    ):
        raise ConsolidationEvidenceError("generated consolidation SBOM drift")
    source = cast("JsonObject", manifest["source"])
    return {
        "candidate_id": program["candidate_id"],
        "material_count": source["material_count"],
        "schema_version": "architecture-consolidation-evidence-result/v1",
        "status": "pass",
    }


def _verify_current(repo_root: Path) -> JsonObject:
    replay = _verify(repo_root)
    manifest = _read_json(repo_root / MANIFEST_PATH)
    program = cast("JsonObject", manifest["program"])
    recorded_source = cast("JsonObject", manifest["source"])
    source_sha = str(program["source_sha"])
    head_sha = _require_source_ancestor_of_head(repo_root, source_sha)
    current = _source_candidate(repo_root, head_sha)

    expected_exclusions = [path.as_posix() for path in EXCLUDED_OUTPUTS]
    if recorded_source.get("excluded_outputs") != expected_exclusions:
        raise ConsolidationEvidenceError("recorded candidate exclusions differ from policy")
    if (
        current["candidate_id"] != program.get("candidate_id")
        or current["candidate_id"] != recorded_source.get("materials_digest")
        or current["materials"] != recorded_source.get("materials")
        or current["material_count"] != recorded_source.get("material_count")
    ):
        raise ConsolidationEvidenceError(
            "current candidate contains non-evidence changes after the recorded source commit"
        )
    return {
        "candidate_id": replay["candidate_id"],
        "head_sha": head_sha,
        "material_count": replay["material_count"],
        "schema_version": "architecture-consolidation-evidence-result/v1",
        "source_sha": source_sha,
        "status": "pass",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("candidate", "advisory"):
        subparser = subparsers.add_parser(action)
        subparser.add_argument("--repo-root", type=Path, default=Path("."))
        subparser.add_argument("--source-sha", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--repo-root", type=Path, default=Path("."))
    generate.add_argument("--source-sha", required=True)
    generate.add_argument("--verification-results", required=True, type=Path)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--repo-root", type=Path, default=Path("."))
    verify_current = subparsers.add_parser("verify-current")
    verify_current.add_argument("--repo-root", type=Path, default=Path("."))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    repo_root = arguments.repo_root.resolve()
    try:
        if arguments.action == "candidate":
            candidate = _source_candidate(repo_root, arguments.source_sha)
            result: JsonObject = {
                "candidate_id": candidate["candidate_id"],
                "excluded_outputs": candidate["excluded_outputs"],
                "material_count": candidate["material_count"],
                "schema_version": "architecture-consolidation-evidence-result/v1",
                "source_sha": arguments.source_sha,
                "status": "pass",
            }
        elif arguments.action == "advisory":
            result = _advisory_report(repo_root, arguments.source_sha)
        elif arguments.action == "generate":
            result = _generate(
                repo_root,
                arguments.source_sha,
                arguments.verification_results.resolve(),
            )
        elif arguments.action == "verify":
            result = _verify(repo_root)
        else:
            result = _verify_current(repo_root)
    except (ConsolidationEvidenceError, OSError, ValueError) as error:
        print(
            json.dumps(
                {
                    "error": str(error),
                    "schema_version": "architecture-consolidation-evidence-result/v1",
                    "status": "fail",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
