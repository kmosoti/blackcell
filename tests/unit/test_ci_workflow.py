from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOW_PATH = ROOT / ".github/workflows/ci.yml"
CHECKOUT_ACTION = "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
SETUP_UV_ACTION = "astral-sh/setup-uv@11f9893b081a58869d3b5fccaea48c9e9e46f990"
AST_GREP_ACTION = "ast-grep/action@d9518f632658f9c7c4b4dd4df22e98388a0d2c68"
AST_GREP_RELEASE = "0.45.0"
ARCHITECTURE_GATE = (
    "uv run python tools/run_pytest.py tests/architecture/test_dependencies.py "
    "-q --blackcell-require-all-pass"
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
STRUCTURAL_RULE_PROBES = """\
probe_rule() {
  expected_rule="$1"
  probe="$2"
  set +e
  output="$(
    printf '%s\\n' "$probe" \\
      | ast-grep scan \\
          --rule architecture/ast-grep/python-semantic-names.yml \\
          --stdin \\
          --json=compact \\
          2>/dev/null
  )"
  status=$?
  set -e
  if [ "$status" -ne 1 ] \\
    || ! grep -Fq "\\\"ruleId\\\":\\\"$expected_rule\\\"" <<<"$output"; then
    exit 1
  fi
}

probe_rule \\
  python-maturity-labelled-identifier \\
  "$(printf '%s%s_%s = 1' al pha review)"
probe_rule \\
  python-generation-labelled-identifier \\
  "$(printf 'class Engine%s%s: pass' V 3)"
probe_rule \\
  python-retired-source-evidence-label \\
  "$(printf 'marker = \\"%s_%s\\"' release evidence)"
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
    ast_grep_references = {
        reference for reference in action_references if reference.startswith("ast-grep/action@")
    }
    assert checkout_references == {CHECKOUT_ACTION}
    assert setup_uv_references == {SETUP_UV_ACTION}
    assert ast_grep_references == {AST_GREP_ACTION}
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference) for reference in action_references)

    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "actions/checkout@v" not in workflow_text
    assert "astral-sh/setup-uv@v" not in workflow_text
    assert workflow_text.count(f"{CHECKOUT_ACTION} # v7.0.0") == 2
    assert workflow_text.count(f"{SETUP_UV_ACTION} # v8.3.2") == 2


def test_quality_gate_uses_the_no_ignore_suite_without_release_history() -> None:
    steps = _quality_steps()
    checkout = steps[0]
    architecture = next(step for step in steps if step.get("name") == "Architecture fitness")
    full_suite = next(step for step in steps if step.get("name") == "Full test suite")

    assert checkout["uses"] == CHECKOUT_ACTION
    assert "with" not in checkout
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


def test_quality_gate_runs_pinned_rust_structural_naming_rules() -> None:
    naming = next(step for step in _quality_steps() if step.get("name") == "Semantic naming")

    assert naming == {
        "name": "Semantic naming",
        "uses": AST_GREP_ACTION,
        "with": {
            "version": AST_GREP_RELEASE,
            "config": "sgconfig.yml",
            "paths": "src tests tools examples .github/workflows/ci.yml",
        },
    }
    assert "if" not in naming
    assert "continue-on-error" not in naming

    probes = next(step for step in _quality_steps() if step.get("name") == "Structural rule probes")
    assert probes == {
        "name": "Structural rule probes",
        "shell": "bash",
        "run": STRUCTURAL_RULE_PROBES,
    }
    assert "continue-on-error" not in probes


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
    retired_tool_name = "release_" + "evidence.py"

    assert "architecture_consolidation_evidence" not in workflow_text
    assert "verify-current" not in workflow_text
    assert retired_tool_name not in workflow_text
    assert "Historical evidence boundary" not in workflow_text
    assert all(" generate " not in f" {step.get('run', '')} " for step in steps)
    assert all("continue-on-error" not in step for step in steps)
