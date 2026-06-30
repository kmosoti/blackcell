import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from blackcell.cli.app import app

runner = CliRunner()


def test_control_plane_validate_defaults_to_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_contract(tmp_path, _valid_contract())
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["control-plane", "validate"], catch_exceptions=False)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["errors"] == []


def test_control_plane_validate_invalid_contract_emits_json_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_contract(tmp_path, _valid_contract().replace("status: Backlog", "status: In Progress"))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["control-plane", "validate"], catch_exceptions=False)

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["valid"] is False
    assert payload["errors"][0]["code"] == "blocked_dependency"


def test_control_plane_agent_context_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_contract(tmp_path, _valid_contract())
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["--jsonl", "control-plane", "agent-context", "BCP-0002"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["key"] == "BCP-0002"
    assert payload["blocked_by"][0]["key"] == "BCP-0001"


def test_control_plane_schema_rich(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["--rich", "control-plane", "schema"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "Control Plane Schema" in result.stdout


def test_control_plane_schema_rich_after_control_plane_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["control-plane", "--rich", "schema"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "Control Plane Schema" in result.stdout


def test_control_plane_schema_format_rich_after_control_plane_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["control-plane", "--format", "rich", "schema"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Control Plane Schema" in result.stdout


def test_capabilities_check_accepts_manifest_path(tmp_path: Path) -> None:
    manifest = Path.cwd() / "generated" / "cache" / "github_graphql_capabilities.json"

    result = runner.invoke(
        app,
        ["control-plane", "capabilities", "check", "--manifest", str(manifest)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["valid"] is True


def test_capabilities_check_rich_after_capabilities_command() -> None:
    manifest = Path.cwd() / "generated" / "cache" / "github_graphql_capabilities.json"

    result = runner.invoke(
        app,
        [
            "control-plane",
            "capabilities",
            "--rich",
            "check",
            "--manifest",
            str(manifest),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "GitHub GraphQL Capabilities" in result.stdout


def test_control_plane_missing_contract_reports_json_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["control-plane", "validate"], catch_exceptions=False)

    assert result.exit_code == 1
    assert json.loads(result.stderr) == {"error": {"message": "missing blackcell.plan.yaml"}}


def _write_contract(path: Path, content: str) -> None:
    (path / ".git").mkdir()
    (path / "blackcell.plan.yaml").write_text(content, encoding="utf-8")


def _valid_contract() -> str:
    return """
version: 1
project:
  key: BCP
  name: BlackCell
global:
  acceptance_criteria:
    - global ac
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
  - key: BCP-0001
    title: First issue
    type: feature
    status: Todo
    priority: P0
    complexity: 5
    epic: EPIC-1
    milestone: MS-1
  - key: BCP-0002
    title: Second issue
    type: bug
    status: Backlog
    priority: P1
    complexity: 3
    epic: EPIC-1
    milestone: MS-1
    depends_on:
      - BCP-0001
"""
