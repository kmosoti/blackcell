from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from blackcell.config.models import BlackcellConfig
from blackcell.control_plane.cache import (
    ControlPlaneSyncCache,
    SyncCacheEntry,
    now_timestamp,
)
from blackcell.control_plane.models import IssuePlan, PlanContract
from blackcell.control_plane.rendering import (
    extract_prior_remote_body,
    has_blackcell_issue_marker,
    issue_body_digest,
    issue_contract_digest,
    render_issue_body,
)
from blackcell.models import IssueRef, ProjectItemRef
from blackcell.providers import CreateIssueRequest, ProjectProvider


class SyncActionType(StrEnum):
    CREATE_ISSUE = "create_issue"
    UPDATE_ISSUE = "update_issue"
    ATTACH_PROJECT_ITEM = "attach_project_item"
    ADOPT_ISSUE = "adopt_issue"
    NOOP = "noop"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SyncAction:
    type: SyncActionType
    issue_key: str
    title: str
    applied: bool
    message: str
    issue_id: str | None = None
    issue_number: int | None = None
    issue_url: str | None = None
    project_item_id: str | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class SyncResult:
    dry_run: bool
    apply: bool
    issue_count: int
    actions: tuple[SyncAction, ...]
    summary: dict[str, int]


@dataclass(frozen=True, slots=True)
class RenderedIssue:
    title: str
    body: str
    contract_digest: str
    body_digest: str
    prior_remote_digest: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteDiscovery:
    issue: IssueRef
    source: str
    prior_remote_body: str | None = None


def sync_issues(
    *,
    contract: PlanContract,
    config: BlackcellConfig,
    provider: ProjectProvider,
    start: Path | None = None,
    cache_path: Path | None = None,
    apply_changes: bool = False,
    issue_key: str | None = None,
    refresh_cache: bool = False,
) -> SyncResult:
    issues = _selected_issues(contract, issue_key)
    project_items = provider.list_project_items(first=100)
    item_by_content_id = _project_items_by_content_id(project_items)
    cache = ControlPlaneSyncCache.open(
        start=start,
        path=cache_path,
        create=apply_changes,
    )

    actions: list[SyncAction] = []
    try:
        for issue in issues:
            issue_actions, project_items = _sync_issue(
                contract=contract,
                issue=issue,
                config=config,
                provider=provider,
                cache=cache,
                project_items=project_items,
                item_by_content_id=item_by_content_id,
                apply_changes=apply_changes,
                refresh_cache=refresh_cache,
            )
            item_by_content_id = _project_items_by_content_id(project_items)
            actions.extend(issue_actions)
    finally:
        if cache is not None:
            cache.close()

    return SyncResult(
        dry_run=not apply_changes,
        apply=apply_changes,
        issue_count=len(issues),
        actions=tuple(actions),
        summary=_summary(actions),
    )


def _sync_issue(
    *,
    contract: PlanContract,
    issue: IssuePlan,
    config: BlackcellConfig,
    provider: ProjectProvider,
    cache: ControlPlaneSyncCache | None,
    project_items: list[ProjectItemRef],
    item_by_content_id: dict[str, ProjectItemRef],
    apply_changes: bool,
    refresh_cache: bool,
) -> tuple[list[SyncAction], list[ProjectItemRef]]:
    actions: list[SyncAction] = []
    repository_id = _repository_cache_id(config)
    project_id = config.project.id
    cache_entry = None
    if cache is not None and not refresh_cache:
        cache_entry = cache.get(issue.key, repository_id=repository_id, project_id=project_id)

    discovery: RemoteDiscovery | None = None
    if cache_entry is not None:
        remote_issue = provider.read_issue_by_id(cache_entry.issue_id)
        if remote_issue is not None:
            discovery = RemoteDiscovery(
                issue=remote_issue,
                source="cache",
                prior_remote_body=extract_prior_remote_body(remote_issue.body or ""),
            )

    if discovery is None:
        discovery = _discover_remote_issue(provider, issue)
        if discovery is not None:
            actions.append(
                SyncAction(
                    type=SyncActionType.ADOPT_ISSUE,
                    issue_key=issue.key,
                    title=issue.github_title,
                    issue_id=discovery.issue.id,
                    issue_number=discovery.issue.number,
                    issue_url=discovery.issue.url,
                    applied=apply_changes,
                    message=f"{'adopted' if apply_changes else 'would adopt'} remote issue",
                    source=discovery.source,
                )
            )

    if discovery is None:
        rendered = _render(contract, issue)
        if not apply_changes:
            return [
                SyncAction(
                    type=SyncActionType.CREATE_ISSUE,
                    issue_key=issue.key,
                    title=issue.github_title,
                    applied=False,
                    message="would create GitHub issue",
                )
            ], project_items

        remote_issue = provider.create_issue(
            CreateIssueRequest(title=rendered.title, body=rendered.body)
        )
        actions.append(
            SyncAction(
                type=SyncActionType.CREATE_ISSUE,
                issue_key=issue.key,
                title=issue.github_title,
                issue_id=remote_issue.id,
                issue_number=remote_issue.number,
                issue_url=remote_issue.url,
                applied=True,
                message="created GitHub issue",
            )
        )
        project_items = provider.list_project_items(first=100)
        item_by_content_id = _project_items_by_content_id(project_items)
        project_item = item_by_content_id.get(remote_issue.id)
        if project_item is None:
            project_item = provider.add_project_item_by_id(remote_issue.id)
            project_items.append(project_item)
            actions.append(
                _attach_action(
                    issue=issue,
                    remote_issue=remote_issue,
                    project_item=project_item,
                    applied=True,
                )
            )

        _write_cache(
            cache=cache,
            config=config,
            issue=issue,
            remote_issue=remote_issue,
            rendered=rendered,
            project_item_id=project_item.id if project_item else None,
            adoption_source=None,
        )
        return actions, project_items

    remote_issue = discovery.issue
    prior_remote_body = discovery.prior_remote_body
    rendered = _render(contract, issue, prior_remote_body=prior_remote_body)
    project_item = item_by_content_id.get(remote_issue.id)

    if remote_issue.title != rendered.title or (remote_issue.body or "") != rendered.body:
        if apply_changes:
            remote_issue = provider.update_issue(
                issue_id=remote_issue.id,
                title=rendered.title,
                body=rendered.body,
            )
        actions.append(
            SyncAction(
                type=SyncActionType.UPDATE_ISSUE,
                issue_key=issue.key,
                title=issue.github_title,
                issue_id=remote_issue.id,
                issue_number=remote_issue.number,
                issue_url=remote_issue.url,
                applied=apply_changes,
                message=f"{'updated' if apply_changes else 'would update'} GitHub issue",
            )
        )

    if project_item is None:
        if apply_changes:
            project_item = provider.add_project_item_by_id(remote_issue.id)
            project_items.append(project_item)
        actions.append(
            _attach_action(
                issue=issue,
                remote_issue=remote_issue,
                project_item=project_item,
                applied=apply_changes,
            )
        )

    if not any(
        action.type in {SyncActionType.UPDATE_ISSUE, SyncActionType.ATTACH_PROJECT_ITEM}
        for action in actions
        if action.issue_key == issue.key
    ):
        actions.append(
            SyncAction(
                type=SyncActionType.NOOP,
                issue_key=issue.key,
                title=issue.github_title,
                issue_id=remote_issue.id,
                issue_number=remote_issue.number,
                issue_url=remote_issue.url,
                project_item_id=project_item.id if project_item else None,
                applied=False,
                message="issue already matches rendered contract and project attachment",
                source=discovery.source,
            )
        )

    if apply_changes:
        _write_cache(
            cache=cache,
            config=config,
            issue=issue,
            remote_issue=remote_issue,
            rendered=rendered,
            project_item_id=project_item.id if project_item else None,
            adoption_source=discovery.source if discovery.source != "cache" else None,
        )

    return actions, project_items


def _discover_remote_issue(provider: ProjectProvider, issue: IssuePlan) -> RemoteDiscovery | None:
    marker_matches = provider.find_issues_by_blackcell_marker(issue.key)
    if len(marker_matches) > 1:
        raise ValueError(f"multiple GitHub issues contain BlackCell marker for {issue.key}")
    if marker_matches:
        remote_issue = marker_matches[0]
        return RemoteDiscovery(
            issue=remote_issue,
            source="body_marker",
            prior_remote_body=extract_prior_remote_body(remote_issue.body or ""),
        )

    title_matches = provider.find_issues_by_exact_title(issue.github_title)
    if len(title_matches) > 1:
        raise ValueError(
            f"multiple GitHub issues match title for {issue.key}: {issue.github_title}"
        )
    if title_matches:
        remote_issue = title_matches[0]
        prior_remote_body = None
        if not has_blackcell_issue_marker(remote_issue.body or "", issue.key):
            prior_remote_body = remote_issue.body
        return RemoteDiscovery(
            issue=remote_issue,
            source="exact_title",
            prior_remote_body=prior_remote_body,
        )
    return None


def _render(
    contract: PlanContract,
    issue: IssuePlan,
    *,
    prior_remote_body: str | None = None,
) -> RenderedIssue:
    body = render_issue_body(contract, issue, prior_remote_body=prior_remote_body)
    prior_remote_digest = issue_body_digest(prior_remote_body) if prior_remote_body else None
    return RenderedIssue(
        title=issue.github_title,
        body=body,
        contract_digest=issue_contract_digest(contract, issue),
        body_digest=issue_body_digest(body),
        prior_remote_digest=prior_remote_digest,
    )


def _write_cache(
    *,
    cache: ControlPlaneSyncCache | None,
    config: BlackcellConfig,
    issue: IssuePlan,
    remote_issue: IssueRef,
    rendered: RenderedIssue,
    project_item_id: str | None,
    adoption_source: str | None,
) -> None:
    if cache is None:
        return
    adopted_at = now_timestamp() if adoption_source else None
    cache.upsert(
        SyncCacheEntry(
            issue_key=issue.key,
            repository_id=_repository_cache_id(config),
            project_id=config.project.id,
            issue_id=remote_issue.id,
            issue_number=remote_issue.number,
            issue_url=remote_issue.url,
            project_item_id=project_item_id,
            contract_digest=rendered.contract_digest,
            body_digest=rendered.body_digest,
            synced_at=now_timestamp(),
            adopted_at=adopted_at,
            adoption_source=adoption_source,
            prior_remote_digest=rendered.prior_remote_digest,
        )
    )


def _attach_action(
    *,
    issue: IssuePlan,
    remote_issue: IssueRef,
    project_item: ProjectItemRef | None,
    applied: bool,
) -> SyncAction:
    return SyncAction(
        type=SyncActionType.ATTACH_PROJECT_ITEM,
        issue_key=issue.key,
        title=issue.github_title,
        issue_id=remote_issue.id,
        issue_number=remote_issue.number,
        issue_url=remote_issue.url,
        project_item_id=project_item.id if project_item else None,
        applied=applied,
        message=f"{'attached' if applied else 'would attach'} issue to GitHub Project",
    )


def _selected_issues(contract: PlanContract, issue_key: str | None) -> tuple[IssuePlan, ...]:
    if issue_key is None:
        return contract.issues
    for issue in contract.issues:
        if issue.key == issue_key:
            return (issue,)
    raise ValueError(f"unknown issue key: {issue_key}")


def _project_items_by_content_id(items: list[ProjectItemRef]) -> dict[str, ProjectItemRef]:
    return {
        item.content_id: item
        for item in items
        if item.content_id is not None and not item.is_archived
    }


def _repository_cache_id(config: BlackcellConfig) -> str:
    return config.repository.node_id or config.repository.name_with_owner


def _summary(actions: list[SyncAction]) -> dict[str, int]:
    summary = {action_type.value: 0 for action_type in SyncActionType}
    for action in actions:
        summary[action.type.value] += 1
    return summary
