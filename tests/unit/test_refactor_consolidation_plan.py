from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[2]
PLAN_PATH = ROOT / "refactor-consolidation.plan.yaml"
BLACKCELL_PLAN_PATH = ROOT / "blackcell.plan.yaml"
REQUIRED_DIMENSIONS = {"strategic", "logistics", "human", "risk", "assessment"}
EXPECTED_WORK_PACKAGES = {f"AC0{number}" for number in range(8)}
EXPECTED_ISSUE_NUMBERS = {f"AC0{number}": number + 64 for number in range(8)}


def _plan() -> dict[str, Any]:
    value = yaml.safe_load(PLAN_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast("dict[str, Any]", value)


def test_consolidation_plan_has_a_closed_program_contract() -> None:
    plan = _plan()
    program = plan["program"]

    assert plan["schema_version"] == "blackcell-refactor-consolidation-plan/v1"
    assert program["status"] == "in-progress"
    assert program["branch"] == "refactor/consolidation"
    assert program["superseded_branch"] == "refactor/architecture-consolidation"
    assert program["base_ref"] == "origin/main"
    assert "runtime-v1" in program["runtime_v1_context"]
    assert program["evidence_transition"]["runtime_v1_manifest"] == "historical-read-only"
    assert set(plan["planning_dimensions"]) == REQUIRED_DIMENSIONS
    assert plan["issue_delivery"]["repository"] == "kmosoti/blackcell"
    assert plan["issue_delivery"]["parent"]["issue_number"] == 63
    delivery = plan["issue_delivery"]
    assert delivery["integration_branch"] == "refactor/consolidation"
    assert delivery["superseded_branch"] == "refactor/architecture-consolidation"
    assert delivery["materialization_tool"] == "local-gh-cli"
    assert delivery["assignee"] == "kmosoti"
    assert delivery["labels"] == {
        "default": ["enhancement"],
        "documentation": ["AC00", "AC06"],
    }
    assert delivery["project"] == {
        "title": "BlackCell",
        "status": {
            "epic": "In Progress",
            "completed": {"AC00": "Done"},
            "active": {},
            "queued": "Todo",
        },
        "type": "refactor",
    }
    assert delivery["relationships"] == {
        "kind": "github-native",
        "parent_issue": 63,
        "ordered_children": list(range(64, 72)),
        "blocked_by": {
            65: [64],
            66: [64],
            67: [64],
            68: [66, 67],
            69: [65, 67],
            70: [64],
            71: [65, 66, 67, 68, 69, 70],
            60: [65, 66, 67],
            61: [60],
        },
    }


def test_every_work_package_has_direct_evidence_and_three_scenarios() -> None:
    plan = _plan()
    packages = cast("list[dict[str, Any]]", plan["work_packages"])
    by_id = {package["id"]: package for package in packages}

    assert set(by_id) == EXPECTED_WORK_PACKAGES
    assert len(by_id) == len(packages)
    assert {package_id: package["issue_number"] for package_id, package in by_id.items()} == (
        EXPECTED_ISSUE_NUMBERS
    )
    for package in packages:
        assert package["title"]
        assert package["boundary"]
        assert package["desired_invariant"]
        assert package["acceptance"]
        assert package["stop_conditions"]
        assert set(package["scenarios"]) == {"best", "nominal", "failure"}
        assert package["direct_evidence"]
        for evidence in package["direct_evidence"]:
            path = ROOT / evidence["path"]
            test_path = ROOT / evidence["test"]
            assert path.is_file(), evidence["path"]
            assert test_path.is_file(), evidence["test"]
            assert evidence["symbol"] in path.read_text(encoding="utf-8")
            assert evidence["observation"]


def test_work_package_dependencies_are_acyclic_and_backward_mapped() -> None:
    packages = cast("list[dict[str, Any]]", _plan()["work_packages"])
    by_id = {package["id"]: package for package in packages}
    unresolved = set(by_id)
    resolved: set[str] = set()

    while unresolved:
        ready = {
            package_id
            for package_id in unresolved
            if set(by_id[package_id]["depends_on"]) <= resolved
        }
        assert ready, "architecture-consolidation dependencies must be acyclic"
        unresolved -= ready
        resolved |= ready

    assert by_id["AC00"]["depends_on"] == []
    assert by_id["AC00"]["status"] == "accepted"
    assert by_id["AC00"]["adr"] == "docs/adr/0008-architecture-consolidation.md"
    assert by_id["AC00"]["decision_artifact"] == (
        "docs/decisions/architecture-consolidation/ac00-baseline.json"
    )
    assert (ROOT / by_id["AC00"]["adr"]).is_file()
    assert (ROOT / by_id["AC00"]["decision_artifact"]).is_file()
    assert set(by_id["AC07"]["depends_on"]) == {
        "AC01",
        "AC02",
        "AC03",
        "AC04",
        "AC05",
        "AC06",
    }


def test_blackcell_plan_declares_the_project_program_and_historical_context() -> None:
    plan = yaml.safe_load(BLACKCELL_PLAN_PATH.read_text(encoding="utf-8"))
    assert isinstance(plan, dict)
    program = plan["architecture_consolidation"]

    assert plan["runtime_v1"]["status"] == "evidence-complete"
    assert program["status"] == "in-progress"
    assert program["branch"] == "refactor/consolidation"
    assert program["superseded_branch"] == "refactor/architecture-consolidation"
    assert program["base_ref"] == "origin/main"
    assert program["plan"] == "refactor-consolidation.plan.yaml"
    assert program["planning_model"] == [
        "strategic",
        "logistics",
        "human",
        "risk",
        "assessment",
    ]
    assert program["work_packages"] == [f"AC0{number}" for number in range(8)]
    assert program["delivery_metadata"]["development_branch"] == "refactor/consolidation"
    assert program["delivery_metadata"]["assignee"] == "kmosoti"
    assert program["delivery_metadata"]["labels"]["documentation"] == ["AC00", "AC06"]
    assert program["delivery_metadata"]["project"]["status"] == {
        "epic": "In Progress",
        "completed": {"AC00": "Done"},
        "active": {},
        "queued": "Todo",
    }
    assert program["evidence_transition"]["observed_status"] == (
        "expected-drift-after-program-registration"
    )
    assert program["evidence_transition"]["baseline"] == (
        "docs/decisions/architecture-consolidation/ac00-baseline.json"
    )
    candidate = program["evidence_transition"]["consolidation_candidate"]
    assert candidate == {
        "status": "candidate-scheme-ratified-not-issued",
        "issuer": "AC07",
        "manifest": "release/architecture-consolidation/verification-manifest.json",
        "candidate_id_format": "sha256:<canonical-source-materials-digest>",
        "sbom_policy": ("Regenerate only if the locked production dependency closure changes."),
    }
    assert (
        "--ignore=tests/unit/test_release_evidence.py"
        in program["verification"]["pre_ac00_full_gate"]
    )
    assert (
        program["verification"]["interim_full_gate"]
        == (program["verification"]["pre_ac00_full_gate"])
    )
