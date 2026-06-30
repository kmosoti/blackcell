import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from blackcell.cli.app import app
from blackcell.config import BlackcellConfig, ProjectRef, RepositoryRef, write_config
from blackcell.control_plane import (
    ControlPlaneSyncCache,
    SyncCacheEntry,
    extract_contract_digest,
    extract_prior_remote_body,
    has_blackcell_issue_marker,
    issue_body_digest,
    load_contract,
    load_github_capabilities,
    render_issue_body,
    write_github_capabilities,
)
from blackcell.control_plane.sync import sync_issues
from blackcell.models import IssueRef, ProjectItemRef
from blackcell.providers import CreateIssueRequest

runner = CliRunner()


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

    assert [action.type.value for action in result.actions] == ["create_issue"]
    assert result.actions[0].applied is False
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

    assert [action.type.value for action in created.actions] == ["create_issue"]
    assert [action.type.value for action in noop.actions] == ["noop"]
    assert len(provider.created_requests) == 1


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

    assert [action.type.value for action in first.actions] == [
        "adopt_issue",
        "update_issue",
        "attach_project_item",
    ]
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
        project_items: list[ProjectItemRef] | None = None,
    ) -> None:
        self.config = config
        self.issues = {issue.id: issue for issue in issues or []}
        self.project_items = list(project_items or [])
        self.created_requests: list[CreateIssueRequest] = []
        self.updated_requests: list[tuple[str, str, str]] = []
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

    def list_project_items(self, *, first: int = 20) -> list[ProjectItemRef]:
        return self.project_items[:first]

    def add_project_item_by_id(self, content_id: str) -> ProjectItemRef:
        self.attached_content_ids.append(content_id)
        issue = self.issues[content_id]
        item = self._project_item(issue)
        self.project_items.append(item)
        return item

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
issues:
  - key: BCP-0001
    title: First issue
    type: feature
    status: Todo
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
