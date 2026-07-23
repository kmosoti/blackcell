from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[2]
PLAN_PATH = ROOT / "alpha.plan.yaml"
BLACKCELL_PLAN_PATH = ROOT / "blackcell.plan.yaml"

EXPECTED_DEPENDENCIES = {
    "A00": set(),
    "A01": {"A00"},
    "A02": {"A00"},
    "A03": {"A01", "A02"},
    "A04": {"A03"},
    "A05": {"A04"},
    "A06": {"A05"},
    "A07": {"A05"},
    "A08": {"A06", "A07"},
}


def _yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast("dict[str, Any]", value)


def test_alpha_program_is_the_single_active_delivery_authority() -> None:
    plan = _yaml(PLAN_PATH)
    program = plan["program"]
    delivery = plan["delivery"]

    assert plan["schema_version"] == "blackcell-alpha-plan/v1"
    assert program["status"] == "in-progress"
    assert program["branch"] == "feature/repository-review-alpha"
    assert program["active_work_package"] == "A08"
    assert delivery["active_programs"] == 1
    assert delivery["tracked_writer_limit"] == 1
    assert delivery["remote_mutations"] == [
        {
            "kind": "github-epic-dag-created",
            "epic": 91,
            "children": {
                "A00": 92,
                "A01": 93,
                "A02": 94,
                "A03": 95,
                "A04": 96,
                "A05": 97,
                "A06": 98,
                "A07": 99,
                "A08": 100,
            },
            "edge_count": 10,
            "project": {"owner": "kmosoti", "number": 7},
            "development_branch": None,
            "verified": True,
        },
        {
            "kind": "github-issues-closed",
            "reason": "not_planned",
            "issues": [
                42,
                43,
                44,
                45,
                50,
                51,
                52,
                54,
                55,
                56,
                57,
                58,
                59,
                60,
                61,
                *range(75, 91),
            ],
            "verified": True,
        },
    ]
    assert delivery["github_reconciliation"] == {
        "status": "complete",
        "superseded_epics": [44, 54, 75],
        "superseded_standalone_issues": [42, 43],
        "superseded_observability_issues": [45, 50, 51, 52],
        "superseded_scaffolding_issues": list(range(55, 62)),
        "superseded_greenfield_issues": list(range(76, 91)),
        "close_reason": "not_planned",
        "replacement_epic": 91,
        "replacement_issues": {
            "A00": 92,
            "A01": 93,
            "A02": 94,
            "A03": 95,
            "A04": 96,
            "A05": 97,
            "A06": 98,
            "A07": 99,
            "A08": 100,
        },
        "readback": {
            "child_order": list(range(92, 101)),
            "edge_count": 10,
            "topological_waves": [[92], [93, 94], [95], [96], [97], [98, 99], [100]],
            "project_fields": {
                "epic_status": "In Progress",
                "active_status": "In Progress",
                "queued_status": "Todo",
                "priority": "P0",
            },
            "linked_development_branches": [],
        },
    }
    assert (ROOT / "AGENTS.md").is_file()
    assert not (ROOT / ".agents").exists()
    assert not (ROOT / ".codex").exists()


def test_alpha_architecture_has_one_daemon_and_explicit_client_boundaries() -> None:
    architecture = _yaml(PLAN_PATH)["architecture"]
    daemon = architecture["daemon"]
    clients = architecture["clients"]
    kernform = architecture["project_configuration"]
    legacy = architecture["legacy_runtime"]

    assert architecture["authority"]["owner"] == "daemon"
    assert daemon["process_model"] == "foreground"
    assert daemon["supervision"] == {
        "strategy": "operating-system service manager",
        "linux_default": "systemd user service",
        "portable_fallback": "documented foreground process",
        "forbidden": [
            "double-fork daemonization",
            "custom PID-file authority",
            "a second scheduler embedded in a client",
        ],
    }
    assert clients["tui"]["framework"] == "PyRatatui 0.2"
    assert clients["tui"]["concurrency"] == (
        "native terminal rendering stays on the event loop; client calls use bounded async tasks"
    )
    assert clients["web"]["framework"] == "Litestar"
    assert clients["web"]["updates"] == (
        "channels and WebSockets consume the ordered daemon event stream"
    )
    assert kernform["supported_version"] == "0.1.0"
    assert kernform["wire_schema"] == "kernform.command/v1"
    assert kernform["transport"] == "argv-only subprocess"
    assert kernform["version_probe"] == ["kernform", "--agent", "--version"]
    assert kernform["command_prefix"] == ["kernform", "--agent", "--format", "json"]
    assert kernform["initial_commands"] == ["check", "init"]
    assert kernform["prohibited_initial_commands"] == ["inspect"]
    assert legacy["component"] == "DailyOperatorV2Workflow"
    assert legacy["disposition"] == "migration-and-replay-evidence-only"


def test_alpha_work_package_dag_is_acyclic_and_has_one_active_node() -> None:
    packages = cast("list[dict[str, Any]]", _yaml(PLAN_PATH)["work_packages"])
    by_id = {package["id"]: package for package in packages}

    assert set(by_id) == set(EXPECTED_DEPENDENCIES)
    assert len(by_id) == len(packages)
    assert {key: set(value["depends_on"]) for key, value in by_id.items()} == (
        EXPECTED_DEPENDENCIES
    )
    expected_statuses = {
        "A00": "completed",
        "A01": "completed",
        "A02": "completed",
        "A03": "completed",
        "A04": "completed",
        "A05": "completed",
        "A06": "completed",
        "A07": "completed",
        "A08": "active",
    }
    assert {key: value["status"] for key, value in by_id.items()} == expected_statuses
    assert [package["id"] for package in packages if package["status"] == "active"] == ["A08"]
    for package_id, status in expected_statuses.items():
        if status in {"active", "completed"}:
            assert all(
                expected_statuses[dependency] == "completed"
                for dependency in by_id[package_id]["depends_on"]
            )
    assert {package["id"]: package["issue_number"] for package in packages} == {
        f"A0{offset}": 92 + offset for offset in range(9)
    }

    unresolved = set(by_id)
    resolved: set[str] = set()
    waves: list[set[str]] = []
    while unresolved:
        ready = {
            package_id
            for package_id in unresolved
            if set(by_id[package_id]["depends_on"]) <= resolved
        }
        assert ready, "alpha work-package dependencies must be acyclic"
        waves.append(ready)
        unresolved -= ready
        resolved |= ready

    assert waves == [
        {"A00"},
        {"A01", "A02"},
        {"A03"},
        {"A04"},
        {"A05"},
        {"A06", "A07"},
        {"A08"},
    ]


def test_alpha_plan_enforces_the_fast_development_loop() -> None:
    plan = _yaml(PLAN_PATH)
    strategy = plan["test_strategy"]

    assert strategy["ordinary_iteration"] == [
        "run only exact affected pytest nodes with --blackcell-require-all-pass",
        "run Ruff check and format check only on changed Python paths",
        "run one pytest process at a time",
    ]
    assert strategy["milestone_gate"] == "uv run ruff check ."
    assert "complete pytest coverage gate" in strategy["ci_owned"]
    assert "release or publication" in strategy["local_full_suite_rule"]
    assert plan["verification"]["project_gate"] == "uv run ruff check ."
    assert "--cov" not in plan["verification"]["focused_gate"]


def test_canonical_files_reference_alpha_and_remove_stale_delivery_programs() -> None:
    blackcell = _yaml(BLACKCELL_PLAN_PATH)
    alpha = blackcell["alpha"]

    assert alpha["status"] == "in-progress"
    assert alpha["plan"] == PLAN_PATH.name
    assert alpha["active_work_package"] == "A08"
    assert alpha["legacy_runtime"] == {
        "component": "DailyOperatorV2Workflow",
        "disposition": "migration-and-replay-evidence-only",
        "new_alpha_invocation_allowed": False,
    }
    assert "scope_realignment" not in blackcell
    assert "product_proof" not in blackcell

    obsolete = (
        ROOT / "product-proof.plan.yaml",
        ROOT / "scope-realignment.plan.yaml",
        ROOT / "docs/product-proof",
        ROOT / "tests/unit/test_product_proof_plan.py",
        ROOT / "tests/unit/test_scope_realignment_plan.py",
    )
    assert all(not path.exists() for path in obsolete)

    canonical = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "README.md",
            "docs/charter.md",
            "docs/scope.md",
            "docs/architecture.md",
            "docs/index.md",
            "docs/atlas/decisions.md",
            "docs/adr/0009-project-runtime-scope.md",
        )
    )
    assert "alpha.plan.yaml" in canonical
    assert "product-proof.plan.yaml" not in canonical
    assert "scope-realignment.plan.yaml" not in canonical
    assert "PP00" not in canonical

    cli_source = (ROOT / "src/blackcell/cli/app.py").read_text(encoding="utf-8")
    client_source = (ROOT / "src/blackcell/adapters/runtime_http.py").read_text(encoding="utf-8")
    assert "def daemon_status(" in cli_source
    assert "def daemon_submit(" not in cli_source
    assert "def submit_run(" not in client_source
    assert '"/api/v1/runs"' not in client_source
