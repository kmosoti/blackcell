from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from blackcell.control_plane import (
    ContractError,
    IssueStatus,
    LocalControlPlane,
    load_contract,
    plan_contract_schema,
    validate_contract,
    validate_status_transition,
)
from blackcell.control_plane.capabilities import (
    load_github_capabilities,
    manifest_from_schema,
    validate_github_capabilities,
)


def test_contract_loads_enums_and_agent_context_inherits_global_policy(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml())

    contract = load_contract(tmp_path)
    context = LocalControlPlane(start=tmp_path).render_agent_context("BCP-0002")

    assert contract.issues[0].status is IssueStatus.TODO
    assert context.acceptance_criteria == ("global ac", "local ac")
    assert context.definition_of_ready == ("global ready", "local ready")
    assert context.definition_of_done == ("global done", "local done")
    assert [dependency.key for dependency in context.blocked_by] == ["BCP-0001"]
    assert context.agent_workflow
    assert context.agent_workflow.model == "gpt-5.3-codex-spark"


def test_issue_plan_public_properties_match_contract_api(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml())

    issue = load_contract(tmp_path).issues[1]

    assert issue.kind is issue.type
    assert issue.github_title == "Second issue"
    assert issue.is_backlog
    assert not issue.is_active
    assert not issue.is_done
    assert issue.has_dependencies
    assert not issue.has_scope
    assert issue.has_delivery_contract
    assert issue.hierarchy_keys == ("EPIC-1", "MS-1")


def test_contract_load_rejects_invalid_enum(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml().replace("status: Todo", "status: Ready"))

    with pytest.raises(ContractError, match="status must be one of"):
        load_contract(tmp_path)


def test_schema_includes_codex_cli_agent_projection_config() -> None:
    schema = plan_contract_schema()

    properties = cast(dict[str, Any], schema["properties"])
    agent_workflow = cast(dict[str, Any], properties["agent_workflow"])
    agent_workflow_properties = cast(dict[str, Any], agent_workflow["properties"])
    codex_cli = cast(dict[str, Any], agent_workflow_properties["codex_cli"])
    codex_cli_properties = cast(dict[str, Any], codex_cli["properties"])
    agents = cast(dict[str, Any], codex_cli_properties["agents"])
    agent = cast(dict[str, Any], agents["items"])
    agent_properties = cast(dict[str, Any], agent["properties"])

    assert cast(dict[str, Any], codex_cli_properties["max_threads"])["minimum"] == 1
    assert cast(dict[str, Any], codex_cli_properties["max_depth"])["maximum"] == 1
    assert "developer_instructions" in agent["required"]
    assert cast(dict[str, Any], agent_properties["sandbox_mode"])["type"] == "string"


def test_validate_contract_reports_missing_dependency(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml().replace("BCP-0001", "MISSING", 1))

    result = validate_contract(load_contract(tmp_path))

    assert not result.valid
    assert [error.code for error in result.errors] == ["missing_dependency"]


def test_validate_contract_reports_duplicate_issue_keys(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml().replace("key: BCP-0002", "key: BCP-0001"))

    result = validate_contract(load_contract(tmp_path))

    assert not result.valid
    assert "duplicate_key" in {error.code for error in result.errors}


def test_validate_contract_reports_dependency_cycles(tmp_path: Path) -> None:
    contract = _contract_yaml().replace(
        "depends_on:\n      - BCP-0001", "depends_on:\n      - BCP-0001"
    )
    contract = contract.replace("status: Todo", "status: Backlog", 1)
    contract = contract.replace(
        "change_spec:\n      - first issue",
        "depends_on:\n      - BCP-0002\n    change_spec:\n      - first issue",
    )
    _write_contract(tmp_path, contract)

    result = validate_contract(load_contract(tmp_path))

    assert not result.valid
    assert "dependency_cycle" in {error.code for error in result.errors}


def test_validate_contract_reports_active_blocked_dependencies(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml().replace("status: Backlog", "status: In Progress", 1))

    result = validate_contract(load_contract(tmp_path))

    assert not result.valid
    assert "blocked_dependency" in {error.code for error in result.errors}


def test_status_transition_policy_allows_only_declared_edges() -> None:
    assert validate_status_transition(IssueStatus.TODO, IssueStatus.IN_PROGRESS).valid

    result = validate_status_transition(IssueStatus.BACKLOG, IssueStatus.DONE)

    assert not result.valid
    assert result.errors[0].code == "invalid_status_transition"


def test_project_shape_is_provider_neutral(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml())

    shape = LocalControlPlane(start=tmp_path).plan_project_shape()

    assert [field.name for field in shape.fields] == [
        "Status",
        "Priority",
        "Complexity",
        "Type",
    ]
    assert shape.issue_count == 2


def test_default_github_capability_manifest_covers_required_references() -> None:
    result = validate_github_capabilities(Path.cwd())

    assert result.valid
    assert result.warnings == ()


def test_github_capability_validation_rejects_missing_mutation() -> None:
    manifest = load_github_capabilities(Path.cwd())
    manifest = replace(
        manifest,
        mutations=tuple(mutation for mutation in manifest.mutations if mutation != "createIssue"),
    )

    result = validate_github_capabilities(manifest=manifest)

    assert not result.valid
    assert any("mutation:createIssue" in error.message for error in result.errors)


def test_github_capability_validation_requires_update_issue_input() -> None:
    manifest = load_github_capabilities(Path.cwd())
    manifest = replace(
        manifest,
        input_objects={
            name: fields
            for name, fields in manifest.input_objects.items()
            if name != "UpdateIssueInput"
        },
    )

    result = validate_github_capabilities(manifest=manifest)

    assert not result.valid
    assert any("input_object:UpdateIssueInput" in error.message for error in result.errors)


def test_manifest_from_schema_extracts_mutations_fields_inputs_and_enums() -> None:
    manifest = manifest_from_schema(
        """
        \"\"\"
        Root mutation entrypoint.
        \"\"\"
        type Mutation {
          \"\"\"
          Create a repository issue.
          \"\"\"
          createIssue(input: CreateIssueInput!): CreateIssuePayload
        }

        type Query {
          node(id: ID!): Node
          repository(owner: String!, name: String!): Repository
        }

        type Repository {
          issue(number: Int!): Issue
        }

        input CreateIssueInput {
          repositoryId: ID!
          title: String!
        }

        enum ProjectV2ItemType {
          ISSUE
          PULL_REQUEST
        }
        """
    )

    assert "createIssue" in manifest.mutations
    assert "repository" in manifest.objects["Query"]
    assert manifest.input_objects["CreateIssueInput"] == ("repositoryId", "title")
    assert manifest.enums["ProjectV2ItemType"] == ("ISSUE", "PULL_REQUEST")


def _write_contract(path: Path, content: str) -> None:
    (path / ".git").mkdir()
    (path / "blackcell.plan.yaml").write_text(content, encoding="utf-8")


def _contract_yaml() -> str:
    return """
version: 1
project:
  key: BCP
  name: BlackCell
global:
  acceptance_criteria:
    - global ac
  definition_of_ready:
    - global ready
  definition_of_done:
    - global done
pr_policy:
  required_checks:
    - pytest
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
    change_spec:
      - first issue
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
    acceptance_criteria:
      - local ac
    definition_of_ready:
      - local ready
    definition_of_done:
      - local done
agent_workflow:
  model: gpt-5.3-codex-spark
  workers:
    - key: tests
      name: Test worker
"""
