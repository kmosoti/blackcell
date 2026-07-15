from pathlib import Path
from typing import TypedDict, cast

import yaml


class _RuntimePlanNode(TypedDict):
    id: str
    depends_on: list[str]
    deliverable: str
    acceptance_evidence: list[str]


PLAN_PATH = Path("blackcell.plan.yaml")
CHARTER_PATH = Path("docs/charter.md")
SPEC_PATH = Path("docs/spec/bcp-0034-evolutionary-runtime.md")
LEDGER_PATH = Path("docs/migration-ledger.md")

EXPECTED_DEPENDENCIES: dict[str, set[str]] = {}
EXPECTED_SATISFIED = {
    "WP09b",
    "WP10",
    "WP11-decision",
    "WP12",
    "WP17",
    "WP19-role-dag",
    "WP20",
    "WP22b",
    "WP23a",
    "WP23",
    "WP24",
    "WP25",
    "WP26",
    "WP27",
}


def test_remaining_runtime_v1_dag_is_complete_acyclic_and_selects_ready_work() -> None:
    plan = yaml.safe_load(PLAN_PATH.read_text(encoding="utf-8"))
    dag = plan["runtime_v1"]["remaining_dag"]
    nodes = cast("list[_RuntimePlanNode]", dag["nodes"])
    by_id = {node["id"]: node for node in nodes}

    assert len(by_id) == len(nodes)
    assert set(by_id) == set(EXPECTED_DEPENDENCIES)
    assert set(dag["satisfied_dependencies"]) == EXPECTED_SATISFIED
    assert {
        node_id: set(node["depends_on"]) for node_id, node in by_id.items()
    } == EXPECTED_DEPENDENCIES
    assert all(node["deliverable"] for node in nodes)
    assert all(node["acceptance_evidence"] for node in nodes)

    waves = _execution_waves(by_id, EXPECTED_SATISFIED)

    assert waves == []
    assert dag["selected_next"] is None
    assert plan["runtime_v1"]["status"] == "evidence-complete"
    assert plan["runtime_v1"]["release_evidence"] == {
        "status": "complete-unpublished",
        "config": "release/runtime-v1/release.toml",
        "guide": "docs/guides/runtime-v1-release.md",
        "examples": "examples/runtime-v1",
        "sbom": "release/runtime-v1/blackcell-runtime-v1.cdx.json",
        "verification_manifest": "release/runtime-v1/verification-manifest.json",
        "publication_performed": False,
    }


def test_remaining_runtime_v1_nodes_are_synchronized_across_program_docs() -> None:
    spec = SPEC_PATH.read_text(encoding="utf-8")
    ledger = LEDGER_PATH.read_text(encoding="utf-8")

    assert "| WP27 |" in spec
    assert "| WP27 |" in ledger
    assert "no bounded runtime-v1 DAG node remains" in spec
    assert "evidence-complete and unpublished" in ledger


def test_charter_separates_accepted_phase_1_from_the_active_runtime_program() -> None:
    charter = CHARTER_PATH.read_text(encoding="utf-8")

    assert "## Accepted Phase 1 product and research surface" in charter
    assert "Phase 1 product acceptance is complete" in charter
    assert "Runtime-v1 is now evidence-complete" in charter
    assert "No runtime-v1 DAG node remains" in charter
    assert "does not promote a neuro-symbolic-reasoning-system claim" in charter


def _execution_waves(
    nodes: dict[str, _RuntimePlanNode],
    satisfied: set[str],
) -> list[set[str]]:
    unresolved = set(nodes)
    resolved = set(satisfied)
    waves: list[set[str]] = []

    while unresolved:
        ready = {node_id for node_id in unresolved if set(nodes[node_id]["depends_on"]) <= resolved}
        assert ready, "remaining runtime-v1 DAG must be acyclic and fully resolvable"
        waves.append(ready)
        unresolved -= ready
        resolved |= ready

    return waves
