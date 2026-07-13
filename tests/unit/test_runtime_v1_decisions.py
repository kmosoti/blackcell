from __future__ import annotations

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
