from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOW_PATH = ROOT / ".github/workflows/ci.yml"
CHECKOUT_ACTION = "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
SETUP_UV_ACTION = "astral-sh/setup-uv@11f9893b081a58869d3b5fccaea48c9e9e46f990"
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
BUBBLEWRAP_SETUP = """\
sudo apt-get update
sudo apt-get install --yes --no-install-recommends apparmor-profiles bubblewrap
sudo apparmor_parser --replace /usr/share/apparmor/extra-profiles/bwrap-userns-restrict
/usr/bin/bwrap \\
  --unshare-all \\
  --die-with-parent \\
  --ro-bind / / \\
  --proc /proc \\
  --dev /dev \\
  /usr/bin/true
"""


def _jobs() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return cast("dict[str, Any]", workflow["jobs"])


def _job_steps(job_name: str) -> list[dict[str, Any]]:
    job = cast("dict[str, Any]", _jobs()[job_name])
    return cast("list[dict[str, Any]]", job["steps"])


def _quality_steps() -> list[dict[str, Any]]:
    return _job_steps("quality")


def test_ci_actions_are_pinned_to_immutable_node24_release_commits() -> None:
    action_references = [
        cast("str", step["uses"])
        for job_name in _jobs()
        for step in _job_steps(job_name)
        if "uses" in step
    ]

    checkout_references = {
        reference for reference in action_references if reference.startswith("actions/checkout@")
    }
    setup_uv_references = {
        reference for reference in action_references if reference.startswith("astral-sh/setup-uv@")
    }
    assert checkout_references == {CHECKOUT_ACTION}
    assert setup_uv_references == {SETUP_UV_ACTION}
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference) for reference in action_references)

    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "actions/checkout@v" not in workflow_text
    assert "astral-sh/setup-uv@v" not in workflow_text
    assert workflow_text.count(f"{CHECKOUT_ACTION} # v7.0.0") == 2
    assert workflow_text.count(f"{SETUP_UV_ACTION} # v8.3.2") == 2


def test_quality_gate_uses_full_history_and_the_no_ignore_suite() -> None:
    steps = _quality_steps()
    checkout = steps[0]
    architecture = next(step for step in steps if step.get("name") == "Architecture fitness")
    full_suite = next(step for step in steps if step.get("name") == "Full test suite")

    assert checkout["uses"] == CHECKOUT_ACTION
    assert checkout["with"]["fetch-depth"] == 0
    assert architecture == {
        "name": "Architecture fitness",
        "run": ARCHITECTURE_GATE,
    }
    assert full_suite["run"] == (
        "uv run python tools/run_pytest.py --cov=blackcell --cov-report=term-missing"
    )
    assert "if" not in full_suite
    assert "continue-on-error" not in full_suite
    assert "--ignore" not in full_suite["run"]


def test_quality_runner_configures_bubblewrap_without_disabling_userns_policy() -> None:
    setup = next(
        step for step in _quality_steps() if step.get("name") == "Configure Bubblewrap sandbox"
    )

    assert setup == {
        "name": "Configure Bubblewrap sandbox",
        "run": BUBBLEWRAP_SETUP,
    }
    assert "apparmor-profiles bubblewrap" in BUBBLEWRAP_SETUP
    assert "bwrap-userns-restrict" in BUBBLEWRAP_SETUP
    assert "/usr/bin/bwrap" in BUBBLEWRAP_SETUP

    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "apparmor_restrict_unprivileged_userns=0" not in workflow_text
    assert "kernel.unprivileged_userns_clone=1" not in workflow_text


def test_ci_has_no_source_bound_evidence_gate_or_bypass() -> None:
    steps = [step for job_name in _jobs() for step in _job_steps(job_name)]
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "architecture_consolidation_evidence" not in workflow_text
    assert "verify-current" not in workflow_text
    assert all(" generate " not in f" {step.get('run', '')} " for step in steps)
    assert all("continue-on-error" not in step for step in steps)
