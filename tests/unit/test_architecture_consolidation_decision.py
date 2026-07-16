from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from pathlib import Path
from typing import Any, cast

import yaml

from blackcell.context.signals import SignalPacket as LegacySignalPacket
from blackcell.control.models import ActionProposal
from blackcell.features.derive_signal_packet.models import SignalPacket as CurrentSignalPacket
from blackcell.features.request_decision.models import (
    DecisionBudget,
    DecisionProposal,
    DecisionRoute,
)
from blackcell.gateway.models import GatewayBudget, RoutingDecision

ROOT = Path(__file__).parents[2]
DECISION_PATH = ROOT / "docs/decisions/architecture-consolidation/ac00-baseline.json"
FITNESS_DECISION_PATH = (
    ROOT / "docs/decisions/architecture-consolidation/ac07-architecture-fitness.json"
)
ADR_PATH = ROOT / "docs/adr/0008-architecture-consolidation.md"
CONSOLIDATION_PLAN_PATH = ROOT / "refactor-consolidation.plan.yaml"
ALLOWED_BOUNDARY_DECISIONS = {"retain", "consolidate", "defer", "reject"}
EXPECTED_HYPOTHESES = {f"H{number}" for number in range(1, 8)}
EXPECTED_REQUIRED_RULES = {
    "construction-confined-to-approved-owners": (
        "tests/architecture/test_dependencies.py::"
        "test_concrete_runtime_construction_stays_at_approved_sites"
    ),
    "production-isolated-from-compatibility-and-experiments": (
        "tests/architecture/test_dependencies.py::"
        "test_production_runtime_does_not_import_compatibility_or_experiments"
    ),
    "operator-store-non-reach-through": (
        "tests/architecture/test_dependencies.py::"
        "test_repository_runtime_composition_is_owned_by_bootstrap"
    ),
    "replay-isolated-from-live-paths": (
        "tests/architecture/test_dependencies.py::"
        "test_replay_slice_cannot_reach_live_models_or_actions"
    ),
}
EXPECTED_INVENTORIES = {
    "construction_sites",
    "service_lifecycles",
    "structural_protocols",
    "record_shape_clusters",
    "compatibility_paths",
    "protocol_hotspots",
    "sqlite_commitments",
    "public_schema_owners",
    "import_breadth",
    "constructor_fan_in",
    "co_change",
}
HISTORICAL_RUNTIME_V1_DIGESTS = {
    "docs/decisions/runtime-v1/wp11-local-predictor.json": (
        "c3b286b662b7fdc13cc129457773421a4f8a7b6693712fca529072ffab603216"
    ),
    "docs/decisions/runtime-v1/wp12-clingo.json": (
        "b14f1caad49678fc965f045f943df25eba0ce7bae26627556ac9880226c4735f"
    ),
    "docs/decisions/runtime-v1/wp23-context-retrieval.json": (
        "12f6b7b09a532bfac60f451e7fb9eeaaf6349fcfd4985228bb59e531cf1e7e37"
    ),
    "docs/decisions/runtime-v1/wp23a-fts5.json": (
        "2793877d7a2160c87da3098eecdc7afcde4ec704945a518db50831609514d02f"
    ),
    "docs/decisions/runtime-v1/wp24-prediction-experiments.json": (
        "8f8ea4e3f37fe09f1efeee74db979d2a17a900c10230de0eb5829f57bc3da2ef"
    ),
    "docs/decisions/runtime-v1/wp25-runtime-benchmark.json": (
        "f25f71a3128373fa591132a5e25a814832cedf5b81c858f96decdd4a32662de9"
    ),
    "docs/decisions/runtime-v1/wp26-legacy-retirement.json": (
        "49bc5693f57d72da260cb7503d3af1fcb83095ab96a87ae019c9688df3d41f23"
    ),
    "docs/decisions/runtime-v1/wp27-release-evidence.json": (
        "e4de8dce493a3563a2b8f53de081fcbebe38e27f21be1c40c1b6f8d1d9bb182c"
    ),
    "release/runtime-v1/README.md": (
        "353d43912135e9fbd15d3c53bbdbff4c4d35831b9756e2343015884f6523d490"
    ),
    "release/runtime-v1/verification-manifest.json": (
        "3439933f35c28cacf9d9fe86f7db6b1d97e68bb0d47249452a788030baeeb68b"
    ),
    "release/runtime-v1/blackcell-runtime-v1.cdx.json": (
        "e5fbdc1ee88f37a0ee1f63f67948c590e62ca132c27ad61ca266bfc7d121bb02"
    ),
    "release/runtime-v1/release.toml": (
        "bde90f2e4e0d760999d7b93a240123d1f7b30d8cc6a70a34f067acac75eac743"
    ),
}
RECORD_TYPES: dict[str, type[Any]] = {
    "GatewayBudget": GatewayBudget,
    "DecisionBudget": DecisionBudget,
    "RoutingDecision": RoutingDecision,
    "DecisionRoute": DecisionRoute,
    "DecisionProposal": DecisionProposal,
    "ActionProposal": ActionProposal,
    "blackcell.context.SignalPacket": LegacySignalPacket,
    "blackcell.features.derive_signal_packet.SignalPacket": CurrentSignalPacket,
}


def _decision() -> dict[str, Any]:
    value = json.loads(DECISION_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast("dict[str, Any]", value)


def _accepted_superseded_evidence() -> set[tuple[str, str, str]]:
    plan = yaml.safe_load(CONSOLIDATION_PLAN_PATH.read_text(encoding="utf-8"))
    implementation_base_sha = plan["program"]["implementation_base_sha"]
    superseded: set[tuple[str, str, str]] = set()
    for path in DECISION_PATH.parent.glob("ac0[1-7]-*.json"):
        decision = json.loads(path.read_text(encoding="utf-8"))
        entries = decision.get("superseded_baseline_evidence", ())
        if not entries:
            continue
        expected_work_package = path.name.split("-", 1)[0].upper()
        assert decision["schema_version"] == "architecture-consolidation-decision/v1"
        assert decision["work_package"] == expected_work_package
        assert decision["decision"] == "accept"
        assert decision["base_sha"] == implementation_base_sha
        for entry in entries:
            assert set(entry) == {"path", "symbol", "replacement"}
            replacement = entry["replacement"]
            assert set(replacement) == {"path", "symbol"}
            replacement_path = ROOT / replacement["path"]
            assert replacement_path.is_file(), replacement
            assert replacement["symbol"] in replacement_path.read_text(encoding="utf-8")
            superseded.add((decision["work_package"], entry["path"], entry["symbol"]))
    return superseded


def test_ac00_is_source_bound_and_changes_no_runtime_contract() -> None:
    decision = _decision()

    assert decision["schema_version"] == "architecture-consolidation-decision/v1"
    assert decision["work_package"] == "AC00"
    assert decision["decision"] == "accept"
    assert decision["source"] == {
        "branch": "refactor/consolidation",
        "base_sha": "eb05e034f3e6bdcef83167df6dcfdc8a1eaf06f0",
        "runtime_behavior_changed": False,
        "persisted_schema_changed": False,
        "dependency_closure_changed": False,
        "historical_runtime_v1_evidence_changed": False,
    }
    assert len(decision["boundary_criteria"]) == 8
    assert len(decision["preserved_invariants"]) >= 10


def test_every_hypothesis_and_boundary_has_a_bounded_evidenced_decision() -> None:
    hypotheses = cast("list[dict[str, Any]]", _decision()["hypotheses"])
    by_id = {item["id"]: item for item in hypotheses}
    superseded = _accepted_superseded_evidence()

    assert set(by_id) == EXPECTED_HYPOTHESES
    assert all(item["classification"] == "confirmed" for item in hypotheses)
    assert all(item["decisions"] for item in hypotheses)

    seen_decisions: set[str] = set()
    for hypothesis in hypotheses:
        for boundary in hypothesis["decisions"]:
            seen_decisions.add(boundary["decision"])
            assert boundary["decision"] in ALLOWED_BOUNDARY_DECISIONS
            assert boundary["target_work_package"] in {f"AC0{number}" for number in range(1, 8)}
            assert boundary["reason"]
            assert boundary["preserved_invariants"]
            assert boundary["evidence"]
            for evidence in boundary["evidence"]:
                source = ROOT / evidence["path"]
                test = ROOT / evidence["test"]
                assert test.is_file(), evidence
                if source.is_file() and evidence["symbol"] in source.read_text(encoding="utf-8"):
                    continue
                assert (
                    boundary["target_work_package"],
                    evidence["path"],
                    evidence["symbol"],
                ) in superseded, evidence

    assert seen_decisions == ALLOWED_BOUNDARY_DECISIONS


def test_baseline_covers_required_architecture_dimensions_without_metric_gates() -> None:
    decision = _decision()
    inventory = decision["inventory"]
    fitness = decision["fitness_policy"]

    assert set(inventory) == EXPECTED_INVENTORIES
    assert inventory["construction_sites"]
    assert inventory["service_lifecycles"]
    protocols = inventory["structural_protocols"]
    assert protocols["selection"].startswith("all Protocol classes")
    baseline_protocols = {
        (item["path"], item["symbol"]): (
            item["bases"],
            item["declared_members"],
            item["effective_members"],
        )
        for item in protocols["entries"]
    }
    assert len(baseline_protocols) == len(protocols["entries"])
    assert (
        "src/blackcell/features/build_context/ports.py",
        "EvidenceSelectionLike",
    ) in baseline_protocols
    assert max(item["effective_members"] for item in protocols["entries"]) == 22

    for cluster in inventory["record_shape_clusters"]:
        actual_fields = {
            record: [field.name for field in fields(RECORD_TYPES[record])]
            for record in cluster["records"]
        }
        assert cluster["fields_by_record"] == actual_fields
        field_sets = [set(names) for names in actual_fields.values()]
        shared = set.intersection(*field_sets)
        distinct = set.union(*field_sets) - shared
        assert set(cluster["shared_fields"]) == shared
        assert set(cluster["distinct_fields"]) == distinct

    assert inventory["import_breadth"]["threshold"] is None
    assert inventory["constructor_fan_in"]["threshold"] is None
    assert inventory["co_change"]["source_commits"] == 110
    assert (
        "results are advisory and have no pass threshold" in inventory["co_change"]["limitations"]
    )
    assert set(fitness["rejected_hard_metrics"]) == {
        "class count",
        "line count",
        "module count",
        "import count",
        "similarity threshold",
    }
    assert "live-free replay isolation" in fitness["hard_rules"]
    assert "package co-change frequency" in fitness["advisory_measurements"]


def test_public_schema_owners_are_explicit_and_source_checked() -> None:
    owners = _decision()["inventory"]["public_schema_owners"]

    assert len(owners) >= 10
    for owner in owners:
        source = ROOT / owner["path"]
        test = ROOT / owner["test"]
        assert source.is_file(), owner
        assert test.is_file(), owner
        assert owner["symbol"] in source.read_text(encoding="utf-8"), owner


def test_runtime_v1_evidence_is_historical_and_byte_stable() -> None:
    policy = _decision()["evidence_policy"]

    assert policy["runtime_v1"]["status"] == "historical-read-only"
    assert policy["runtime_v1"]["forbidden_reuse"] == (
        "verification claim for architecture-consolidation source"
    )
    actual_paths = {
        path.relative_to(ROOT).as_posix()
        for root in (ROOT / "docs/decisions/runtime-v1", ROOT / "release/runtime-v1")
        for path in root.rglob("*")
        if path.is_file()
    }
    assert actual_paths == set(HISTORICAL_RUNTIME_V1_DIGESTS)
    for relative_path, expected_digest in HISTORICAL_RUNTIME_V1_DIGESTS.items():
        payload = (ROOT / relative_path).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == expected_digest


def test_ac07_retires_source_bound_candidates_without_weakening_fitness() -> None:
    decision = json.loads(FITNESS_DECISION_PATH.read_text(encoding="utf-8"))

    assert decision["schema_version"] == "architecture-consolidation-fitness-decision/v1"
    assert decision["work_package"] == "AC07"
    assert decision["decision"] == "accept"
    assert decision["source_bound_candidate"]["retired"] is True
    advisory = decision["advisory"]
    assert advisory["thresholds"] is None
    assert advisory["policy"] == "All six measurements are advisory; no value is a CI threshold."
    assert advisory["provenance"] == {
        "evidence_class": "embedded-static-observation",
        "reproducibility": (
            "Not asserted after retirement; remeasure the named methods against the then-current "
            "tree when a future decision needs current values."
        ),
        "retention": (
            "Methods, observations, and conclusions are retained directly in this decision; "
            "branch-only commit and deleted-manifest references are intentionally omitted."
        ),
    }
    assert set(advisory["measurements"]) == {
        "constructor_fan_in",
        "import_breadth",
        "module_size",
        "package_co_change",
        "protocol_breadth",
        "record_similarity",
    }
    for measurement in advisory["measurements"].values():
        assert measurement["method"]
        assert measurement["conclusion"]
    assert advisory["measurements"]["module_size"]["before"]["run_records_v2_lines"] == 2690
    assert advisory["measurements"]["module_size"]["after"]["run_records_v2_lines"] == 2019
    assert advisory["measurements"]["protocol_breadth"]["before"]["maximum_effective_members"] == 22
    assert advisory["measurements"]["protocol_breadth"]["after"]["maximum_effective_members"] == 16
    assert (
        "architecture-fitness-no-skip-or-xfail" in decision["verification_policy"]["required_gates"]
    )

    architecture_tests = (ROOT / decision["architecture_fitness"]["binary_gate"]).read_text(
        encoding="utf-8"
    )
    required_rules = decision["architecture_fitness"]["required_rules"]
    assert {rule["id"]: rule["node_id"] for rule in required_rules} == EXPECTED_REQUIRED_RULES
    for rule in required_rules:
        assert rule["node_id"].endswith(f"::{rule['test']}")
        assert f"def {rule['test']}" in architecture_tests

    for relative_path in decision["source_bound_candidate"]["retired_paths"]:
        assert not (ROOT / relative_path).exists(), relative_path

    plan = cast(
        "dict[str, Any]",
        yaml.safe_load((ROOT / "refactor-consolidation.plan.yaml").read_text(encoding="utf-8")),
    )
    ac07 = next(item for item in plan["work_packages"] if item["id"] == "AC07")
    assert ac07["decision_artifact"] == FITNESS_DECISION_PATH.relative_to(ROOT).as_posix()
    assert set(ac07["retired_paths"]) == set(decision["source_bound_candidate"]["retired_paths"])


def test_adr_and_documentation_ratify_the_same_policy() -> None:
    adr = ADR_PATH.read_text(encoding="utf-8")
    architecture = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    index = (ROOT / "docs/index.md").read_text(encoding="utf-8")
    decision_log = (ROOT / "docs/atlas/decisions.md").read_text(encoding="utf-8")

    assert "Status: accepted" in adr
    assert "Boundary-earning criteria" in adr
    assert "The runtime-v1 evidence bundle is historical and read-only." in adr
    assert "source-bound candidate issuance retired" in adr
    assert "complete maintained quality gate" in architecture
    assert "adr/0008-architecture-consolidation" in architecture
    assert "adr/0008-architecture-consolidation.md" in index
    assert "adr/0008-architecture-consolidation" in decision_log
