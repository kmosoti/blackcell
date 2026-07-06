import json
import subprocess
from pathlib import Path

import pytest

from blackcell.cli.app import app
from blackcell.config import BlackcellConfig, ProjectRef, RepositoryRef, write_config
from blackcell.control_plane import (
    ControlPlaneSyncCache,
    IssuePlan,
    PlanContract,
    PullRequestCacheEntry,
    SyncCacheEntry,
    extract_contract_digest,
    extract_prior_remote_body,
    has_blackcell_issue_marker,
    has_blackcell_pull_request_marker,
    issue_body_digest,
    load_contract,
    load_github_capabilities,
    pull_request_body_digest,
    render_issue_body,
    render_pull_request_body,
    run_pull_request_workflow,
    write_github_capabilities,
)
from blackcell.control_plane.git import CheckResult, GitState
from blackcell.control_plane.pr import (
    PullRequestCommand,
    PullRequestWorkflowState,
)
from blackcell.control_plane.sync import sync_issues
from blackcell.models import (
    IssueRef,
    ProjectFieldOptionRef,
    ProjectFieldRef,
    ProjectItemFieldValueRef,
    ProjectItemRef,
    PullRequestRef,
)
from blackcell.providers import (
    CreateIssueRequest,
    CreateProjectFieldRequest,
    CreatePullRequestRequest,
    ProjectFieldValue,
)
from tests.cli_runner import CycloptsCliRunner

runner = CycloptsCliRunner()


def test_issue_body_rendering_is_deterministic(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    issue = contract.issues[0]

    body = render_issue_body(contract, issue)

    assert body == render_issue_body(contract, issue)
    assert has_blackcell_issue_marker(body, "BCP-0001")
    assert extract_contract_digest(body) is not None
    assert issue_body_digest(body) == issue_body_digest(render_issue_body(contract, issue))
    assert "title" not in body.splitlines()[0].lower()


def test_pull_request_body_rendering_is_deterministic(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    issue = contract.issues[0]

    body = render_pull_request_body(issue, issue_number=5, head_ref_name="feature/bcp-0001")

    assert body == render_pull_request_body(
        issue,
        issue_number=5,
        head_ref_name="feature/bcp-0001",
    )
    assert has_blackcell_pull_request_marker(body, "BCP-0001")
    assert pull_request_body_digest(body) == pull_request_body_digest(
        render_pull_request_body(issue, issue_number=5, head_ref_name="feature/bcp-0001")
    )
    assert "Related issue: #5" in body


def test_issue_body_preserves_prior_remote_context_once(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    issue = contract.issues[0]

    body = render_issue_body(contract, issue, prior_remote_body="human notes")

    assert "## Prior remote context" in body
    assert extract_prior_remote_body(body) == "human notes"


def test_sync_cache_reads_matching_repository_and_project_only(tmp_path: Path) -> None:
    cache = ControlPlaneSyncCache.open(path=tmp_path / "control_plane.sqlite3")
    assert cache is not None
    entry = SyncCacheEntry(
        issue_key="BCP-0001",
        repository_id="R_123",
        project_id="PVT_123",
        issue_id="I_123",
        issue_number=5,
        issue_url="https://example.test/issues/5",
        project_item_id="PVTI_123",
        contract_digest="sha256:contract",
        body_digest="sha256:body",
        synced_at="2026-06-30T00:00:00Z",
    )

    cache.upsert(entry)

    assert cache.get("BCP-0001", repository_id="R_123", project_id="PVT_123") == entry
    assert cache.get("BCP-0001", repository_id="R_other", project_id="PVT_123") is None
    assert cache.get("BCP-0001", repository_id="R_123", project_id="PVT_other") is None
    cache.close()


def test_pull_request_cache_reads_matching_repository_and_project_only(tmp_path: Path) -> None:
    cache = ControlPlaneSyncCache.open(path=tmp_path / "control_plane.sqlite3")
    assert cache is not None
    entry = PullRequestCacheEntry(
        issue_key="BCP-0001",
        repository_id="R_123",
        project_id="PVT_123",
        pull_request_id="PR_123",
        pull_request_number=12,
        pull_request_url="https://example.test/pull/12",
        issue_id="I_123",
        project_item_id="PVTI_123",
        base_ref_name="main",
        head_ref_name="feature/bcp-0001",
        head_ref_oid="HEAD",
        body_digest="sha256:body",
        is_draft=True,
        synced_at="2026-06-30T00:00:00Z",
    )

    cache.upsert_pull_request(entry)

    assert cache.get_pull_request("BCP-0001", repository_id="R_123", project_id="PVT_123") == entry
    assert cache.get_pull_request("BCP-0001", repository_id="R_other", project_id="PVT_123") is None
    assert cache.get_pull_request("BCP-0001", repository_id="R_123", project_id="PVT_other") is None
    cache.close()


def test_sync_dry_run_create_issue_has_no_mutations_or_cache(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    config = _config()
    provider = MemorySyncProvider(config)
    cache_path = tmp_path / "generated" / "cache" / "control_plane.sqlite3"

    result = sync_issues(
        contract=contract,
        config=config,
        provider=provider,
        start=tmp_path,
        cache_path=cache_path,
        issue_key="BCP-0001",
    )

    assert result.actions[0].type.value == "create_issue"
    assert result.actions[0].applied is False
    assert "create_project_field" in [action.type.value for action in result.actions]
    assert provider.created_requests == []
    assert not cache_path.exists()


def test_sync_apply_create_then_noop_uses_cache(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    config = _config()
    provider = MemorySyncProvider(config)
    cache_path = tmp_path / "control_plane.sqlite3"

    created = sync_issues(
        contract=contract,
        config=config,
        provider=provider,
        start=tmp_path,
        cache_path=cache_path,
        apply_changes=True,
        issue_key="BCP-0001",
    )
    noop = sync_issues(
        contract=contract,
        config=config,
        provider=provider,
        start=tmp_path,
        cache_path=cache_path,
        apply_changes=True,
        issue_key="BCP-0001",
    )

    assert created.actions[0].type.value == "create_issue"
    assert "update_project_item_field" in [action.type.value for action in created.actions]
    assert [action.type.value for action in noop.actions] == ["noop"]
    assert len(provider.created_requests) == 1


def test_sync_dry_run_reports_missing_project_fields(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    provider = MemorySyncProvider(_config())

    result = sync_issues(
        contract=contract,
        config=_config(),
        provider=provider,
        start=tmp_path,
        issue_key="BCP-0001",
    )

    assert [action.type.value for action in result.actions] == [
        "create_issue",
        "create_project_field",
        "create_project_field",
        "create_project_field",
        "create_project_field",
    ]
    assert provider.created_project_field_requests == []


def test_sync_apply_sets_project_fields_then_noops(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    config = _config()
    provider = MemorySyncProvider(config)
    cache_path = tmp_path / "control_plane.sqlite3"

    created = sync_issues(
        contract=contract,
        config=config,
        provider=provider,
        start=tmp_path,
        cache_path=cache_path,
        apply_changes=True,
        issue_key="BCP-0001",
    )
    noop = sync_issues(
        contract=contract,
        config=config,
        provider=provider,
        start=tmp_path,
        cache_path=cache_path,
        apply_changes=True,
        issue_key="BCP-0001",
    )

    assert [action.type.value for action in created.actions] == [
        "create_issue",
        "create_project_field",
        "create_project_field",
        "create_project_field",
        "create_project_field",
        "update_project_item_field",
        "update_project_item_field",
        "update_project_item_field",
        "update_project_item_field",
    ]
    assert [action.type.value for action in noop.actions] == ["noop"]
    assert len(provider.created_project_field_requests) == 4
    assert len(provider.updated_project_item_field_values) == 4


def test_sync_updates_missing_status_options_preserves_custom_options(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml(status="Review Required"))
    contract = load_contract(tmp_path)
    config = _config()
    provider = MemorySyncProvider(
        config,
        fields=[
            ProjectFieldRef(
                id="FIELD_Status",
                name="Status",
                data_type="SINGLE_SELECT",
                options=(
                    ProjectFieldOptionRef(id="status_blocked", name="Blocked"),
                    ProjectFieldOptionRef(id="status_todo", name="Todo"),
                    ProjectFieldOptionRef(id="status_done", name="Done"),
                ),
            ),
            ProjectFieldRef(
                id="FIELD_Priority",
                name="Priority",
                data_type="SINGLE_SELECT",
                options=(
                    ProjectFieldOptionRef(id="priority_p0", name="P0"),
                    ProjectFieldOptionRef(id="priority_p1", name="P1"),
                    ProjectFieldOptionRef(id="priority_p2", name="P2"),
                    ProjectFieldOptionRef(id="priority_p3", name="P3"),
                ),
            ),
            ProjectFieldRef(id="FIELD_Complexity", name="Complexity", data_type="NUMBER"),
            ProjectFieldRef(
                id="FIELD_Type",
                name="Type",
                data_type="SINGLE_SELECT",
                options=(
                    ProjectFieldOptionRef(id="type_feature", name="feature"),
                    ProjectFieldOptionRef(id="type_bug", name="bug"),
                    ProjectFieldOptionRef(id="type_refactor", name="refactor"),
                    ProjectFieldOptionRef(id="type_chore", name="chore"),
                ),
            ),
        ],
    )

    result = sync_issues(
        contract=contract,
        config=config,
        provider=provider,
        start=tmp_path,
        cache_path=tmp_path / "control_plane.sqlite3",
        apply_changes=True,
        issue_key="BCP-0001",
    )

    assert "update_project_field" in [action.type.value for action in result.actions]
    assert provider.updated_project_field_options == [
        (
            "FIELD_Status",
            ("Blocked", "Todo", "Done", "Backlog", "In Progress", "Review Required"),
        )
    ]


def test_sync_full_contract_archives_unmanaged_issue_project_items(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    config = _config()
    unmanaged = ProjectItemRef(
        id="PVTI_unmanaged",
        type="ISSUE",
        is_archived=False,
        project=config.project,
        content_id="I_unmanaged",
        content_title="Obsolete issue",
        content_url="https://example.test/issues/99",
        content_type="Issue",
    )
    provider = MemorySyncProvider(config, project_items=[unmanaged])

    result = sync_issues(
        contract=contract,
        config=config,
        provider=provider,
        start=tmp_path,
        cache_path=tmp_path / "control_plane.sqlite3",
        apply_changes=True,
    )

    assert "archive_project_item" in [action.type.value for action in result.actions]
    assert provider.archived_project_item_ids == ["PVTI_unmanaged"]


def test_sync_full_contract_archives_unmanaged_issue_items_beyond_first_page(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    config = _config()
    project_items = [
        ProjectItemRef(
            id=f"PVTI_pr_{index}",
            type="PULL_REQUEST",
            is_archived=False,
            project=config.project,
            content_id=f"PR_{index}",
            content_title=f"PR {index}",
            content_url=f"https://example.test/pull/{index}",
            content_type="PullRequest",
        )
        for index in range(100)
    ]
    project_items.append(
        ProjectItemRef(
            id="PVTI_unmanaged_late",
            type="ISSUE",
            is_archived=False,
            project=config.project,
            content_id="I_unmanaged_late",
            content_title="Late obsolete issue",
            content_url="https://example.test/issues/199",
            content_type="Issue",
        )
    )
    provider = MemorySyncProvider(config, project_items=project_items)

    result = sync_issues(
        contract=contract,
        config=config,
        provider=provider,
        start=tmp_path,
        cache_path=tmp_path / "control_plane.sqlite3",
        apply_changes=True,
    )

    assert "archive_project_item" in [action.type.value for action in result.actions]
    assert "PVTI_unmanaged_late" in provider.archived_project_item_ids


def test_sync_issue_key_does_not_archive_unmanaged_project_items(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    config = _config()
    unmanaged = ProjectItemRef(
        id="PVTI_unmanaged",
        type="ISSUE",
        is_archived=False,
        project=config.project,
        content_id="I_unmanaged",
        content_title="Obsolete issue",
        content_url="https://example.test/issues/99",
        content_type="Issue",
    )
    provider = MemorySyncProvider(config, project_items=[unmanaged])

    result = sync_issues(
        contract=contract,
        config=config,
        provider=provider,
        start=tmp_path,
        cache_path=tmp_path / "control_plane.sqlite3",
        apply_changes=True,
        issue_key="BCP-0001",
    )

    assert "archive_project_item" not in [action.type.value for action in result.actions]
    assert provider.archived_project_item_ids == []


def test_sync_issue_key_filters_contract_issues(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    provider = MemorySyncProvider(_config())

    result = sync_issues(
        contract=contract,
        config=_config(),
        provider=provider,
        start=tmp_path,
        issue_key="BCP-0002",
    )

    assert result.issue_count == 1
    assert result.actions[0].issue_key == "BCP-0002"


def test_sync_adopts_exact_title_and_preserves_prior_body_once(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    config = _config()
    remote = IssueRef(
        id="I_existing",
        number=9,
        title="First issue",
        url="https://example.test/issues/9",
        state="OPEN",
        repository=config.repository,
        body="human notes",
    )
    provider = MemorySyncProvider(config, issues=[remote])
    cache_path = tmp_path / "control_plane.sqlite3"

    first = sync_issues(
        contract=contract,
        config=config,
        provider=provider,
        start=tmp_path,
        cache_path=cache_path,
        apply_changes=True,
        issue_key="BCP-0001",
    )
    second = sync_issues(
        contract=contract,
        config=config,
        provider=provider,
        start=tmp_path,
        cache_path=cache_path,
        apply_changes=True,
        issue_key="BCP-0001",
    )

    assert [action.type.value for action in first.actions[:3]] == [
        "adopt_issue",
        "update_issue",
        "attach_project_item",
    ]
    assert "update_project_item_field" in [action.type.value for action in first.actions]
    assert provider.issues["I_existing"].body
    assert provider.issues["I_existing"].body.count("## Prior remote context") == 1
    assert extract_prior_remote_body(provider.issues["I_existing"].body or "") == "human notes"
    assert [action.type.value for action in second.actions] == ["noop"]
    assert len(provider.updated_requests) == 1


def test_sync_stale_cache_rediscovery_adopts_remote_issue(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    config = _config()
    cache_path = tmp_path / "control_plane.sqlite3"
    cache = ControlPlaneSyncCache.open(path=cache_path)
    assert cache is not None
    cache.upsert(
        SyncCacheEntry(
            issue_key="BCP-0001",
            repository_id="R_123",
            project_id="PVT_123",
            issue_id="I_deleted",
            issue_number=1,
            issue_url="https://example.test/issues/1",
            project_item_id=None,
            contract_digest="sha256:old",
            body_digest="sha256:old",
            synced_at="2026-06-30T00:00:00Z",
        )
    )
    cache.close()
    remote = IssueRef(
        id="I_existing",
        number=9,
        title="First issue",
        url="https://example.test/issues/9",
        state="OPEN",
        repository=config.repository,
        body="human notes",
    )
    provider = MemorySyncProvider(config, issues=[remote])

    result = sync_issues(
        contract=contract,
        config=config,
        provider=provider,
        start=tmp_path,
        cache_path=cache_path,
        apply_changes=True,
        issue_key="BCP-0001",
    )

    assert result.actions[0].type.value == "adopt_issue"
    assert result.actions[0].source == "exact_title"


def test_sync_marker_rediscovery_preserves_prior_context(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    config = _config()
    managed_body = render_issue_body(
        contract,
        contract.issues[0],
        prior_remote_body="human notes",
    ).replace("first change", "old change")
    remote = IssueRef(
        id="I_existing",
        number=9,
        title="First issue",
        url="https://example.test/issues/9",
        state="OPEN",
        repository=config.repository,
        body=managed_body,
    )
    provider = MemorySyncProvider(config, issues=[remote])

    result = sync_issues(
        contract=contract,
        config=config,
        provider=provider,
        start=tmp_path,
        cache_path=tmp_path / "control_plane.sqlite3",
        apply_changes=True,
        issue_key="BCP-0001",
        refresh_cache=True,
    )

    assert result.actions[0].source == "body_marker"
    assert extract_prior_remote_body(provider.issues["I_existing"].body or "") == "human notes"


def test_sync_cli_dry_run_outputs_json_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_contract(tmp_path, _contract_yaml())
    write_config(_config(), start=tmp_path)
    _write_capabilities(tmp_path)
    provider = MemorySyncProvider(_config())
    monkeypatch.setattr(
        "blackcell.control_plane.facade.default_registry",
        lambda: MemoryRegistry(provider),
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["control-plane", "sync", "--issue-key", "BCP-0001"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["actions"][0]["type"] == "create_issue"
    assert provider.created_requests == []


def test_sync_cli_missing_capability_reports_json_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_contract(tmp_path, _contract_yaml())
    write_config(_config(), start=tmp_path)
    manifest = load_github_capabilities(Path.cwd())
    manifest = type(manifest)(
        generated_at=manifest.generated_at,
        schema_url=manifest.schema_url,
        reference_urls=manifest.reference_urls,
        mutations=tuple(item for item in manifest.mutations if item != "updateIssue"),
        objects=manifest.objects,
        input_objects=manifest.input_objects,
        enums=manifest.enums,
    )
    write_github_capabilities(manifest, start=tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["control-plane", "sync", "--issue-key", "BCP-0001"],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "GitHub capability validation failed" in json.loads(result.stderr)["error"]["message"]


def test_pull_request_workflow_dirty_worktree_blocks_before_remote(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    provider = MemorySyncProvider(_config(), issues=[_remote_issue(_config())])

    result = run_pull_request_workflow(
        contract=contract,
        config=_config(),
        provider=provider,
        start=tmp_path,
        cache_path=tmp_path / "control_plane.sqlite3",
        issue_key="BCP-0001",
        command=PullRequestCommand.SYNC,
        apply_changes=True,
        git_state=_git_state(dirty=True),
    )

    assert result.state is PullRequestWorkflowState.NEEDS_CHANGES
    assert result.blockers == ("dirty_worktree",)
    assert result.actions == ()
    assert provider.created_pull_request_requests == []


def test_pull_request_workflow_requires_pushed_branch(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    provider = MemorySyncProvider(_config(), issues=[_remote_issue(_config())])

    result = run_pull_request_workflow(
        contract=contract,
        config=_config(),
        provider=provider,
        start=tmp_path,
        cache_path=tmp_path / "control_plane.sqlite3",
        issue_key="BCP-0001",
        command=PullRequestCommand.SYNC,
        git_state=_git_state(upstream=False),
    )

    assert result.state is PullRequestWorkflowState.NEEDS_PUSH
    assert result.next_commands == ("git push -u origin feature/bcp-0001",)
    assert result.actions == ()


def test_pull_request_workflow_issue_not_synced_reports_targeted_commands(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    provider = MemorySyncProvider(_config())

    result = run_pull_request_workflow(
        contract=contract,
        config=_config(),
        provider=provider,
        start=tmp_path,
        cache_path=tmp_path / "control_plane.sqlite3",
        issue_key="BCP-0001",
        command=PullRequestCommand.SYNC,
        git_state=_git_state(),
    )

    assert result.state is PullRequestWorkflowState.READY_BLOCKED
    assert result.blockers == ("issue_not_synced",)
    assert result.next_commands == (
        "uv run blackcell control-plane sync --issue-key BCP-0001 --apply",
        "uv run blackcell control-plane pr sync --issue-key BCP-0001 --apply",
    )
    assert result.actions == ()
    assert provider.created_pull_request_requests == []


def test_pull_request_workflow_dry_run_create_has_no_mutations_or_cache(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    config = _config()
    provider = MemorySyncProvider(config, issues=[_remote_issue(config)])
    cache_path = tmp_path / "control_plane.sqlite3"

    result = run_pull_request_workflow(
        contract=contract,
        config=config,
        provider=provider,
        start=tmp_path,
        cache_path=cache_path,
        issue_key="BCP-0001",
        command=PullRequestCommand.SYNC,
        git_state=_git_state(),
    )

    assert result.state is PullRequestWorkflowState.NEEDS_DRAFT_PR
    assert result.next_commands == (
        "uv run blackcell control-plane pr sync --issue-key BCP-0001 --apply",
    )
    assert [action.type.value for action in result.actions] == ["create_pull_request"]
    assert result.actions[0].applied is False
    assert provider.created_pull_request_requests == []
    assert not cache_path.exists()


def test_pull_request_workflow_apply_create_preflights_project_fields(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    config = _config()
    provider = MemorySyncProvider(config, issues=[_remote_issue(config)])

    with pytest.raises(ValueError, match="missing required contract fields"):
        run_pull_request_workflow(
            contract=contract,
            config=config,
            provider=provider,
            start=tmp_path,
            cache_path=tmp_path / "control_plane.sqlite3",
            issue_key="BCP-0001",
            command=PullRequestCommand.SYNC,
            apply_changes=True,
            git_state=_git_state(),
        )

    assert provider.created_pull_request_requests == []
    assert provider.attached_content_ids == []


def test_pull_request_workflow_apply_create_preflights_project_field_options(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    config = _config()
    fields = _project_fields()
    fields[0] = ProjectFieldRef(
        id="FIELD_Status",
        name="Status",
        data_type="SINGLE_SELECT",
        options=(
            ProjectFieldOptionRef(id="status_backlog", name="Backlog"),
            ProjectFieldOptionRef(id="status_done", name="Done"),
        ),
    )
    provider = MemorySyncProvider(
        config,
        issues=[_remote_issue(config)],
        fields=fields,
    )

    with pytest.raises(ValueError, match="missing option Todo"):
        run_pull_request_workflow(
            contract=contract,
            config=config,
            provider=provider,
            start=tmp_path,
            cache_path=tmp_path / "control_plane.sqlite3",
            issue_key="BCP-0001",
            command=PullRequestCommand.SYNC,
            apply_changes=True,
            git_state=_git_state(),
        )

    assert provider.created_pull_request_requests == []
    assert provider.attached_content_ids == []


def test_pull_request_workflow_apply_create_then_noop_uses_cache(tmp_path: Path) -> None:
    _write_contract(tmp_path, _contract_yaml())
    contract = load_contract(tmp_path)
    config = _config()
    provider = MemorySyncProvider(
        config,
        issues=[_remote_issue(config)],
        fields=_project_fields(),
    )
    cache_path = tmp_path / "control_plane.sqlite3"

    created = run_pull_request_workflow(
        contract=contract,
        config=config,
        provider=provider,
        start=tmp_path,
        cache_path=cache_path,
        issue_key="BCP-0001",
        command=PullRequestCommand.SYNC,
        apply_changes=True,
        git_state=_git_state(),
    )
    noop = run_pull_request_workflow(
        contract=contract,
        config=config,
        provider=provider,
        start=tmp_path,
        cache_path=cache_path,
        issue_key="BCP-0001",
        command=PullRequestCommand.SYNC,
        apply_changes=True,
        git_state=_git_state(),
    )

    assert created.state is PullRequestWorkflowState.DRAFT_OPEN
    assert [action.type.value for action in created.actions] == [
        "create_pull_request",
        "attach_project_item",
        "update_project_item_field",
        "update_project_item_field",
        "update_project_item_field",
        "update_project_item_field",
    ]
    assert [action.type.value for action in noop.actions] == ["noop"]
    assert len(provider.created_pull_request_requests) == 1
    assert provider.attached_content_ids == ["PR_created_1"]
    assert len(provider.updated_project_item_field_values) == 4


def test_pull_request_workflow_ready_blocks_until_issue_review_required(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path, _contract_yaml(status="Todo", required_checks=("pytest",)))
    contract = load_contract(tmp_path)
    config = _config()
    issue = _remote_issue(config)
    pull_request = _draft_pull_request(config, contract, issue)
    provider = MemorySyncProvider(
        config,
        issues=[issue],
        pull_requests=[pull_request],
        project_items=[_pull_request_project_item(config, pull_request)],
        fields=_project_fields(),
    )

    result = run_pull_request_workflow(
        contract=contract,
        config=config,
        provider=provider,
        start=tmp_path,
        cache_path=tmp_path / "control_plane.sqlite3",
        issue_key="BCP-0001",
        command=PullRequestCommand.READY,
        apply_changes=True,
        git_state=_git_state(),
        check_runner=_passing_checks,
    )

    assert result.state is PullRequestWorkflowState.READY_BLOCKED
    assert result.blockers == ("issue_status_not_review_required",)
    assert provider.ready_pull_request_ids == []


def test_pull_request_workflow_ready_marks_draft_ready_when_gates_pass(
    tmp_path: Path,
) -> None:
    _write_contract(
        tmp_path,
        _contract_yaml(status="Review Required", required_checks=("pytest",)),
    )
    contract = load_contract(tmp_path)
    config = _config()
    issue = _remote_issue(config)
    pull_request = _draft_pull_request(config, contract, issue)
    provider = MemorySyncProvider(
        config,
        issues=[issue],
        pull_requests=[pull_request],
        project_items=[_pull_request_project_item(config, pull_request)],
        fields=_project_fields(),
    )

    result = run_pull_request_workflow(
        contract=contract,
        config=config,
        provider=provider,
        start=tmp_path,
        cache_path=tmp_path / "control_plane.sqlite3",
        issue_key="BCP-0001",
        command=PullRequestCommand.READY,
        apply_changes=True,
        git_state=_git_state(),
        check_runner=_passing_checks,
    )

    assert result.state is PullRequestWorkflowState.REVIEW_READY
    assert [action.type.value for action in result.actions] == [
        "update_project_item_field",
        "update_project_item_field",
        "update_project_item_field",
        "update_project_item_field",
        "mark_ready_for_review",
    ]
    assert provider.ready_pull_request_ids == ["PR_1"]
    assert len(provider.updated_project_item_field_values) == 4
    assert result.pull_request is not None
    assert result.pull_request.is_draft is False


def test_pull_request_workflow_ready_blocks_on_failed_check(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        _contract_yaml(status="Review Required", required_checks=("pytest",)),
    )
    contract = load_contract(tmp_path)
    config = _config()
    issue = _remote_issue(config)
    pull_request = _draft_pull_request(config, contract, issue)
    provider = MemorySyncProvider(
        config,
        issues=[issue],
        pull_requests=[pull_request],
        project_items=[_pull_request_project_item(config, pull_request)],
        fields=_project_fields(),
    )

    result = run_pull_request_workflow(
        contract=contract,
        config=config,
        provider=provider,
        start=tmp_path,
        cache_path=tmp_path / "control_plane.sqlite3",
        issue_key="BCP-0001",
        command=PullRequestCommand.READY,
        apply_changes=True,
        git_state=_git_state(),
        check_runner=_failing_checks,
    )

    assert result.state is PullRequestWorkflowState.READY_BLOCKED
    assert result.blockers == ("check_failed:pytest",)
    assert provider.ready_pull_request_ids == []


def test_pull_request_cli_status_reports_json_push_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_contract(tmp_path, _contract_yaml())
    write_config(_config(), start=tmp_path)
    _write_capabilities(tmp_path)
    _init_git_repo(tmp_path)
    provider = MemorySyncProvider(_config(), issues=[_remote_issue(_config())])
    monkeypatch.setattr(
        "blackcell.control_plane.facade.default_registry",
        lambda: MemoryRegistry(provider),
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["control-plane", "pr", "status", "--issue-key", "BCP-0001"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["state"] == "needs_push"
    assert payload["blockers"] == ["branch_not_pushed"]
    assert payload["actions"] == []


class MemoryRegistry:
    def __init__(self, provider: MemorySyncProvider) -> None:
        self.provider = provider

    def create(self, name: str, config: BlackcellConfig) -> MemorySyncProvider:
        return self.provider


class MemorySyncProvider:
    name = "memory"

    def __init__(
        self,
        config: BlackcellConfig,
        *,
        issues: list[IssueRef] | None = None,
        pull_requests: list[PullRequestRef] | None = None,
        project_items: list[ProjectItemRef] | None = None,
        fields: list[ProjectFieldRef] | None = None,
    ) -> None:
        self.config = config
        self.issues = {issue.id: issue for issue in issues or []}
        self.pull_requests = {pr.id: pr for pr in pull_requests or []}
        self.project_items = list(project_items or [])
        self.fields = list(fields or [])
        self.field_values: dict[tuple[str, str], ProjectFieldValue] = {}
        self.created_requests: list[CreateIssueRequest] = []
        self.updated_requests: list[tuple[str, str, str]] = []
        self.created_pull_request_requests: list[CreatePullRequestRequest] = []
        self.updated_pull_request_requests: list[tuple[str, str, str]] = []
        self.ready_pull_request_ids: list[str] = []
        self.created_project_field_requests: list[CreateProjectFieldRequest] = []
        self.updated_project_field_options: list[tuple[str, tuple[str, ...]]] = []
        self.updated_project_item_field_values: list[tuple[str, str, ProjectFieldValue]] = []
        self.archived_project_item_ids: list[str] = []
        self.attached_content_ids: list[str] = []

    def create_issue(self, request: CreateIssueRequest) -> IssueRef:
        self.created_requests.append(request)
        issue = IssueRef(
            id=f"I_created_{len(self.created_requests)}",
            number=len(self.created_requests),
            title=request.title,
            url=f"https://example.test/issues/{len(self.created_requests)}",
            state="OPEN",
            repository=self.config.repository,
            body=request.body,
        )
        self.issues[issue.id] = issue
        self.project_items.append(self._project_item(issue))
        return issue

    def read_issue(self, number: int) -> IssueRef:
        for issue in self.issues.values():
            if issue.number == number:
                return issue
        raise ValueError(f"unknown issue number: {number}")

    def read_issue_by_id(self, issue_id: str) -> IssueRef | None:
        return self.issues.get(issue_id)

    def list_repository_issues(self, *, first: int = 100) -> list[IssueRef]:
        return list(self.issues.values())[:first]

    def find_issues_by_blackcell_marker(self, issue_key: str) -> list[IssueRef]:
        return [
            issue
            for issue in self.list_repository_issues()
            if has_blackcell_issue_marker(issue.body or "", issue_key)
        ]

    def find_issues_by_exact_title(self, title: str) -> list[IssueRef]:
        return [issue for issue in self.list_repository_issues() if issue.title == title]

    def update_issue(self, *, issue_id: str, title: str, body: str) -> IssueRef:
        self.updated_requests.append((issue_id, title, body))
        issue = self.issues[issue_id]
        updated = IssueRef(
            id=issue.id,
            number=issue.number,
            title=title,
            url=issue.url,
            state=issue.state,
            repository=issue.repository,
            body=body,
        )
        self.issues[issue_id] = updated
        return updated

    def read_pull_request_by_id(self, pull_request_id: str) -> PullRequestRef | None:
        return self.pull_requests.get(pull_request_id)

    def list_repository_pull_requests(self, *, first: int = 100) -> list[PullRequestRef]:
        return list(self.pull_requests.values())[:first]

    def find_pull_requests_by_blackcell_marker(self, issue_key: str) -> list[PullRequestRef]:
        return [
            pull_request
            for pull_request in self.list_repository_pull_requests()
            if has_blackcell_pull_request_marker(pull_request.body or "", issue_key)
        ]

    def find_pull_requests_by_head(self, head_ref_name: str) -> list[PullRequestRef]:
        return [
            pull_request
            for pull_request in self.list_repository_pull_requests()
            if pull_request.head_ref_name == head_ref_name
        ]

    def create_pull_request(self, request: CreatePullRequestRequest) -> PullRequestRef:
        self.created_pull_request_requests.append(request)
        pull_request = PullRequestRef(
            id=f"PR_created_{len(self.created_pull_request_requests)}",
            number=len(self.created_pull_request_requests),
            title=request.title,
            url=f"https://example.test/pull/{len(self.created_pull_request_requests)}",
            state="OPEN",
            is_draft=request.draft,
            base_ref_name=request.base_ref_name,
            head_ref_name=request.head_ref_name,
            head_ref_oid="HEAD",
            repository=self.config.repository,
            body=request.body,
        )
        self.pull_requests[pull_request.id] = pull_request
        return pull_request

    def update_pull_request(self, *, pull_request_id: str, title: str, body: str) -> PullRequestRef:
        self.updated_pull_request_requests.append((pull_request_id, title, body))
        pull_request = self.pull_requests[pull_request_id]
        updated = PullRequestRef(
            id=pull_request.id,
            number=pull_request.number,
            title=title,
            url=pull_request.url,
            state=pull_request.state,
            is_draft=pull_request.is_draft,
            base_ref_name=pull_request.base_ref_name,
            head_ref_name=pull_request.head_ref_name,
            head_ref_oid=pull_request.head_ref_oid,
            repository=pull_request.repository,
            body=body,
        )
        self.pull_requests[pull_request_id] = updated
        return updated

    def mark_pull_request_ready_for_review(self, pull_request_id: str) -> PullRequestRef:
        self.ready_pull_request_ids.append(pull_request_id)
        pull_request = self.pull_requests[pull_request_id]
        updated = PullRequestRef(
            id=pull_request.id,
            number=pull_request.number,
            title=pull_request.title,
            url=pull_request.url,
            state=pull_request.state,
            is_draft=False,
            base_ref_name=pull_request.base_ref_name,
            head_ref_name=pull_request.head_ref_name,
            head_ref_oid=pull_request.head_ref_oid,
            repository=pull_request.repository,
            body=pull_request.body,
        )
        self.pull_requests[pull_request_id] = updated
        return updated

    def list_project_items(self, *, first: int | None = 20) -> list[ProjectItemRef]:
        items = self.project_items if first is None else self.project_items[:first]
        return [self._with_field_values(item) for item in items]

    def add_project_item_by_id(self, content_id: str) -> ProjectItemRef:
        self.attached_content_ids.append(content_id)
        if content_id in self.issues:
            item = self._project_item(self.issues[content_id])
        elif content_id in self.pull_requests:
            item = self._pull_request_project_item(self.pull_requests[content_id])
        else:
            raise ValueError(f"unknown project content ID: {content_id}")
        self.project_items.append(item)
        return item

    def archive_project_item(self, item_id: str) -> None:
        self.archived_project_item_ids.append(item_id)
        self.project_items = [
            ProjectItemRef(
                id=item.id,
                type=item.type,
                is_archived=True if item.id == item_id else item.is_archived,
                project=item.project,
                content_id=item.content_id,
                content_title=item.content_title,
                content_url=item.content_url,
                content_type=item.content_type,
                field_values=item.field_values,
            )
            for item in self.project_items
        ]

    def list_project_fields(self, *, first: int = 50) -> list[ProjectFieldRef]:
        return self.fields[:first]

    def create_project_field(self, request: CreateProjectFieldRequest) -> ProjectFieldRef:
        self.created_project_field_requests.append(request)
        field = ProjectFieldRef(
            id=f"FIELD_{request.name}",
            name=request.name,
            data_type=request.data_type,
            options=tuple(
                ProjectFieldOptionRef(
                    id=f"{request.name.lower()}_{option_name.lower().replace(' ', '_')}",
                    name=option_name,
                )
                for option_name in request.single_select_options
            ),
        )
        self.fields.append(field)
        return field

    def update_project_single_select_field_options(
        self,
        field: ProjectFieldRef,
        option_names: tuple[str, ...],
    ) -> ProjectFieldRef:
        self.updated_project_field_options.append((field.id, option_names))
        existing_by_name = {option.name: option for option in field.options}
        updated = ProjectFieldRef(
            id=field.id,
            name=field.name,
            data_type=field.data_type,
            options=tuple(
                existing_by_name.get(option_name)
                or ProjectFieldOptionRef(
                    id=f"{field.name.lower()}_{option_name.lower().replace(' ', '_')}",
                    name=option_name,
                )
                for option_name in option_names
            ),
        )
        self.fields = [updated if item.id == field.id else item for item in self.fields]
        return updated

    def update_project_item_field_value(
        self,
        *,
        item_id: str,
        field_id: str,
        value: ProjectFieldValue,
    ) -> None:
        self.updated_project_item_field_values.append((item_id, field_id, value))
        self.field_values[(item_id, field_id)] = value

    def _project_item(self, issue: IssueRef) -> ProjectItemRef:
        return ProjectItemRef(
            id=f"PVTI_{issue.id}",
            type="ISSUE",
            is_archived=False,
            project=self.config.project,
            content_id=issue.id,
            content_title=issue.title,
            content_url=issue.url,
            content_type="Issue",
        )

    def _pull_request_project_item(self, pull_request: PullRequestRef) -> ProjectItemRef:
        return ProjectItemRef(
            id=f"PVTI_{pull_request.id}",
            type="PULL_REQUEST",
            is_archived=False,
            project=self.config.project,
            content_id=pull_request.id,
            content_title=pull_request.title,
            content_url=pull_request.url,
            content_type="PullRequest",
        )

    def _with_field_values(self, item: ProjectItemRef) -> ProjectItemRef:
        values: list[ProjectItemFieldValueRef] = []
        field_by_id = {field.id: field for field in self.fields}
        for (item_id, field_id), value in self.field_values.items():
            if item_id != item.id:
                continue
            field = field_by_id[field_id]
            if value.number is not None:
                values.append(
                    ProjectItemFieldValueRef(
                        field_id=field.id,
                        field_name=field.name,
                        type="number",
                        number=value.number,
                    )
                )
            elif value.single_select_option_id is not None:
                option_name = _option_name(field, value.single_select_option_id)
                values.append(
                    ProjectItemFieldValueRef(
                        field_id=field.id,
                        field_name=field.name,
                        type="single_select",
                        option_id=value.single_select_option_id,
                        option_name=option_name,
                    )
                )
        return ProjectItemRef(
            id=item.id,
            type=item.type,
            is_archived=item.is_archived,
            project=item.project,
            content_id=item.content_id,
            content_title=item.content_title,
            content_url=item.content_url,
            content_type=item.content_type,
            field_values=tuple(values),
        )


def _write_contract(path: Path, content: str) -> None:
    (path / ".git").mkdir(exist_ok=True)
    (path / "blackcell.plan.yaml").write_text(content, encoding="utf-8")


def _write_capabilities(path: Path) -> None:
    write_github_capabilities(load_github_capabilities(Path.cwd()), start=path)


def _config() -> BlackcellConfig:
    return BlackcellConfig(
        repository=RepositoryRef(owner="kmosoti", name="blackcell", node_id="R_123"),
        project=ProjectRef(id="PVT_123", number=7, title="BlackCell"),
    )


def _project_fields() -> list[ProjectFieldRef]:
    return [
        ProjectFieldRef(
            id="FIELD_Status",
            name="Status",
            data_type="SINGLE_SELECT",
            options=(
                ProjectFieldOptionRef(id="status_backlog", name="Backlog"),
                ProjectFieldOptionRef(id="status_todo", name="Todo"),
                ProjectFieldOptionRef(id="status_in_progress", name="In Progress"),
                ProjectFieldOptionRef(id="status_review_required", name="Review Required"),
                ProjectFieldOptionRef(id="status_done", name="Done"),
            ),
        ),
        ProjectFieldRef(
            id="FIELD_Priority",
            name="Priority",
            data_type="SINGLE_SELECT",
            options=(
                ProjectFieldOptionRef(id="priority_p0", name="P0"),
                ProjectFieldOptionRef(id="priority_p1", name="P1"),
                ProjectFieldOptionRef(id="priority_p2", name="P2"),
                ProjectFieldOptionRef(id="priority_p3", name="P3"),
            ),
        ),
        ProjectFieldRef(id="FIELD_Complexity", name="Complexity", data_type="NUMBER"),
        ProjectFieldRef(
            id="FIELD_Type",
            name="Type",
            data_type="SINGLE_SELECT",
            options=(
                ProjectFieldOptionRef(id="type_feature", name="feature"),
                ProjectFieldOptionRef(id="type_bug", name="bug"),
                ProjectFieldOptionRef(id="type_refactor", name="refactor"),
                ProjectFieldOptionRef(id="type_chore", name="chore"),
            ),
        ),
    ]


def _remote_issue(config: BlackcellConfig) -> IssueRef:
    return IssueRef(
        id="I_123",
        number=5,
        title="First issue",
        url="https://example.test/issues/5",
        state="OPEN",
        repository=config.repository,
        body="human notes",
    )


def _draft_pull_request(
    config: BlackcellConfig,
    contract: PlanContract,
    issue: IssueRef,
) -> PullRequestRef:
    issue_plan = _contract_issue(contract)
    return PullRequestRef(
        id="PR_1",
        number=12,
        title=issue_plan.github_title,
        url="https://example.test/pull/12",
        state="OPEN",
        is_draft=True,
        base_ref_name="main",
        head_ref_name="feature/bcp-0001",
        head_ref_oid="HEAD",
        repository=config.repository,
        body=render_pull_request_body(
            issue_plan,
            issue_number=issue.number,
            head_ref_name="feature/bcp-0001",
        ),
    )


def _contract_issue(contract: PlanContract) -> IssuePlan:
    return contract.issues[0]


def _pull_request_project_item(
    config: BlackcellConfig,
    pull_request: PullRequestRef,
) -> ProjectItemRef:
    return ProjectItemRef(
        id=f"PVTI_{pull_request.id}",
        type="PULL_REQUEST",
        is_archived=False,
        project=config.project,
        content_id=pull_request.id,
        content_title=pull_request.title,
        content_url=pull_request.url,
        content_type="PullRequest",
    )


def _option_name(field: ProjectFieldRef, option_id: str) -> str | None:
    for option in field.options:
        if option.id == option_id:
            return option.name
    return None


def _git_state(
    *,
    dirty: bool = False,
    upstream: bool = True,
    upstream_oid: str = "HEAD",
) -> GitState:
    return GitState(
        branch="feature/bcp-0001",
        head_oid="HEAD",
        upstream_ref="origin/feature/bcp-0001" if upstream else None,
        upstream_oid=upstream_oid if upstream else None,
        dirty=dirty,
    )


def _passing_checks(names: tuple[str, ...], start: Path | None) -> tuple[CheckResult, ...]:
    return tuple(CheckResult(name=name, command=None, passed=True) for name in names)


def _failing_checks(names: tuple[str, ...], start: Path | None) -> tuple[CheckResult, ...]:
    return tuple(
        CheckResult(
            name=name,
            command=None,
            passed=False,
            exit_code=1,
            message="failed",
        )
        for name in names
    )


def _init_git_repo(path: Path) -> None:
    for command in (
        ("git", "init", "-b", "feature/bcp-0001"),
        ("git", "config", "user.email", "test@example.test"),
        ("git", "config", "user.name", "BlackCell Test"),
        ("git", "add", "."),
        ("git", "commit", "-m", "initial"),
    ):
        subprocess.run(command, cwd=path, check=True, capture_output=True, text=True)


def _contract_yaml(
    *,
    status: str = "Todo",
    required_checks: tuple[str, ...] = (),
) -> str:
    pr_policy = ""
    if required_checks:
        checks = "\n".join(f"    - {check}" for check in required_checks)
        pr_policy = f"""
pr_policy:
  required_checks:
{checks}
"""

    return f"""
version: 1
project:
  key: BCP
  name: BlackCell
{pr_policy}
global:
  acceptance_criteria:
    - global ac
  definition_of_ready:
    - global ready
  definition_of_done:
    - global done
issues:
  - key: BCP-0001
    title: First issue
    type: feature
    status: {status}
    priority: P0
    complexity: 5
    scope:
      - first scope
    context:
      - first context
    change_spec:
      - first change
  - key: BCP-0002
    title: Second issue
    type: bug
    status: Backlog
    priority: P1
    complexity: 3
"""
