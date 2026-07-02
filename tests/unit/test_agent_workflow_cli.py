import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from blackcell.cli.app import app

runner = CliRunner()


def test_agent_workflow_validate_outputs_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_contract(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["control-plane", "agent-workflow", "validate"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["valid"] is True


def test_agent_workflow_install_dry_run_outputs_json_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_contract(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["control-plane", "agent-workflow", "install", "--target", "codex-cli"],
        catch_exceptions=False,
    )

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["target"] == "codex-cli"
    assert payload["dry_run"] is True
    assert payload["actions"][0]["action"] == "create"
    assert not (tmp_path / ".codex" / "config.toml").exists()


def test_agent_workflow_check_drift_exits_nonzero_when_artifacts_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_contract(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["control-plane", "agent-workflow", "check-drift", "--target", "codex-cli"],
        catch_exceptions=False,
    )

    payload = json.loads(result.stdout)
    assert result.exit_code == 1
    assert payload["drift"] is True
    assert payload["actions"][0]["action"] == "create"


def test_agent_workflow_install_conflict_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_contract(tmp_path)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("[agents]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["control-plane", "agent-workflow", "install", "--target", "codex-cli"],
        catch_exceptions=False,
    )

    payload = json.loads(result.stdout)
    assert result.exit_code == 1
    assert payload["conflicts"] is True
    assert payload["actions"][0]["action"] == "conflict"


def test_agent_workflow_validate_without_agent_workflow_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_contract(tmp_path, include_agent_workflow=False)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["control-plane", "agent-workflow", "validate"],
        catch_exceptions=False,
    )

    payload = json.loads(result.stdout)
    assert result.exit_code == 1
    assert payload["errors"][0]["code"] == "missing_agent_workflow"


def _write_contract(path: Path, *, include_agent_workflow: bool = True) -> None:
    (path / ".git").mkdir()
    content = _contract_yaml()
    if not include_agent_workflow:
        content = content.split("agent_workflow:")[0]
    (path / "blackcell.plan.yaml").write_text(content, encoding="utf-8")


def _contract_yaml() -> str:
    return """
version: 1
project:
  key: BCP
  name: BlackCell
issues:
  - key: BCP-0008
    title: Render Codex CLI agent workflow artifacts
    type: feature
    status: Todo
    priority: P0
    complexity: 5
agent_workflow:
  model: gpt-5.3-codex-spark
  workers:
    - key: agent-workflow
      name: Agent workflow worker
"""
