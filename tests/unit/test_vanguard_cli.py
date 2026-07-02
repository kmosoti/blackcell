import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from blackcell.cli.app import app
from blackcell.vanguard import DEFAULT_QA_COMMANDS

runner = CliRunner()


def test_vanguard_changespec_init_outputs_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_contract(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["vanguard", "changespec", "init", "--issue-key", "BCP-0006"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["issue_key"] == "BCP-0006"
    assert payload["intent"] == "Add Vanguard CLI scope"
    assert payload["verification"]["required"] == list(DEFAULT_QA_COMMANDS)
    assert payload["executor_scope"]["allowed_files"] == [
        "src/blackcell/vanguard/",
        "src/blackcell/cli/app.py",
        "tests/unit/test_vanguard_cli.py",
    ]


def test_vanguard_changespec_validate_success(tmp_path: Path) -> None:
    path = tmp_path / "changespec.json"
    path.write_text(json.dumps(_valid_changespec()), encoding="utf-8")

    result = runner.invoke(
        app,
        ["vanguard", "changespec", "validate", str(path)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["valid"] is True


def test_vanguard_changespec_validate_failure(tmp_path: Path) -> None:
    changespec = _valid_changespec()
    changespec["intent"] = ""
    path = tmp_path / "changespec.json"
    path.write_text(json.dumps(changespec), encoding="utf-8")

    result = runner.invoke(
        app,
        ["vanguard", "changespec", "validate", str(path)],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["valid"] is False
    assert payload["errors"][0]["code"] == "missing_intent"


def test_vanguard_qa_plan_outputs_deterministic_commands(tmp_path: Path) -> None:
    path = tmp_path / "changespec.json"
    path.write_text(json.dumps(_valid_changespec()), encoding="utf-8")

    result = runner.invoke(
        app,
        ["vanguard", "qa", "plan", str(path)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["issue_key"] == "BCP-0006"
    assert [command["command"] for command in payload["commands"]] == list(DEFAULT_QA_COMMANDS)
    assert all(not command["mutating"] for command in payload["commands"])


def test_vanguard_templates_render_outputs_deterministic_records() -> None:
    result = runner.invoke(
        app,
        ["vanguard", "templates", "render"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert [record["name"] for record in payload["templates"]] == [
        "evidence-draft",
        "qa-plan",
        "read-only-review",
    ]


def _write_contract(path: Path) -> None:
    (path / ".git").mkdir()
    (path / "blackcell.plan.yaml").write_text(_contract_yaml(), encoding="utf-8")


def _valid_changespec() -> dict[str, object]:
    return {
        "change_id": "BCP-0006",
        "issue_key": "BCP-0006",
        "intent": "Add Vanguard CLI scope",
        "non_goals": ["remote mutation"],
        "candidate_invariants": ["candidate evidence"],
        "behavior_contract": ["reviewed behavior"],
        "preserved_contracts": ["existing control-plane behavior"],
        "acceptance_criteria": ["commands emit JSON"],
        "verification": {
            "required": list(DEFAULT_QA_COMMANDS),
            "conditional": [],
        },
        "executor_scope": {
            "allowed_files": ["src/blackcell/vanguard/"],
            "forbidden": ["remote mutation"],
        },
        "escalation_rules": ["Ask before expanding scope"],
    }


def _contract_yaml() -> str:
    return """
version: 1
project:
  key: BCP
  name: BlackCell
global:
  acceptance_criteria:
    - global JSON output
  definition_of_done:
    - global done
roadmaps:
  - key: RM-1
    title: Roadmap
    epics:
      - EPIC-1
epics:
  - key: EPIC-1
    title: Epic
    roadmap: RM-1
    milestones:
      - MS-1
milestones:
  - key: MS-1
    title: Milestone
    epic: EPIC-1
issues:
  - key: BCP-0005
    title: Project fields
    type: feature
    status: Done
    priority: P0
    complexity: 5
    epic: EPIC-1
    milestone: MS-1
  - key: BCP-0006
    title: Add Vanguard CLI scope
    type: feature
    status: In Progress
    priority: P0
    complexity: 5
    epic: EPIC-1
    milestone: MS-1
    depends_on:
      - BCP-0005
    areas_of_responsibility:
      - src/blackcell/vanguard/
      - src/blackcell/cli/app.py
      - tests/unit/test_vanguard_cli.py
    context:
      - candidate evidence
    change_spec:
      - reviewed behavior
    acceptance_criteria:
      - local acceptance
    definition_of_done:
      - existing control-plane behavior
"""
