from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOW_PATH = ROOT / ".github/workflows/ci.yml"
CHECKOUT_ACTION = "actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10"
SETUP_UV_ACTION = "astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b"


def _job_steps(job_name: str) -> list[dict[str, Any]]:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    jobs = cast("dict[str, Any]", workflow["jobs"])
    job = cast("dict[str, Any]", jobs[job_name])
    return cast("list[dict[str, Any]]", job["steps"])


def _quality_steps() -> list[dict[str, Any]]:
    return _job_steps("quality")


def test_ci_actions_are_pinned_to_immutable_node24_release_commits() -> None:
    for job_name in ("quality", "types"):
        actions = [step["uses"] for step in _job_steps(job_name) if "uses" in step]
        assert actions == [CHECKOUT_ACTION, SETUP_UV_ACTION]

    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "actions/checkout@v" not in workflow_text
    assert "astral-sh/setup-uv@v" not in workflow_text


def test_quality_gate_uses_full_history_and_the_no_ignore_suite() -> None:
    steps = _quality_steps()
    checkout = steps[0]
    full_suite = next(step for step in steps if step.get("name") == "Full test suite")

    assert checkout["uses"] == CHECKOUT_ACTION
    assert checkout["with"]["fetch-depth"] == 0
    assert full_suite["run"] == (
        "uv run python tools/run_pytest.py --cov=blackcell --cov-report=term-missing"
    )
    assert "if" not in full_suite
    assert "continue-on-error" not in full_suite
    assert "--ignore" not in full_suite["run"]


def test_quality_gate_replays_evidence_and_scopes_current_candidate_freshness() -> None:
    steps = _quality_steps()
    replay = next(
        step
        for step in steps
        if step.get("name") == "Verify architecture-consolidation evidence replay"
    )
    freshness = next(
        step
        for step in steps
        if step.get("name") == "Verify current architecture-consolidation candidate"
    )

    assert replay == {
        "name": "Verify architecture-consolidation evidence replay",
        "run": "uv run python tools/architecture_consolidation_evidence.py verify --repo-root .",
    }
    assert freshness == {
        "name": "Verify current architecture-consolidation candidate",
        "if": "github.event_name == 'pull_request' && github.head_ref == 'refactor/consolidation'",
        "run": (
            "uv run python tools/architecture_consolidation_evidence.py "
            "verify-current --repo-root ."
        ),
    }
    assert all(" generate " not in f" {step.get('run', '')} " for step in steps)
    assert all("continue-on-error" not in step for step in steps)
