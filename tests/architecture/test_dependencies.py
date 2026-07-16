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
        path.name
        for path in SOURCE_ROOT.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
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
    rules = _load_json(RULES_PATH)
    feature_dependencies = rules["feature_dependencies"]
    violations: list[ImportEdge] = []
    for edge in _imports():
        parts = edge.importer.split(".")
        if len(parts) < 3 or parts[:2] != ["blackcell", "features"]:
            continue
        if not edge.imported.startswith("blackcell."):
            continue
        dependencies = tuple(
            f"blackcell.features.{dependency}"
            for dependency in feature_dependencies.get(parts[2], ())
        )
        allowed = ("blackcell.kernel", f"blackcell.features.{parts[2]}", *dependencies)
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


def test_http_framework_imports_stay_at_interface_and_bootstrap_edges() -> None:
    framework_modules = ("litestar", "msgspec")
    allowed_importers = (
        "blackcell.interfaces.http",
        "blackcell.bootstrap",
    )
    violations = [
        edge
        for edge in _imports()
        if edge.imported.startswith(framework_modules)
        and not edge.importer.startswith(allowed_importers)
    ]

    assert not violations, _format(violations)


def test_opentelemetry_sdk_imports_stay_inside_the_telemetry_adapter() -> None:
    violations = [
        edge
        for edge in _imports()
        if edge.imported.startswith("opentelemetry")
        and not edge.importer.startswith("blackcell.adapters.telemetry")
    ]

    assert not violations, _format(violations)


def test_current_runtime_does_not_import_historical_compatibility() -> None:
    rules = _load_json(RULES_PATH)
    historical = tuple(rules["historical_compatibility_roots"])
    current = tuple(rules["current_runtime_roots"])
    violations = [
        edge
        for edge in _imports()
        if edge.importer.startswith(current) and edge.imported.startswith(historical)
    ]

    assert not violations, _format(violations)


def test_current_contract_owners_are_unambiguous() -> None:
    rules = _load_json(RULES_PATH)

    assert rules["schema_version"] == 2
    assert "legacy-canonical" not in rules["classified_roots"].values()
    assert "blackcell.adapters" in rules["current_runtime_roots"]
    assert "blackcell.adapters" in rules["benchmark_model_forbidden_importers"]
    assert rules["concept_owners"] == {
        "authorization-execution-observation": "blackcell.control",
        "benchmark-decision-models": "blackcell.models",
        "context-frame-construction": "blackcell.features.build_context",
        "durable-decision-records": "blackcell.features.request_decision",
        "evidence-selection": "blackcell.features.retrieve_evidence",
        "historical-context-baselines": "blackcell.context",
        "live-model-admission-routing": "blackcell.gateway",
        "telemetry-attribute-values": "blackcell.telemetry",
    }
    assert rules["feature_dependencies"] == {"build_context": ["retrieve_evidence"]}


def test_runtime_paths_do_not_import_benchmark_model_implementations() -> None:
    rules = _load_json(RULES_PATH)
    forbidden_importers = tuple(rules["benchmark_model_forbidden_importers"])
    violations = [
        edge
        for edge in _imports()
        if edge.importer.startswith(forbidden_importers)
        and edge.imported.startswith("blackcell.models")
    ]

    assert not violations, _format(violations)


def test_run_record_protocol_helpers_depend_on_artifact_helpers_one_way() -> None:
    artifacts = "blackcell.adapters.persistence.sqlite._run_records_v2_artifacts"
    protocol = "blackcell.adapters.persistence.sqlite._run_records_v2_protocol"
    violations = [
        edge
        for edge in _imports()
        if edge.importer == artifacts and edge.imported.startswith(protocol)
    ]

    assert not violations, _format(violations)


def test_repository_runtime_composition_is_owned_by_bootstrap() -> None:
    rules = _load_json(RULES_PATH)
    composition_root = rules["composition_root"]
    composition_path = SOURCE_ROOT / Path(*composition_root.split(".")[1:]).with_suffix(".py")
    operator_path = SOURCE_ROOT / "operator" / "facade.py"
    api_path = SOURCE_ROOT / "bootstrap" / "runtime_api.py"
    worker_path = SOURCE_ROOT / "bootstrap" / "worker.py"

    assert composition_root == "blackcell.bootstrap.repository"
    assert composition_path.is_file()
    assert not (SOURCE_ROOT / "operator" / "repository_adapters.py").exists()
    assert (SOURCE_ROOT / "adapters" / "repository" / "adapter.py").is_file()

    operator_tree = ast.parse(
        operator_path.read_text(encoding="utf-8"),
        filename=str(operator_path),
    )
    forbidden_imports = (
        "blackcell.adapters",
        "blackcell.bootstrap",
        "blackcell.config",
    )
    operator_edges = [edge for edge in _imports() if edge.path == operator_path]
    assert not [edge for edge in operator_edges if edge.imported.startswith(forbidden_imports)], (
        _format(operator_edges)
    )

    forbidden_constructors = {
        "ArtifactStore",
        "CodexCliModelAdapter",
        "EventStore",
        "KernelFeedbackRunRecorder",
        "KernelRunReplayAdapter",
        "ModelGateway",
        "RepositoryRecordedModelAdapter",
        "RepositoryStatusExecutionAdapter",
        "RepositoryStatusOutcomeObserver",
        "RepositoryStatusReader",
        "SQLiteDecisionAttemptJournal",
        "SQLiteExecutionJournal",
    }
    calls = {
        node.func.id
        for node in ast.walk(operator_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert calls.isdisjoint(forbidden_constructors)

    public_store_assignments = {
        target.attr
        for node in ast.walk(operator_tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and target.attr in {"events", "artifacts"}
        )
    }
    assert not public_store_assignments

    for path in (api_path, worker_path):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        reach_through = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr in {"events", "artifacts"}
            and isinstance(node.value, ast.Name)
            and node.value.id == "operator"
        ]
        assert not reach_through, path


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


def test_wp26_has_retired_all_legacy_package_debt() -> None:
    debt = _load_json(DEBT_PATH)

    assert debt == {
        "schema_version": 1,
        "legacy_roots": [],
        "allowed_import_violations": [],
    }


def test_relative_imports_are_resolved_before_dependency_rules() -> None:
    importer = "blackcell.adapters.repository.adapter"
    expectations = {
        "from ...context import signals": (
            "blackcell.context",
            "blackcell.context.signals",
        ),
        "from ... import context, models": (
            "blackcell",
            "blackcell.context",
            "blackcell.models",
        ),
        "from .helper import value": (
            "blackcell.adapters.repository.helper",
            "blackcell.adapters.repository.helper.value",
        ),
        "from blackcell import models": ("blackcell", "blackcell.models"),
    }

    for source, expected in expectations.items():
        node = ast.parse(source).body[0]
        assert isinstance(node, ast.ImportFrom)
        assert _resolved_imports_from(importer, node) == expected


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
            elif isinstance(node, ast.ImportFrom):
                edges.extend(
                    ImportEdge(importer, imported, path, node.lineno)
                    for imported in _resolved_imports_from(importer, node)
                )
    return tuple(edges)


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
