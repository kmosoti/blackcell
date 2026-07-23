from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[2]
PLAN_PATH = ROOT / "refactor-consolidation.plan.yaml"
BLACKCELL_PLAN_PATH = ROOT / "blackcell.plan.yaml"
REQUIRED_DIMENSIONS = {"strategic", "logistics", "human", "risk", "assessment"}
EXPECTED_WORK_PACKAGES = {f"AC0{number}" for number in range(8)}
EXPECTED_ISSUE_NUMBERS = {f"AC0{number}": number + 64 for number in range(8)}
DECISION_ROOT = ROOT / "docs/decisions/architecture-consolidation"
ARCHITECTURE_NODE_IDS = (
    "tests/architecture/test_dependencies.py::"
    "test_concrete_runtime_construction_stays_at_approved_sites",
    "tests/architecture/test_dependencies.py::"
    "test_production_runtime_does_not_import_compatibility_or_experiments",
    "tests/architecture/test_dependencies.py::"
    "test_repository_runtime_composition_is_owned_by_bootstrap",
    "tests/architecture/test_dependencies.py::"
    "test_replay_slice_cannot_reach_live_models_or_actions",
)
ARCHITECTURE_GATE = (
    "uv run python tools/run_pytest.py "
    + " ".join(ARCHITECTURE_NODE_IDS)
    + " -q --blackcell-require-all-pass"
)


def _plan() -> dict[str, Any]:
    value = yaml.safe_load(PLAN_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast("dict[str, Any]", value)


def _superseded_evidence() -> set[tuple[str, str, str]]:
    plan = _plan()
    implementation_base_sha = plan["program"]["implementation_base_sha"]
    superseded: set[tuple[str, str, str]] = set()
    for path in DECISION_ROOT.glob("ac0[1-7]-*.json"):
        decision = json.loads(path.read_text(encoding="utf-8"))
        entries = decision.get("superseded_baseline_evidence", ())
        if not entries:
            continue
        assert decision["schema_version"] == "architecture-consolidation-decision/v1"
        assert decision["work_package"] == path.name.split("-", 1)[0].upper()
        assert decision["decision"] == "accept"
        assert decision["base_sha"] == implementation_base_sha
        for item in entries:
            replacement = item["replacement"]
            replacement_path = ROOT / replacement["path"]
            assert replacement_path.is_file()
            assert replacement["symbol"] in replacement_path.read_text(encoding="utf-8")
            superseded.add((decision["work_package"], item["path"], item["symbol"]))
    return superseded


def _baseline_evidence_targets() -> dict[tuple[str, str], str]:
    baseline_path = DECISION_ROOT / "ac00-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    return {
        (evidence["path"], evidence["symbol"]): boundary["target_work_package"]
        for hypothesis in baseline["hypotheses"]
        for boundary in hypothesis["decisions"]
        for evidence in boundary["evidence"]
    }


def test_consolidation_plan_has_a_completed_program_contract() -> None:
    plan = _plan()
    program = plan["program"]

    assert plan["schema_version"] == "blackcell-refactor-consolidation-plan/v1"
    assert program["status"] == "merged-complete"
    assert program["branch"] == "refactor/consolidation"
    assert program["superseded_branch"] == "refactor/architecture-consolidation"
    assert program["base_ref"] == "origin/main"
    assert program["implementation_base_sha"] == ("1a249d8aaa1f5f230c8492ab249ea06d255f24ee")
    assert program["merge"] == {
        "pull_request": 72,
        "base_branch": "main",
        "merge_commit": "ff1e6be69da3135d35411ae10f63208da278c4bf",
        "merged_at": "2026-07-17T00:53:01Z",
    }
    assert "runtime-v1" in program["runtime_v1_context"]
    assert program["verification_policy"]["runtime_v1_evidence"] == "historical-read-only"
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
            "epic": "Done",
            "completed": {
                "AC00": "Done",
                "AC01": "Done",
                "AC02": "Done",
                "AC03": "Done",
                "AC04": "Done",
                "AC05": "Done",
                "AC06": "Done",
                "AC07": "Done",
            },
            "active": {},
            "queued": None,
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
        },
    }
    assert "implementation-complete-awaiting-merge" not in PLAN_PATH.read_text(encoding="utf-8")


def test_every_work_package_has_direct_evidence_and_three_scenarios() -> None:
    plan = _plan()
    packages = cast("list[dict[str, Any]]", plan["work_packages"])
    by_id = {package["id"]: package for package in packages}
    superseded = _superseded_evidence()
    baseline_targets = _baseline_evidence_targets()

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
            assert test_path.is_file(), evidence["test"]
            if path.is_file() and evidence["symbol"] in path.read_text(encoding="utf-8"):
                pass
            else:
                target = baseline_targets.get(
                    (evidence["path"], evidence["symbol"]),
                    package["id"],
                )
                assert (target, evidence["path"], evidence["symbol"]) in superseded
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
    for package_id in ("AC01", "AC02", "AC03", "AC04", "AC05", "AC06"):
        package = by_id[package_id]
        assert package["status"] == "accepted"
        assert package["accepted_on"].isoformat() == "2026-07-16"
        assert (ROOT / package["decision_artifact"]).is_file()
    assert set(by_id["AC07"]["depends_on"]) == {
        "AC01",
        "AC02",
        "AC03",
        "AC04",
        "AC05",
        "AC06",
    }
    assert by_id["AC07"]["status"] == "accepted"
    assert by_id["AC07"]["accepted_on"].isoformat() == "2026-07-17"
    assert by_id["AC07"]["decision_updated_on"].isoformat() == "2026-07-16"
    assert by_id["AC07"]["decision_artifact"] == (
        "docs/decisions/architecture-consolidation/ac07-architecture-fitness.json"
    )


def test_blackcell_plan_declares_the_project_program_and_historical_context() -> None:
    plan = yaml.safe_load(BLACKCELL_PLAN_PATH.read_text(encoding="utf-8"))
    assert isinstance(plan, dict)
    program = plan["architecture_consolidation"]

    assert plan["runtime_v1"]["status"] == "evidence-complete"
    assert program["status"] == "merged-complete"
    assert program["branch"] == "refactor/consolidation"
    assert program["superseded_branch"] == "refactor/architecture-consolidation"
    assert program["base_ref"] == "origin/main"
    assert program["implementation_base_sha"] == ("1a249d8aaa1f5f230c8492ab249ea06d255f24ee")
    assert program["plan"] == "refactor-consolidation.plan.yaml"
    assert program["merge"] == {
        "pull_request": 72,
        "base_branch": "main",
        "merge_commit": "ff1e6be69da3135d35411ae10f63208da278c4bf",
        "merged_at": "2026-07-17T00:53:01Z",
    }
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
        "epic": "Done",
        "completed": {
            "AC00": "Done",
            "AC01": "Done",
            "AC02": "Done",
            "AC03": "Done",
            "AC04": "Done",
            "AC05": "Done",
            "AC06": "Done",
            "AC07": "Done",
        },
        "active": {},
        "queued": None,
    }
    policy = program["verification_policy"]
    assert policy["runtime_v1_evidence"] == "historical-read-only"
    assert policy["baseline"] == ("docs/decisions/architecture-consolidation/ac00-baseline.json")
    assert policy["architecture_fitness_decision"] == (
        "docs/decisions/architecture-consolidation/ac07-architecture-fitness.json"
    )
    assert policy["source_bound_candidate"] == "retired"
    assert "every ordinary change" in policy["retirement_reason"]
    assert program["verification"]["architecture_fitness_gate"] == ARCHITECTURE_GATE
    assert program["verification"]["final_full_gate"] == (
        "uv run python tools/run_pytest.py --cov=blackcell --cov-report=term-missing"
    )
    assert "--ignore" not in program["verification"]["final_full_gate"]
    assert program["verification"]["historical_runtime_v1_gate"] == (
        "uv run python tools/run_pytest.py tests/unit/test_release_evidence.py -q"
    )
    assert (
        "uv run python tools/release_evidence.py verify --repo-root ." not in plan["verification"]
    )
    assert all(
        "architecture_consolidation_evidence" not in command for command in plan["verification"]
    )
    assert "implementation-complete-awaiting-merge" not in BLACKCELL_PLAN_PATH.read_text(
        encoding="utf-8"
    )
