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
from blackcell.control_plane.project_fields import (
    current_field_value,
    desired_project_field_values,
    field_value_matches,
    missing_required_fields,
    project_field_specs,
    project_field_value,
)
from blackcell.control_plane.rendering import (
    extract_prior_remote_body,
    has_blackcell_issue_marker,
    issue_body_digest,
    issue_contract_digest,
    render_issue_body,
)
from blackcell.models import IssueRef, ProjectFieldRef, ProjectItemRef
from blackcell.providers import (
    CreateIssueRequest,
    CreateProjectFieldRequest,
    ProjectProvider,
)


class SyncActionType(StrEnum):
    CREATE_ISSUE = "create_issue"
    UPDATE_ISSUE = "update_issue"
    ATTACH_PROJECT_ITEM = "attach_project_item"
    CREATE_PROJECT_FIELD = "create_project_field"
    UPDATE_PROJECT_FIELD = "update_project_field"
    UPDATE_PROJECT_ITEM_FIELD = "update_project_item_field"
    ARCHIVE_PROJECT_ITEM = "archive_project_item"
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
    field_name: str | None = None
    field_value: str | int | float | None = None
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


@dataclass(frozen=True, slots=True)
class IssueSyncOutcome:
    actions: tuple[SyncAction, ...]
    remote_issue: IssueRef | None
    project_item: ProjectItemRef | None
    project_items: list[ProjectItemRef]


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
    outcomes: list[IssueSyncOutcome] = []
    try:
        for issue in issues:
            outcome = _sync_issue(
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
            project_items = outcome.project_items
            item_by_content_id = _project_items_by_content_id(project_items)
            actions.extend(outcome.actions)
            outcomes.append(outcome)
        project_actions = _sync_project_representation(
            issues=issues,
            outcomes=outcomes,
            provider=provider,
            project_items=project_items,
            apply_changes=apply_changes,
            archive_unmanaged=issue_key is None,
        )
        actions.extend(project_actions)
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
) -> IssueSyncOutcome:
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
            return IssueSyncOutcome(
                actions=(
                    SyncAction(
                        type=SyncActionType.CREATE_ISSUE,
                        issue_key=issue.key,
                        title=issue.github_title,
                        applied=False,
                        message="would create GitHub issue",
                    ),
                ),
                remote_issue=None,
                project_item=None,
                project_items=project_items,
            )

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
        return IssueSyncOutcome(
            actions=tuple(actions),
            remote_issue=remote_issue,
            project_item=project_item,
            project_items=project_items,
        )

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

    return IssueSyncOutcome(
        actions=tuple(actions),
        remote_issue=remote_issue,
        project_item=project_item,
        project_items=project_items,
    )


def _sync_project_representation(
    *,
    issues: tuple[IssuePlan, ...],
    outcomes: list[IssueSyncOutcome],
    provider: ProjectProvider,
    project_items: list[ProjectItemRef],
    apply_changes: bool,
    archive_unmanaged: bool,
) -> tuple[SyncAction, ...]:
    actions: list[SyncAction] = []
    fields = provider.list_project_fields(first=50)
    field_actions, fields = _ensure_project_fields(
        provider=provider,
        fields=fields,
        apply_changes=apply_changes,
    )
    actions.extend(field_actions)
    if missing_required_fields(fields):
        return tuple(actions)

    field_by_name = {field.name: field for field in fields}
    item_by_content_id = _project_items_by_content_id(project_items)
    outcome_by_issue_key = {
        issue.key: outcome
        for issue, outcome in zip(issues, outcomes, strict=False)
        if outcome.remote_issue is not None
    }
    for issue in issues:
        outcome = outcome_by_issue_key.get(issue.key)
        if outcome is None or outcome.remote_issue is None:
            continue
        project_item = outcome.project_item or item_by_content_id.get(outcome.remote_issue.id)
        if project_item is None:
            continue
        actions.extend(
            _sync_issue_field_values(
                issue=issue,
                project_item=project_item,
                field_by_name=field_by_name,
                provider=provider,
                apply_changes=apply_changes,
            )
        )
    if archive_unmanaged:
        managed_issue_ids = {
            outcome.remote_issue.id for outcome in outcomes if outcome.remote_issue is not None
        }
        actions.extend(
            _archive_unmanaged_issue_items(
                project_items=project_items,
                managed_issue_ids=managed_issue_ids,
                provider=provider,
                apply_changes=apply_changes,
            )
        )
    return tuple(actions)


def _ensure_project_fields(
    *,
    provider: ProjectProvider,
    fields: list[ProjectFieldRef],
    apply_changes: bool,
) -> tuple[list[SyncAction], list[ProjectFieldRef]]:
    actions: list[SyncAction] = []
    field_by_name = {field.name: field for field in fields}
    ensured_fields = list(fields)
    for field_name, data_type, options in project_field_specs():
        field = field_by_name.get(field_name)
        if field is None:
            actions.append(
                SyncAction(
                    type=SyncActionType.CREATE_PROJECT_FIELD,
                    issue_key="project",
                    title=field_name,
                    applied=apply_changes,
                    message=(
                        f"{'created' if apply_changes else 'would create'} "
                        f"GitHub Project field {field_name}"
                    ),
                    field_name=field_name,
                )
            )
            if apply_changes:
                field = provider.create_project_field(
                    CreateProjectFieldRequest(
                        name=field_name,
                        data_type=data_type,
                        single_select_options=options,
                    )
                )
                ensured_fields.append(field)
                field_by_name[field.name] = field
            continue

        if field.data_type != data_type:
            raise ValueError(
                f"GitHub Project field {field_name} is {field.data_type}, expected {data_type}"
            )
        if data_type == "SINGLE_SELECT":
            existing_options = {option.name for option in field.options}
            missing_options = tuple(option for option in options if option not in existing_options)
            if missing_options:
                actions.append(
                    SyncAction(
                        type=SyncActionType.UPDATE_PROJECT_FIELD,
                        issue_key="project",
                        title=field_name,
                        applied=apply_changes,
                        message=(
                            f"{'updated' if apply_changes else 'would update'} "
                            f"GitHub Project field {field_name} options"
                        ),
                        field_name=field_name,
                        field_value=", ".join(missing_options),
                    )
                )
                if apply_changes:
                    field = provider.update_project_single_select_field_options(field, options)
                    ensured_fields = [
                        field if existing.id == field.id else existing
                        for existing in ensured_fields
                    ]
                    field_by_name[field.name] = field
    return actions, ensured_fields


def _sync_issue_field_values(
    *,
    issue: IssuePlan,
    project_item: ProjectItemRef,
    field_by_name: dict[str, ProjectFieldRef],
    provider: ProjectProvider,
    apply_changes: bool,
) -> tuple[SyncAction, ...]:
    actions: list[SyncAction] = []
    for field_name, desired_value in desired_project_field_values(issue):
        field = field_by_name[field_name]
        value = project_field_value(field, desired_value)
        current = current_field_value(project_item, field.id)
        if field_value_matches(current, desired_value):
            continue
        if apply_changes:
            provider.update_project_item_field_value(
                item_id=project_item.id,
                field_id=field.id,
                value=value,
            )
        actions.append(
            SyncAction(
                type=SyncActionType.UPDATE_PROJECT_ITEM_FIELD,
                issue_key=issue.key,
                title=issue.github_title,
                issue_id=project_item.content_id,
                issue_url=project_item.content_url,
                project_item_id=project_item.id,
                applied=apply_changes,
                message=(
                    f"{'updated' if apply_changes else 'would update'} GitHub Project {field_name}"
                ),
                field_name=field_name,
                field_value=desired_value,
            )
        )
    return tuple(actions)


def _archive_unmanaged_issue_items(
    *,
    project_items: list[ProjectItemRef],
    managed_issue_ids: set[str],
    provider: ProjectProvider,
    apply_changes: bool,
) -> tuple[SyncAction, ...]:
    actions: list[SyncAction] = []
    for item in project_items:
        if item.is_archived:
            continue
        if item.content_id is None or item.content_id in managed_issue_ids:
            continue
        if item.content_type != "Issue" and item.type != "ISSUE":
            continue
        if apply_changes:
            provider.archive_project_item(item.id)
        actions.append(
            SyncAction(
                type=SyncActionType.ARCHIVE_PROJECT_ITEM,
                issue_key="project",
                title=item.content_title or item.id,
                applied=apply_changes,
                message=(
                    f"{'archived' if apply_changes else 'would archive'} "
                    "unmanaged GitHub Project issue item"
                ),
                issue_id=item.content_id,
                issue_url=item.content_url,
                project_item_id=item.id,
            )
        )
    return tuple(actions)


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
