from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blackcell.orchestration.review import EpistemicDimension
from blackcell.orchestration.verification import (
    VerificationCriterionKind,
    VerificationReasonCode,
)

ROOT = Path(__file__).parents[2]
SOURCE_ROOT = ROOT / "src" / "blackcell"
RULES_PATH = ROOT / "architecture" / "dependency_rules.json"
EXECUTABLE_ROOTS = (
    SOURCE_ROOT,
    ROOT / "tests",
    ROOT / "tools",
    ROOT / "examples",
    ROOT / ".github",
)
_MATURITY_TERMS = ("al" + "pha", "be" + "ta", "pre" + "view")
_MATURITY_LABEL = re.compile(rf"(?i)(?:^|[^a-z0-9])({'|'.join(_MATURITY_TERMS)})(?:$|[^a-z0-9])")
_GENERATION_PATH = re.compile(r"(?i)(?:^|[-_.])v[0-9]+(?:$|[-_.])")
_GENERATION_IDENTIFIER = re.compile(r"(?i)(?:^|_)v[0-9]+(?=_|$)|(?<=[a-z])v[0-9]+(?=[A-Z_]|$)")
_VERSION_LITERAL = re.compile(r"(?i)(?:/|[._-])v[0-9]+(?:$|[^0-9])")
_VERSION_BOUNDARY_MODULES = frozenset(
    {
        "blackcell.adapters.daemon_systemd",
        "blackcell.adapters.execution.bubblewrap",
        "blackcell.adapters.execution.text_changes",
        "blackcell.adapters.execution.worktree",
        "blackcell.adapters.kernform_cli",
        "blackcell.adapters.models.codex_cli",
        "blackcell.adapters.recovery.local",
        "blackcell.adapters.runtime_http",
        "blackcell.bootstrap.execution_plan",
        "blackcell.bootstrap.process",
        "blackcell.cli.app",
        "blackcell.config.execution",
        "blackcell.config.process",
        "blackcell.config.review",
        "blackcell.config.verification",
        "blackcell.gateway.configuration",
        "blackcell.interfaces.http.app",
        "blackcell.interfaces.http.contracts",
        "blackcell.interfaces.http.web",
        "blackcell.interfaces.kernform_contracts",
        "blackcell.interfaces.tui.app",
        "blackcell.interfaces.tui.controller",
        "blackcell.interfaces.tui.cursor",
        "blackcell.orchestration.acceptance",
        "blackcell.orchestration.changes",
        "blackcell.orchestration.execution_artifacts",
        "blackcell.orchestration.execution_plan",
        "blackcell.orchestration.replay",
        "blackcell.orchestration.review",
        "blackcell.orchestration.review_lifecycle",
        "blackcell.orchestration.run_lifecycle",
        "blackcell.orchestration.verification",
        "blackcell.orchestration.verification_lifecycle",
    }
)


@dataclass(frozen=True, slots=True)
class ImportEdge:
    importer: str
    imported: str
    path: Path
    line: int


def test_every_package_root_is_classified() -> None:
    classified = set(_load_json(RULES_PATH)["classified_roots"])
    actual = {
        path.name
        for path in SOURCE_ROOT.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }

    assert actual == classified


def test_kernel_has_no_outward_blackcell_dependencies() -> None:
    violations = [
        edge
        for edge in _imports()
        if edge.importer.startswith("blackcell.kernel")
        and edge.imported.startswith("blackcell.")
        and not edge.imported.startswith("blackcell.kernel")
    ]

    assert not violations, _format(violations)


def test_orchestration_contracts_depend_only_on_inward_runtime_contracts() -> None:
    allowed = (
        "blackcell.gateway",
        "blackcell.kernel",
        "blackcell.orchestration",
    )
    violations = [
        edge
        for edge in _imports()
        if edge.importer.startswith("blackcell.orchestration")
        and edge.imported.startswith("blackcell.")
        and not edge.imported.startswith(allowed)
    ]

    assert not violations, _format(violations)


def test_canonical_runtime_cannot_import_retired_implementations() -> None:
    retired = tuple(_load_json(RULES_PATH)["retired_runtime_roots"])
    violations = [
        edge
        for edge in _imports()
        if any(edge.imported == root or edge.imported.startswith(f"{root}.") for root in retired)
    ]

    assert not violations, _format(violations)


def test_retired_runtime_modules_are_absent() -> None:
    retired = tuple(_load_json(RULES_PATH)["retired_runtime_roots"])
    present = []
    for module in retired:
        relative = Path(*module.split(".")[1:])
        package = SOURCE_ROOT / relative
        if package.with_suffix(".py").exists() or any(package.glob("*.py")):
            present.append(module)

    assert not present


def test_executable_names_are_maturity_and_generation_agnostic() -> None:
    maturity_violations: list[str] = []
    path_violations: list[str] = []
    identifier_violations: list[str] = []
    for root in EXECUTABLE_ROOTS:
        if not root.exists():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(ROOT)
            if any(_GENERATION_PATH.search(part) for part in relative.parts):
                path_violations.append(str(relative))
            text = path.read_text(encoding="utf-8")
            if _MATURITY_LABEL.search(text):
                maturity_violations.append(str(relative))
            if path.suffix == ".py":
                tree = ast.parse(text, filename=str(path))
                for name, line in _declared_names(tree):
                    if _GENERATION_IDENTIFIER.search(name):
                        identifier_violations.append(f"{relative}:{line}: {name}")
            elif path.suffix == ".js":
                for match in re.finditer(
                    r"\b(?:class|function|const|let|var)\s+([A-Za-z_$][\w$]*)",
                    text,
                ):
                    if _GENERATION_IDENTIFIER.search(match.group(1)):
                        identifier_violations.append(
                            f"{relative}:{text.count(chr(10), 0, match.start()) + 1}: "
                            f"{match.group(1)}"
                        )

    assert not maturity_violations, "maturity labels are forbidden:\n" + "\n".join(
        maturity_violations
    )
    assert not path_violations, "generation-labelled paths are forbidden:\n" + "\n".join(
        path_violations
    )
    assert not identifier_violations, "generation-labelled identifiers are forbidden:\n" + (
        "\n".join(identifier_violations)
    )


def test_contract_versions_are_confined_to_boundary_modules() -> None:
    violations: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        module = _module(path)
        if module in _VERSION_BOUNDARY_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _VERSION_LITERAL.search(node.value)
            ):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: {node.value!r}")

    assert not violations, "version literals escaped explicit boundaries:\n" + "\n".join(violations)


def test_epistemic_guard_has_closed_review_and_verification_rows() -> None:
    assert {item.value for item in EpistemicDimension} == {
        "acceptance-coverage",
        "causal-overreach",
        "counterevidence",
        "evidence-grounding",
        "scope-challenge",
        "uncertainty",
    }
    assert VerificationCriterionKind.EPISTEMIC_POLICY.value == "epistemic-policy"
    assert {
        VerificationReasonCode.EPISTEMIC_CONCERN,
        VerificationReasonCode.EPISTEMIC_UNKNOWN,
        VerificationReasonCode.EPISTEMIC_COVERAGE_COMPLETE,
        VerificationReasonCode.EPISTEMIC_NOT_APPLICABLE,
    } <= set(VerificationReasonCode)


def _imports() -> tuple[ImportEdge, ...]:
    edges: list[ImportEdge] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        importer = _module(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                edges.extend(
                    ImportEdge(importer, name.name, path, node.lineno) for name in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                edges.extend(
                    ImportEdge(importer, imported, path, node.lineno)
                    for imported in _resolved_imports_from(importer, node)
                )
    return tuple(edges)


def _module(path: Path) -> str:
    relative = path.relative_to(ROOT / "src").with_suffix("")
    return ".".join(relative.parts)


def _declared_names(tree: ast.AST) -> tuple[tuple[str, int], ...]:
    names: set[tuple[str, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add((node.id, node.lineno))
        elif isinstance(node, ast.Attribute):
            names.add((node.attr, node.lineno))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add((node.name, node.lineno))
        elif isinstance(node, ast.arg):
            names.add((node.arg, node.lineno))
        elif isinstance(node, ast.alias) and node.asname is not None:
            names.add((node.asname, getattr(node, "lineno", 0)))
    return tuple(sorted(names, key=lambda item: (item[1], item[0])))


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


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _format(edges: list[ImportEdge]) -> str:
    return "\n".join(
        f"{edge.path.relative_to(ROOT)}:{edge.line}: {edge.importer} -> {edge.imported}"
        for edge in edges
    )
