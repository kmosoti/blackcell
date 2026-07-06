from pathlib import Path

import pytest

from blackcell.agents import (
    ConfigScope,
    check_opencode_agent_pack_drift,
    doctor_opencode_agent_pack,
    install_opencode_agent_pack,
    render_opencode_artifacts,
    resolve_opencode_config_root,
)
from blackcell.agents.opencode import MARKDOWN_START_PREFIX
from blackcell.agents.registry import blackcell_agent_commands, blackcell_agents


def test_opencode_artifacts_render_markdown_frontmatter() -> None:
    artifacts = render_opencode_artifacts(scope=ConfigScope.PROJECT)
    by_path = {artifact.path: artifact for artifact in artifacts}

    spore = by_path[".opencode/agents/blackcell-spore.md"]
    observe = by_path[".opencode/commands/blackcell-observe.md"]

    assert len(artifacts) == 10
    assert spore.content.startswith("---\n")
    assert "permission:" in spore.content
    assert "tools:" not in spore.content
    assert "edit: deny" in spore.content
    assert MARKDOWN_START_PREFIX in spore.content
    assert "agent: blackcell-spore" in observe.content


def test_agent_prompts_keep_world_model_protocol_sections() -> None:
    required = (
        "# Role",
        "# Operating Model",
        "# Inputs",
        "# Workflow",
        "# Evidence Rules",
        "# Constraint Rules",
        "# Handoff Protocol",
        "# Output Format",
        "# Stop Conditions",
        "# Failure Handling",
    )

    for agent in blackcell_agents():
        for section in required:
            assert section in agent.prompt, f"{agent.key} missing {section}"


def test_command_prompts_keep_structured_workflow_sections() -> None:
    required = (
        "# Workflow",
        "# Evidence Rules",
        "# Output Format",
        "# Verification",
        "# Risks",
        "# Stop Conditions",
    )

    for command in blackcell_agent_commands():
        for section in required:
            assert section in command.template, f"{command.key} missing {section}"


def test_opencode_global_scope_uses_user_config_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    root = resolve_opencode_config_root(scope=ConfigScope.GLOBAL)
    artifacts = render_opencode_artifacts(scope=ConfigScope.GLOBAL)

    assert root == home / ".config" / "opencode"
    assert artifacts[0].path.startswith("~/.config/opencode/agents/")


def test_opencode_install_dry_run_and_apply(tmp_path: Path) -> None:
    _write_repo(tmp_path)

    dry_run = install_opencode_agent_pack(start=tmp_path)
    assert not (tmp_path / ".opencode").exists()

    applied = install_opencode_agent_pack(start=tmp_path, apply_changes=True)
    second = install_opencode_agent_pack(start=tmp_path, apply_changes=True)

    assert dry_run.dry_run is True
    assert {action.action for action in dry_run.actions} == {"create"}
    assert dry_run.config_root == tmp_path / ".opencode"
    assert applied.drift is False
    assert all(action.applied for action in applied.actions)
    assert (tmp_path / ".opencode" / "agents" / "blackcell-astrophage.md").exists()
    assert {action.action for action in second.actions} == {"noop"}


def test_opencode_install_conflicts_on_unmanaged_artifact(tmp_path: Path) -> None:
    _write_repo(tmp_path)
    agent_path = tmp_path / ".opencode" / "agents" / "blackcell-spore.md"
    agent_path.parent.mkdir(parents=True)
    agent_path.write_text("---\ndescription: human\n---\n", encoding="utf-8")

    result = install_opencode_agent_pack(start=tmp_path)
    action_by_path = {action.path: action for action in result.actions}

    assert result.conflicts is True
    assert action_by_path[".opencode/agents/blackcell-spore.md"].action == "conflict"
    assert action_by_path[".opencode/agents/blackcell-spore.md"].current.managed is False


def test_opencode_check_drift_detects_managed_change(tmp_path: Path) -> None:
    _write_repo(tmp_path)
    install_opencode_agent_pack(start=tmp_path, apply_changes=True)
    agent_path = tmp_path / ".opencode" / "agents" / "blackcell-spore.md"
    agent_path.write_text(
        agent_path.read_text(encoding="utf-8").replace(
            "Observe the repository without changing it.",
            "Observe the repository quietly.",
        ),
        encoding="utf-8",
    )

    result = check_opencode_agent_pack_drift(start=tmp_path)
    action_by_path = {action.path: action for action in result.actions}

    assert result.drift is True
    assert action_by_path[".opencode/agents/blackcell-spore.md"].action == "update"
    assert action_by_path[".opencode/agents/blackcell-spore.md"].current.managed is True


def test_opencode_doctor_reports_missing_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_repo(tmp_path)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    report = doctor_opencode_agent_pack(start=tmp_path)
    checks = {check.key: check for check in report.checks}

    assert report.scope == "project"
    assert checks["opencode-binary"].ok is False
    assert checks["providers-list"].ok is False


def _write_repo(path: Path) -> None:
    (path / ".git").mkdir()
