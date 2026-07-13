from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
SOURCE_ROOT = ROOT / "src" / "blackcell"
RULES_PATH = ROOT / "architecture" / "dependency_rules.json"
DEBT_PATH = ROOT / "architecture" / "dependency_debt.json"


@dataclass(frozen=True, slots=True)
class ImportEdge:
    importer: str
    imported: str
    path: Path
    line: int


def test_every_package_root_is_classified() -> None:
    rules = _load_json(RULES_PATH)
    classified = set(rules["classified_roots"])
    reserved = set(rules["reserved_target_roots"])
    actual = {
        path.name for path in SOURCE_ROOT.iterdir() if path.is_dir() and path.name != "__pycache__"
    }

    unclassified = sorted(actual - classified - reserved)
    assert not unclassified, f"classify new package roots before merging: {unclassified}"


def test_kernel_has_no_outward_blackcell_dependencies() -> None:
    violations = [
        edge
        for edge in _imports()
        if edge.importer.startswith("blackcell.kernel")
        and edge.imported.startswith("blackcell.")
        and not edge.imported.startswith("blackcell.kernel")
    ]

    assert not violations, _format(violations)


def test_feature_slices_do_not_reach_across_slices_or_edges() -> None:
    violations: list[ImportEdge] = []
    for edge in _imports():
        parts = edge.importer.split(".")
        if len(parts) < 3 or parts[:2] != ["blackcell", "features"]:
            continue
        if not edge.imported.startswith("blackcell."):
            continue
        allowed = ("blackcell.kernel", f"blackcell.features.{parts[2]}")
        if not edge.imported.startswith(allowed):
            violations.append(edge)

    assert not violations, _format(violations)


def test_orchestration_contracts_do_not_depend_on_edge_or_legacy_agent_packages() -> None:
    forbidden = (
        "blackcell.adapters",
        "blackcell.agents",
        "blackcell.cli",
        "blackcell.harness",
        "blackcell.latent",
        "blackcell.runtime",
        "blackcell.world",
    )
    violations = [
        edge
        for edge in _imports()
        if edge.importer.startswith("blackcell.orchestration")
        and edge.imported.startswith(forbidden)
    ]

    assert not violations, _format(violations)


def test_workflows_and_runtime_cores_depend_inward() -> None:
    allowed_by_root = {
        "workflows": ("blackcell.kernel", "blackcell.features", "blackcell.workflows"),
        "gateway": ("blackcell.kernel", "blackcell.gateway"),
        "orchestration": (
            "blackcell.kernel",
            "blackcell.features",
            "blackcell.gateway",
            "blackcell.orchestration",
            "blackcell.workflows",
        ),
    }
    violations: list[ImportEdge] = []
    for edge in _imports():
        parts = edge.importer.split(".")
        if len(parts) < 2 or parts[0] != "blackcell":
            continue
        allowed = allowed_by_root.get(parts[1])
        if allowed is None or not edge.imported.startswith("blackcell."):
            continue
        if not edge.imported.startswith(allowed):
            violations.append(edge)

    assert not violations, _format(violations)


def test_core_packages_do_not_import_frameworks_or_provider_sdks() -> None:
    rules = _load_json(RULES_PATH)
    forbidden = tuple(rules["framework_and_provider_modules"])
    protected = (
        "blackcell.kernel",
        "blackcell.features",
        "blackcell.workflows",
        "blackcell.gateway",
        "blackcell.orchestration",
    )
    violations = [
        edge
        for edge in _imports()
        if edge.importer.startswith(protected) and edge.imported.startswith(forbidden)
    ]

    assert not violations, _format(violations)


def test_auth_contract_is_framework_and_edge_independent() -> None:
    rules = _load_json(RULES_PATH)
    forbidden = (
        *rules["framework_and_provider_modules"],
        "blackcell.adapters",
        "blackcell.bootstrap",
        "blackcell.cli",
        "blackcell.config",
        "blackcell.operator",
        "blackcell.runtime",
    )
    violations = [
        edge
        for edge in _imports()
        if edge.importer == "blackcell.interfaces.auth" and edge.imported.startswith(forbidden)
    ]

    assert not violations, _format(violations)


def test_canonical_runtime_does_not_acquire_legacy_dependencies() -> None:
    debt = _load_json(DEBT_PATH)
    legacy = tuple(entry["package"] for entry in debt["legacy_roots"])
    canonical = (
        "blackcell.kernel",
        "blackcell.domains",
        "blackcell.context",
        "blackcell.control",
        "blackcell.models",
        "blackcell.operator",
        "blackcell.evaluation",
        "blackcell.telemetry",
        "blackcell.features",
        "blackcell.workflows",
        "blackcell.gateway",
        "blackcell.orchestration",
    )
    violations = [
        edge
        for edge in _imports()
        if edge.importer.startswith(canonical) and edge.imported.startswith(legacy)
    ]

    assert not violations, _format(violations)


def test_replay_slice_cannot_reach_live_models_or_actions() -> None:
    forbidden = (
        "blackcell.adapters",
        "blackcell.features.execute_affordance",
        "blackcell.gateway",
        "blackcell.orchestration",
    )
    violations = [
        edge
        for edge in _imports()
        if edge.importer.startswith("blackcell.features.replay_run")
        and edge.imported.startswith(forbidden)
    ]

    assert not violations, _format(violations)


def test_architecture_debt_is_precise_and_shrinkable() -> None:
    debt = _load_json(DEBT_PATH)
    entries = debt["legacy_roots"]
    packages = [entry["package"] for entry in entries]

    assert packages == sorted(packages)
    assert len(packages) == len(set(packages))
    assert debt["allowed_import_violations"] == []
    for entry in entries:
        package = entry["package"]
        assert "*" not in package
        assert package.count(".") == 1
        assert (SOURCE_ROOT / package.removeprefix("blackcell.")).is_dir()
        assert entry["target"].startswith("blackcell.")
        assert entry["remove_by"].startswith("WP")


def _imports() -> tuple[ImportEdge, ...]:
    edges: list[ImportEdge] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT / "src").with_suffix("")
        importer = ".".join(relative.parts)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                edges.extend(
                    ImportEdge(importer, name.name, path, node.lineno) for name in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                edges.append(ImportEdge(importer, node.module, path, node.lineno))
    return tuple(edges)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _format(edges: list[ImportEdge]) -> str:
    return "\n".join(
        f"{edge.path.relative_to(ROOT)}:{edge.line}: {edge.importer} -> {edge.imported}"
        for edge in edges
    )
