from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_wp11_local_predictor_deferral_is_explicit_and_non_speculative() -> None:
    decision = json.loads(
        (ROOT / "docs/decisions/runtime-v1/wp11-local-predictor.json").read_text()
    )

    assert decision["schema_version"] == "runtime-v1-decision/v1"
    assert decision["work_package"] == "WP11"
    assert decision["decision"] == "defer"
    assert {item["id"]: item["status"] for item in decision["observations"]} == {
        "local-runtime": "absent",
        "matched-evaluation": "absent",
        "prediction-route": "absent",
    }
    assert len(decision["promotion_prerequisites"]) >= 5
    assert decision["repository_effect"] == {
        "adapter_added": False,
        "dependency_added": False,
        "default_changed": False,
    }


def test_wp12_clingo_promotion_is_bounded_by_parity_and_explicit_injection() -> None:
    decision = json.loads((ROOT / "docs/decisions/runtime-v1/wp12-clingo.json").read_text())

    assert decision["schema_version"] == "runtime-v1-decision/v1"
    assert decision["work_package"] == "WP12"
    assert decision["decision"] == "promote"
    assert decision["compatibility_probe"]["result"] == {
        "clingo": "5.8.0",
        "python": "3.14.6",
    }
    assert decision["promotion_boundary"] == {
        "adapter": "blackcell.adapters.reasoning.ClingoConstraintSolver",
        "dependency": "clingo>=5.8.0,<6",
        "explicit_injection_only": True,
        "feature_port": "blackcell.features.solve_constraints.ConstraintSolver",
        "locked_version": "5.8.0",
        "workflow_default": "DeterministicConstraintSolver",
    }
    assert decision["verification"]["status"] == "pass"
    assert "parity-drift-fail-closed" in decision["verification"]["policy_cases"]


def test_wp23a_fts5_promotion_is_ephemeral_matched_and_experiment_only() -> None:
    decision = json.loads((ROOT / "docs/decisions/runtime-v1/wp23a-fts5.json").read_text())

    assert decision["schema_version"] == "runtime-v1-decision/v1"
    assert decision["work_package"] == "WP23a"
    assert decision["decision"] == "promote"
    assert decision["compatibility_probe"]["result"] == {
        "fts5": True,
        "python": "3.14.6",
        "sqlite": "3.53.1",
    }
    assert decision["promotion_boundary"] == {
        "adapter": "blackcell.adapters.retrieval.Fts5EvidenceRetriever",
        "dependency": "Python standard-library sqlite3 with FTS5 enabled",
        "explicit_experiment_only": True,
        "feature_port": "blackcell.features.retrieve_evidence.EvidenceRetriever",
        "persistent_index": False,
        "workflow_default": "DeterministicEvidenceRetriever",
    }
    assert decision["matched_comparison"]["status"] == "pass"
    assert decision["verification"]["status"] == "pass"
    assert "foreign-identity-rejection" in decision["verification"]["policy_cases"]


def test_wp23_comparison_promotes_only_the_experiment_contract() -> None:
    decision = json.loads(
        (ROOT / "docs/decisions/runtime-v1/wp23-context-retrieval.json").read_text()
    )
    artifact = json.loads(
        (ROOT / decision["recorded_artifact"]["path"]).read_text(encoding="utf-8")
    )

    assert decision["schema_version"] == "runtime-v1-decision/v1"
    assert decision["work_package"] == "WP23"
    assert decision["decision"] == "revise"
    assert decision["recorded_artifact"]["report_id"] == artifact["report_id"]
    assert artifact["schema_version"] == "operator-bench-comparison/v1"
    assert artifact["scenario_count"] == 6
    assert len(artifact["trials"]) == 30
    assert len(artifact["ablations"]) == 15
    assert artifact["inferential"] is False
    assert decision["promotion_boundary"] == {
        "comparison_contract": "promoted",
        "recorded_fixture_artifact": "retained",
        "live_model_quality_claim": False,
        "retrieval_adapter_quality_promotion": False,
        "workflow_default": "DeterministicEvidenceRetriever",
        "default_changed": False,
    }
    assert decision["verification"]["status"] == "pass"


def test_wp24_prediction_experiment_continues_neural_and_nesy_deferral() -> None:
    decision = json.loads(
        (ROOT / "docs/decisions/runtime-v1/wp24-prediction-experiments.json").read_text()
    )
    artifact = json.loads(
        (ROOT / decision["recorded_artifact"]["path"]).read_text(encoding="utf-8")
    )

    assert decision["schema_version"] == "runtime-v1-decision/v1"
    assert decision["work_package"] == "WP24"
    assert decision["decision"] == "defer"
    assert decision["recorded_artifact"]["report_id"] == artifact["report_id"]
    assert artifact["schema_version"] == "prediction-bench-report/v1"
    assert artifact["scenario_count"] == 8
    assert len(artifact["trials"]) == 16
    assert artifact["inferential"] is False
    assert all(item["measurements"] is None for item in decision["candidate_availability"])
    assert decision["promotion_boundary"] == {
        "state_persistence_baseline": "retained",
        "declared_effect_predictor": "experiment-only",
        "local_neural_predictor": "deferred",
        "hybrid_neural_symbolic_predictor": "deferred",
        "wp11_deferral": "continues",
        "learned_world_model_claim": False,
        "neuro_symbolic_reasoning_system_claim": False,
        "workflow_default_changed": False,
    }
    assert decision["verification"]["status"] == "pass"


def test_wp25_runtime_benchmark_accepts_only_the_reproducible_baseline() -> None:
    decision = json.loads(
        (ROOT / "docs/decisions/runtime-v1/wp25-runtime-benchmark.json").read_text()
    )
    artifact = json.loads(
        (ROOT / decision["recorded_artifact"]["path"]).read_text(encoding="utf-8")
    )

    assert decision["schema_version"] == "runtime-v1-decision/v1"
    assert decision["work_package"] == "WP25"
    assert decision["decision"] == "accept"
    assert decision["recorded_artifact"]["report_id"] == artifact["report_id"]
    assert artifact["schema_version"] == "runtime-benchmark-report/v1"
    assert artifact["complete"] is True
    assert artifact["total_passed"] == 28
    assert artifact["total_skipped"] == 0
    assert [item["probe_id"] for item in artifact["probes"]] == [
        "api",
        "worker",
        "restart-fencing",
        "quota",
        "recovery",
        "rootless-container",
    ]
    assert all(item["status"] == "pass" for item in artifact["probes"])
    assert artifact["environment"]["podman_rootless"] is True
    assert decision["acceptance_boundary"] == {
        "reproducible_harness": "accepted",
        "recorded_single_host_baseline": "retained",
        "runtime_optimization_triggered": False,
        "service_slo_claim": False,
        "throughput_claim": False,
        "production_rto_rpo_claim": False,
        "runtime_default_changed": False,
    }
    assert decision["rootless_compatibility"]["compose_dependency_contract_retained"] is True
    assert decision["verification"]["status"] == "pass"


def test_wp26_retires_legacy_authority_and_preserves_read_only_history() -> None:
    decision = json.loads(
        (ROOT / "docs/decisions/runtime-v1/wp26-legacy-retirement.json").read_text()
    )
    characterization_path = ROOT / decision["evidence"]["characterization"]["path"]
    retirement_path = ROOT / decision["evidence"]["retirement"]["path"]
    characterization = json.loads(characterization_path.read_text(encoding="utf-8"))
    retirement = json.loads(retirement_path.read_text(encoding="utf-8"))

    assert decision["schema_version"] == "runtime-v1-decision/v1"
    assert decision["work_package"] == "WP26"
    assert decision["decision"] == "retire"
    assert characterization["characterization_test"]["passed"] == 106
    assert retirement["focused_verification"]["tests_passed"] == 118
    assert retirement["source_after_retirement"]["legacy_root_count"] == 0
    assert retirement["source_after_retirement"]["allowed_import_violation_count"] == 0

    for key, path in (
        ("characterization", characterization_path),
        ("retirement", retirement_path),
    ):
        digest = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
        assert decision["evidence"][key]["digest"] == digest

    assert decision["retained"]["canonical_cli_surfaces"] == ["bench", "events", "operator"]
    assert decision["retained"]["daily_operator_v1"] == (
        "read-only historical decoder and verifier"
    )
    assert decision["retained"]["daily_operator_v2"] == "canonical execution and replay"
    assert decision["retirement_boundary"] == {
        "legacy_root_count": 0,
        "allowed_import_violation_count": 0,
        "aliases_or_tombstone_commands_added": False,
        "tracked_or_external_user_data_migrated": False,
        "runtime_default_changed": False,
        "release_or_publication_performed": False,
    }
    assert decision["verification"]["full_gate"] == {
        "tests_passed": 1263,
        "tests_skipped": 1,
        "coverage_percent": 86.58,
        "ruff_format": "pass",
        "ruff_check": "pass",
        "ty": "pass",
    }
    assert decision["verification"]["status"] == "pass"


def test_wp27_accepts_reproducible_release_evidence_without_publication() -> None:
    decision = json.loads(
        (ROOT / "docs/decisions/runtime-v1/wp27-release-evidence.json").read_text()
    )
    manifest_path = ROOT / decision["evidence"]["verification_manifest"]["path"]
    sbom_path = ROOT / decision["evidence"]["sbom"]["path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))

    assert decision["schema_version"] == "runtime-v1-decision/v1"
    assert decision["work_package"] == "WP27"
    assert decision["decision"] == "accept"
    assert decision["release_candidate"]["status"] == "evidence-complete-unpublished"
    assert decision["release_candidate"]["candidate_id"] == manifest["release"]["candidate_id"]
    assert decision["evidence"]["sbom"]["digest"] == (
        f"sha256:{hashlib.sha256(sbom_path.read_bytes()).hexdigest()}"
    )
    assert decision["evidence"]["verification_manifest"]["digest"] == (
        f"sha256:{hashlib.sha256(manifest_path.read_bytes()).hexdigest()}"
    )
    assert sbom["specVersion"] == "1.7"
    assert decision["publication_boundary"] == {
        "package_built": False,
        "container_image_built": False,
        "tag_created": False,
        "release_created": False,
        "signed": False,
        "provenance_attestation_created": False,
        "vulnerability_scan_claimed": False,
        "commit_or_push_performed": False,
    }
    assert decision["verification"]["status"] == "pass"
