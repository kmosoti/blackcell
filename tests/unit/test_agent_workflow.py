from pathlib import Path

import pytest

from blackcell.control_plane import (
    ContractError,
    LocalControlPlane,
    load_contract,
    validate_agent_workflow,
)
from blackcell.control_plane.agent_rendering import (
    MARKDOWN_START_PREFIX,
    RenderedCodexAgent,
    render_codex_agent_toml,
    render_codex_cli_artifacts,
    render_codex_cli_config,
    render_markdown_section,
)


def test_agent_workflow_dry_run_install_reports_creates_without_writing(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)

    result = LocalControlPlane(start=tmp_path).agent_workflow_install("codex-cli")

    assert [action.action for action in result.actions] == ["create"] * 5
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / "AGENTS.md").exists()


def test_agent_workflow_apply_install_creates_all_artifacts(tmp_path: Path) -> None:
    _write_contract(tmp_path)

    result = LocalControlPlane(start=tmp_path).agent_workflow_install(
        "codex-cli",
        apply_changes=True,
    )

    assert [action.action for action in result.actions] == ["create"] * 5
    assert result.drift is False
    assert all((tmp_path / path).exists() for path in _artifact_paths())


def test_agent_workflow_second_apply_is_noop(tmp_path: Path) -> None:
    _write_contract(tmp_path)
    control_plane = LocalControlPlane(start=tmp_path)

    control_plane.agent_workflow_install("codex-cli", apply_changes=True)
    result = control_plane.agent_workflow_install("codex-cli", apply_changes=True)

    assert [action.action for action in result.actions] == ["noop"] * 5
    assert result.drift is False


def test_agent_workflow_drift_check_passes_after_apply(tmp_path: Path) -> None:
    _write_contract(tmp_path)
    control_plane = LocalControlPlane(start=tmp_path)

    control_plane.agent_workflow_install("codex-cli", apply_changes=True)
    result = control_plane.agent_workflow_check_drift("codex-cli")

    assert result.drift is False
    assert [action.action for action in result.actions] == ["noop"] * 5


def test_agent_workflow_drift_check_fails_after_managed_artifact_change(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    control_plane = LocalControlPlane(start=tmp_path)
    control_plane.agent_workflow_install("codex-cli", apply_changes=True)
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("max_depth = 1", "max_depth = 2"),
        encoding="utf-8",
    )

    result = control_plane.agent_workflow_check_drift("codex-cli")

    assert result.drift is True
    assert result.actions[0].path == ".codex/config.toml"
    assert result.actions[0].action == "update"
    assert result.actions[0].current.exists is True
    assert result.actions[0].current.managed is True
    assert result.actions[0].current.body_digest != result.actions[0].rendered.body_digest


def test_agent_workflow_unmanaged_config_toml_conflicts(tmp_path: Path) -> None:
    _write_contract(tmp_path)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("[agents]\n", encoding="utf-8")

    result = LocalControlPlane(start=tmp_path).agent_workflow_install("codex-cli")

    assert result.conflicts is True
    assert result.actions[0].action == "conflict"
    assert result.actions[0].current.exists is True
    assert result.actions[0].current.managed is False
    assert result.actions[0].rendered.exists is True
    assert result.actions[0].rendered.managed is True


def test_agent_workflow_unmanaged_agent_toml_conflicts(tmp_path: Path) -> None:
    _write_contract(tmp_path)
    agents_dir = tmp_path / ".codex" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "quality-reviewer.toml").write_text(
        'name = "quality-reviewer"\n',
        encoding="utf-8",
    )

    result = LocalControlPlane(start=tmp_path).agent_workflow_install("codex-cli")

    action_by_path = {action.path: action for action in result.actions}
    action = action_by_path[".codex/agents/quality-reviewer.toml"]
    assert action.action == "conflict"
    assert action.current.exists is True
    assert action.current.managed is False


def test_agent_workflow_markdown_updates_managed_section_and_preserves_unmanaged_content(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    control_plane = LocalControlPlane(start=tmp_path)
    control_plane.agent_workflow_install("codex-cli", apply_changes=True)
    agents_path = tmp_path / "AGENTS.md"
    managed = agents_path.read_text(encoding="utf-8").replace(
        "Max delegation depth: `1`",
        "Max delegation depth: `9`",
    )
    agents_path.write_text(
        f"Human introduction.\n\n{managed}\nHuman footer.\n",
        encoding="utf-8",
    )

    result = control_plane.agent_workflow_install("codex-cli", apply_changes=True)
    text = agents_path.read_text(encoding="utf-8")

    assert {action.path: action.action for action in result.actions}["AGENTS.md"] == "update"
    assert "Human introduction." in text
    assert "Human footer." in text
    assert "Max delegation depth: `1`" in text
    assert "Max delegation depth: `9`" not in text


def test_agent_workflow_markdown_without_managed_section_appends_and_preserves_prose(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    agents_path = tmp_path / "AGENTS.md"
    agents_path.write_text("Human introduction.", encoding="utf-8")

    result = LocalControlPlane(start=tmp_path).agent_workflow_install(
        "codex-cli",
        apply_changes=True,
    )
    text = agents_path.read_text(encoding="utf-8")

    action_by_path = {action.path: action for action in result.actions}
    assert action_by_path["AGENTS.md"].action == "update"
    assert action_by_path["AGENTS.md"].applied is True
    assert action_by_path["AGENTS.md"].current.exists is True
    assert action_by_path["AGENTS.md"].current.managed is False
    assert action_by_path["AGENTS.md"].rendered.exists is True
    assert text.startswith("Human introduction.\n\n")
    assert text.count(MARKDOWN_START_PREFIX) == 1


def test_agent_workflow_malformed_markdown_markers_conflict_without_overwriting(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    agents_path = tmp_path / "AGENTS.md"
    malformed = (
        "<!-- blackcell:agent-workflow:start "
        "digest=sha256:0000000000000000000000000000000000000000000000000000000000000000 -->\n"
        "Stale managed content without an end marker.\n"
    )
    agents_path.write_text(malformed, encoding="utf-8")

    result = LocalControlPlane(start=tmp_path).agent_workflow_install(
        "codex-cli",
        apply_changes=True,
    )

    action_by_path = {action.path: action for action in result.actions}
    assert result.conflicts is True
    assert action_by_path["AGENTS.md"].action == "conflict"
    assert action_by_path["AGENTS.md"].applied is False
    assert action_by_path["AGENTS.md"].current.exists is True
    assert action_by_path["AGENTS.md"].current.managed is False
    assert agents_path.read_text(encoding="utf-8") == malformed


def test_agent_workflow_duplicate_markdown_markers_conflict_without_overwriting(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    agents_path = tmp_path / "AGENTS.md"
    duplicate_sections = (
        render_markdown_section("First managed section\n")[0]
        + "\n"
        + render_markdown_section("Second managed section\n")[0]
    )
    agents_path.write_text(duplicate_sections, encoding="utf-8")

    result = LocalControlPlane(start=tmp_path).agent_workflow_install(
        "codex-cli",
        apply_changes=True,
    )

    action_by_path = {action.path: action for action in result.actions}
    assert result.conflicts is True
    assert action_by_path["AGENTS.md"].action == "conflict"
    assert action_by_path["AGENTS.md"].applied is False
    assert agents_path.read_text(encoding="utf-8") == duplicate_sections


def test_agent_workflow_unsupported_target_is_rejected(tmp_path: Path) -> None:
    _write_contract(tmp_path)

    with pytest.raises(ValueError, match="unsupported agent workflow target"):
        LocalControlPlane(start=tmp_path).agent_workflow_install("other-target")


def test_agent_workflow_rejects_codex_agent_key_path_traversal(tmp_path: Path) -> None:
    _write_contract(tmp_path, key="../../../owned")

    with pytest.raises(ContractError, match="codex_cli\\.agents\\[0\\]\\.key"):
        LocalControlPlane(start=tmp_path).agent_workflow_install(
            "codex-cli",
            apply_changes=True,
        )

    assert not (tmp_path / ".codex").exists()


def test_agent_workflow_installed_agent_toml_contains_required_codex_fields(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)

    LocalControlPlane(start=tmp_path).agent_workflow_install("codex-cli", apply_changes=True)

    text = (tmp_path / ".codex" / "agents" / "spark-evidence-drafter.toml").read_text(
        encoding="utf-8"
    )
    assert "name = " in text
    assert "description = " in text
    assert "developer_instructions = " in text


def test_agent_workflow_renders_configured_codex_cli_fields(tmp_path: Path) -> None:
    _write_contract(tmp_path)

    LocalControlPlane(start=tmp_path).agent_workflow_install("codex-cli", apply_changes=True)

    config = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    evidence = (tmp_path / ".codex" / "agents" / "spark-evidence-drafter.toml").read_text(
        encoding="utf-8"
    )
    agents_markdown = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "max_threads = 6" in config
    assert "max_depth = 1" in config
    assert "Drafts evidence summaries from repository context" in evidence
    assert "Max worker threads: `6`" in agents_markdown
    assert "Sandbox mode: `read-only`" in agents_markdown


def test_agent_workflow_rendered_agents_are_read_only(tmp_path: Path) -> None:
    _write_contract(tmp_path)

    LocalControlPlane(start=tmp_path).agent_workflow_install("codex-cli", apply_changes=True)

    for agent_path in (
        tmp_path / ".codex" / "agents" / "spark-evidence-drafter.toml",
        tmp_path / ".codex" / "agents" / "quality-reviewer.toml",
    ):
        assert 'sandbox_mode = "read-only"' in agent_path.read_text(encoding="utf-8")


def test_agent_workflow_config_has_max_depth_one(tmp_path: Path) -> None:
    _write_contract(tmp_path)

    LocalControlPlane(start=tmp_path).agent_workflow_install("codex-cli", apply_changes=True)

    assert "max_depth = 1" in (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")


def test_agent_workflow_validation_rejects_invalid_rendered_fix_mode_guidance(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    contract = load_contract(tmp_path)
    artifacts = list(render_codex_cli_artifacts(contract))
    bad_reviewer = RenderedCodexAgent(
        key="quality-reviewer",
        name="quality-reviewer",
        description="Bad reviewer",
        developer_instructions="Run ruff check --fix before review.\n",
    )
    artifacts[2] = render_codex_agent_toml(
        bad_reviewer,
        path=".codex/agents/quality-reviewer.toml",
    )

    result = validate_agent_workflow(contract, artifacts=tuple(artifacts))

    assert result.valid is False
    assert "mutating_agent_guidance" in {error.code for error in result.errors}


def test_agent_workflow_validation_rejects_ruff_format_without_check(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    contract = load_contract(tmp_path)
    artifacts = list(render_codex_cli_artifacts(contract))
    bad_reviewer = RenderedCodexAgent(
        key="quality-reviewer",
        name="quality-reviewer",
        description="Bad reviewer",
        developer_instructions="Run ruff format before review.\n",
    )
    artifacts[2] = render_codex_agent_toml(
        bad_reviewer,
        path=".codex/agents/quality-reviewer.toml",
    )

    result = validate_agent_workflow(contract, artifacts=tuple(artifacts))

    assert result.valid is False
    assert "mutating_agent_guidance" in {error.code for error in result.errors}


def test_agent_workflow_validation_rejects_non_read_only_rendered_agent(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    contract = load_contract(tmp_path)
    artifacts = list(render_codex_cli_artifacts(contract))
    bad_reviewer = RenderedCodexAgent(
        key="quality-reviewer",
        name="quality-reviewer",
        description="Bad reviewer",
        developer_instructions="Review only.\n",
        sandbox_mode="workspace-write",
    )
    artifacts[2] = render_codex_agent_toml(
        bad_reviewer,
        path=".codex/agents/quality-reviewer.toml",
    )

    result = validate_agent_workflow(contract, artifacts=tuple(artifacts))

    assert result.valid is False
    assert "codex_agent_not_read_only" in {error.code for error in result.errors}


def test_agent_workflow_validation_rejects_rendered_max_depth_above_one(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    contract = load_contract(tmp_path)
    artifacts = list(render_codex_cli_artifacts(contract))
    artifacts[0] = render_codex_cli_config(max_depth=2)

    result = validate_agent_workflow(contract, artifacts=tuple(artifacts))

    assert result.valid is False
    assert "invalid_codex_max_depth" in {error.code for error in result.errors}


def _artifact_paths() -> tuple[str, ...]:
    return (
        ".codex/config.toml",
        ".codex/agents/spark-evidence-drafter.toml",
        ".codex/agents/quality-reviewer.toml",
        "AGENTS.md",
        "docs/agent/code_review.md",
    )


def _write_contract(path: Path, *, key: str = "spark-evidence-drafter") -> None:
    (path / ".git").mkdir()
    (path / "blackcell.plan.yaml").write_text(_contract_yaml(key=key), encoding="utf-8")


def _contract_yaml(*, key: str = "spark-evidence-drafter") -> str:
    return f"""
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
      owns:
        - src/blackcell/control_plane/agent_workflow.py
      change_spec:
        - Own rendered Codex CLI artifact projection.
  codex_cli:
    max_threads: 6
    max_depth: 1
    agents:
      - key: {key}
        name: spark-evidence-drafter
        description: Drafts evidence summaries from repository context without approving behavior.
        developer_instructions: |
          You are the BlackCell Spark evidence drafter for this repository.
          Operate in read-only mode.
          Inspect repository-authored planning context and summarize evidence only.
          Do not approve behavior, draft fixes, edit files, run mutating commands,
          or request remote state changes.
          Return concise notes that separate observed facts from open questions.
        sandbox_mode: read-only
      - key: quality-reviewer
        name: quality-reviewer
        description: Reviews repository changes for contract, test, and documentation risks.
        developer_instructions: |
          You are the BlackCell quality reviewer for repository changes.
          Operate in read-only review mode.
          Inspect diffs, tests, docs, and contract context.
          Report defects, missing coverage, and contract risks. Do not enter fix mode,
          edit files, commit changes, push branches, merge pull requests, close issues,
          or run remote-mutating workflows.
          When suggesting verification, use non-mutating check commands only.
        sandbox_mode: read-only
"""
