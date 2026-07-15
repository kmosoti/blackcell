from __future__ import annotations

import ast
import json
from pathlib import Path

import blackcell.operator as operator_api
import blackcell.runtime as runtime_api
import blackcell.workflows as workflow_api
from blackcell.cli.app import app
from tests.cli_runner import CycloptsCliRunner

ROOT = Path(__file__).parents[2]
SOURCE = ROOT / "src" / "blackcell"
REMOVED_ROOTS = (
    "agents",
    "harness",
    "latent",
    "ledger",
    "nesy",
    "world",
)
REMOVED_IMPORTS = (
    *(f"blackcell.{name}" for name in REMOVED_ROOTS),
    "blackcell.operator.service",
    "blackcell.runtime.models",
    "blackcell.runtime.service",
    "blackcell.workflows.daily_operator",
    "blackcell.workflows.daily_operator_identity",
)
REMOVED_COMMANDS = (
    "adapters",
    "agents",
    "doctor",
    "harness",
    "latent",
    "ledger",
    "nesy",
    "world",
)

runner = CycloptsCliRunner()


def test_wp26_removes_prototype_packages_generated_coordination_and_dual_store_defaults() -> None:
    for root in REMOVED_ROOTS:
        assert not tuple((SOURCE / root).glob("*.py"))
    assert not tuple((ROOT / ".opencode" / "agents").glob("*.md"))
    assert not tuple((ROOT / ".opencode" / "commands").glob("*.md"))

    source = "\n".join(path.read_text(encoding="utf-8") for path in SOURCE.rglob("*.py"))
    assert ".blackcell/latent.sqlite3" not in source
    assert ".blackcell/ledger.sqlite3" not in source


def test_wp26_leaves_no_import_of_a_retired_package_or_coordinator() -> None:
    violations: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = (node.module,)
            for module in modules:
                if any(module == item or module.startswith(f"{item}.") for item in REMOVED_IMPORTS):
                    line_number = getattr(node, "lineno", 0)
                    violations.append(f"{path.relative_to(ROOT)}:{line_number}: {module}")

    assert violations == []


def test_wp26_removes_obsolete_commands_and_retains_the_canonical_cli() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0

    for command in REMOVED_COMMANDS:
        assert f"\n│ {command}" not in result.stdout

    for command in ("operator", "events", "bench"):
        assert f"\n│ {command}" in result.stdout


def test_wp26_removes_public_writers_but_keeps_quota_and_v2_product_contracts() -> None:
    assert not hasattr(operator_api, "LegacyRepositoryOperator")
    assert not hasattr(workflow_api, "DailyOperatorWorkflow")
    assert not hasattr(workflow_api, "DailyOperatorRequest")
    assert not hasattr(runtime_api, "list_runtime_adapters")
    assert hasattr(operator_api, "RepositoryOperator")
    assert hasattr(workflow_api, "DailyOperatorV2Workflow")
    assert hasattr(runtime_api, "RuntimeStorageQuota")


def test_wp26_architecture_debt_and_characterization_are_machine_readable() -> None:
    debt = json.loads((ROOT / "architecture/dependency_debt.json").read_text(encoding="utf-8"))
    characterization = json.loads(
        (ROOT / "experiments/legacy_retirement/wp26-characterization.json").read_text(
            encoding="utf-8"
        )
    )

    assert debt["legacy_roots"] == []
    assert debt["allowed_import_violations"] == []
    assert characterization["characterization_test"]["passed"] == 106
    assert characterization["replay_boundary"] == {
        "daily_operator_v1_history": "retain read-only decoding and verification",
        "daily_operator_v1_writer": "retire",
        "daily_operator_v2_execution_and_replay": "retain",
        "live_model_or_action_allowed_during_replay": False,
    }


def test_wp26_current_documentation_does_not_advertise_retired_commands() -> None:
    paths = (ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md")))
    documentation = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    for command in REMOVED_COMMANDS:
        assert f"blackcell {command}" not in documentation
