from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from blackcell.config.models import BlackcellConfig
from blackcell.control_plane.cache import (
    ControlPlaneSyncCache,
    PullRequestCacheEntry,
    now_timestamp,
)
from blackcell.control_plane.git import (
    CheckResult,
    GitState,
    inspect_git_state,
    run_required_checks,
)
from blackcell.control_plane.models import IssuePlan, IssueStatus, PlanContract
from blackcell.control_plane.project_fields import (
    current_field_value,
    desired_project_field_values,
    field_value_matches,
    missing_required_fields,
    project_field_value,
)
from blackcell.control_plane.rendering import (
    pull_request_body_digest,
    render_pull_request_body,
)
from blackcell.models import IssueRef, ProjectFieldRef, ProjectItemRef, PullRequestRef
from blackcell.providers import CreatePullRequestRequest, ProjectProvider

GitInspector = Callable[[Path | None], GitState]
CheckRunner = Callable[[tuple[str, ...], Path | None], tuple[CheckResult, ...]]


class PullRequestCommand(StrEnum):
    STATUS = "status"
    SYNC = "sync"
    READY = "ready"


class PullRequestWorkflowState(StrEnum):
    NEEDS_CHANGES = "needs_changes"
    NEEDS_PUSH = "needs_push"
    NEEDS_DRAFT_PR = "needs_draft_pr"
    DRAFT_OPEN = "draft_open"
    READY_BLOCKED = "ready_blocked"
    REVIEW_READY = "review_ready"


class PullRequestActionType(StrEnum):
    CREATE_PULL_REQUEST = "create_pull_request"
    UPDATE_PULL_REQUEST = "update_pull_request"
    ATTACH_PROJECT_ITEM = "attach_project_item"
    UPDATE_PROJECT_ITEM_FIELD = "update_project_item_field"
    MARK_READY_FOR_REVIEW = "mark_ready_for_review"
    NOOP = "noop"


@dataclass(frozen=True, slots=True)
class PullRequestAction:
    type: PullRequestActionType
    issue_key: str
    applied: bool
    message: str
    pull_request_id: str | None = None
    pull_request_number: int | None = None
    pull_request_url: str | None = None
    project_item_id: str | None = None
    field_name: str | None = None
    field_value: str | int | float | None = None


@dataclass(frozen=True, slots=True)
class PullRequestWorkflowResult:
    issue_key: str
    state: PullRequestWorkflowState
    dry_run: bool
    apply: bool
    blockers: tuple[str, ...]
    next_commands: tuple[str, ...]
    actions: tuple[PullRequestAction, ...]
    checks: tuple[CheckResult, ...]
    git: GitState
    issue: IssueRef | None
    pull_request: PullRequestRef | None


@dataclass(frozen=True, slots=True)
class RenderedPullRequest:
    title: str
    body: str
    body_digest: str


def run_pull_request_workflow(
    *,
    contract: PlanContract,
    config: BlackcellConfig,
    provider: ProjectProvider,
    issue_key: str,
    command: PullRequestCommand,
    apply_changes: bool = False,
    run_checks: bool = False,
    base_ref_name: str = "main",
    start: Path | None = None,
    cache_path: Path | None = None,
    git_state: GitState | None = None,
    git_inspector: GitInspector = inspect_git_state,
    check_runner: CheckRunner = run_required_checks,
) -> PullRequestWorkflowResult:
    issue = _issue_by_key(contract, issue_key)
    git = git_state or git_inspector(start)
    local_blockers = _local_blockers(git)
    if local_blockers:
        state = PullRequestWorkflowState.NEEDS_CHANGES
        if not git.dirty and git.branch:
            state = PullRequestWorkflowState.NEEDS_PUSH
        return _result(
            issue_key=issue.key,
            state=state,
            apply_changes=apply_changes,
            blockers=tuple(local_blockers),
            next_commands=_next_commands(state, issue.key, git),
            actions=(),
            checks=(),
            git=git,
            issue=None,
            pull_request=None,
        )

    cache = ControlPlaneSyncCache.open(start=start, path=cache_path, create=apply_changes)
    try:
        project_items = provider.list_project_items(first=100)
        item_by_content_id = _project_items_by_content_id(project_items)
        remote_issue = _discover_issue(
            provider=provider,
            cache=cache,
            config=config,
            issue=issue,
        )
        if remote_issue is None:
            return _result(
                issue_key=issue.key,
                state=PullRequestWorkflowState.READY_BLOCKED,
                apply_changes=apply_changes,
                blockers=("issue_not_synced",),
                next_commands=(f"blackcell control-plane sync --issue-key {issue.key} --apply",),
                actions=(),
                checks=(),
                git=git,
                issue=None,
                pull_request=None,
            )

        pull_request = _discover_pull_request(
            provider=provider,
            cache=cache,
            config=config,
            issue=issue,
            head_ref_name=git.branch or "",
        )
        rendered = _render_pull_request(
            issue=issue,
            remote_issue=remote_issue,
            head_ref_name=git.branch or "",
        )

        if pull_request is None:
            return _handle_missing_pull_request(
                command=command,
                apply_changes=apply_changes,
                provider=provider,
                cache=cache,
                config=config,
                issue=issue,
                remote_issue=remote_issue,
                rendered=rendered,
                git=git,
                base_ref_name=base_ref_name,
            )

        actions: list[PullRequestAction] = []
        project_item = item_by_content_id.get(pull_request.id)
        pull_request = _sync_existing_pull_request(
            provider=provider,
            issue=issue,
            pull_request=pull_request,
            rendered=rendered,
            project_item=project_item,
            actions=actions,
            apply_changes=apply_changes,
        )
        project_item_was_missing = project_item is None
        if project_item_was_missing and apply_changes:
            project_item = provider.add_project_item_by_id(pull_request.id)
        if project_item is None:
            actions.append(
                _action(
                    PullRequestActionType.ATTACH_PROJECT_ITEM,
                    issue,
                    pull_request,
                    applied=False,
                    message="would attach pull request to GitHub Project",
                )
            )
        elif project_item_was_missing and apply_changes:
            actions.append(
                _action(
                    PullRequestActionType.ATTACH_PROJECT_ITEM,
                    issue,
                    pull_request,
                    applied=True,
                    message="attached pull request to GitHub Project",
                    project_item_id=project_item.id,
                )
            )
        if project_item is not None:
            _sync_pull_request_project_item_fields(
                provider=provider,
                issue=issue,
                pull_request=pull_request,
                project_item=project_item,
                actions=actions,
                apply_changes=apply_changes,
            )

        checks: tuple[CheckResult, ...] = ()
        blockers: list[str] = []
        if command is PullRequestCommand.READY:
            checks = check_runner(contract.pr_policy.required_checks, start)
            blockers.extend(_ready_blockers(issue=issue, checks=checks, pull_request=pull_request))
            if blockers:
                state = PullRequestWorkflowState.READY_BLOCKED
            elif pull_request.is_draft:
                if apply_changes:
                    pull_request = provider.mark_pull_request_ready_for_review(pull_request.id)
                actions.append(
                    _action(
                        PullRequestActionType.MARK_READY_FOR_REVIEW,
                        issue,
                        pull_request,
                        applied=apply_changes,
                        message=(
                            "marked pull request ready for review"
                            if apply_changes
                            else "would mark pull request ready for review"
                        ),
                    )
                )
                state = (
                    PullRequestWorkflowState.REVIEW_READY
                    if apply_changes
                    else PullRequestWorkflowState.DRAFT_OPEN
                )
            else:
                state = PullRequestWorkflowState.REVIEW_READY
        else:
            if run_checks:
                checks = check_runner(contract.pr_policy.required_checks, start)
            state = (
                PullRequestWorkflowState.DRAFT_OPEN
                if pull_request.is_draft
                else PullRequestWorkflowState.REVIEW_READY
            )

        if not actions and not blockers:
            actions.append(
                _action(
                    PullRequestActionType.NOOP,
                    issue,
                    pull_request,
                    applied=False,
                    message="pull request workflow is already up to date",
                    project_item_id=project_item.id if project_item else None,
                )
            )

        if apply_changes:
            _write_cache(
                cache=cache,
                config=config,
                issue=issue,
                remote_issue=remote_issue,
                pull_request=pull_request,
                rendered=rendered,
                project_item_id=project_item.id if project_item else None,
                ready=state is PullRequestWorkflowState.REVIEW_READY,
            )

        return _result(
            issue_key=issue.key,
            state=state,
            apply_changes=apply_changes,
            blockers=tuple(blockers),
            next_commands=_next_commands(state, issue.key, git),
            actions=tuple(actions),
            checks=checks,
            git=git,
            issue=remote_issue,
            pull_request=pull_request,
        )
    finally:
        if cache is not None:
            cache.close()


def _handle_missing_pull_request(
    *,
    command: PullRequestCommand,
    apply_changes: bool,
    provider: ProjectProvider,
    cache: ControlPlaneSyncCache | None,
    config: BlackcellConfig,
    issue: IssuePlan,
    remote_issue: IssueRef,
    rendered: RenderedPullRequest,
    git: GitState,
    base_ref_name: str,
) -> PullRequestWorkflowResult:
    if command is PullRequestCommand.READY:
        return _result(
            issue_key=issue.key,
            state=PullRequestWorkflowState.NEEDS_DRAFT_PR,
            apply_changes=apply_changes,
            blockers=("missing_draft_pull_request",),
            next_commands=_next_commands(PullRequestWorkflowState.NEEDS_DRAFT_PR, issue.key, git),
            actions=(),
            checks=(),
            git=git,
            issue=remote_issue,
            pull_request=None,
        )

    action = PullRequestAction(
        type=PullRequestActionType.CREATE_PULL_REQUEST,
        issue_key=issue.key,
        applied=apply_changes,
        message=(
            "created draft pull request" if apply_changes else "would create draft pull request"
        ),
    )
    if not apply_changes:
        return _result(
            issue_key=issue.key,
            state=PullRequestWorkflowState.NEEDS_DRAFT_PR,
            apply_changes=False,
            blockers=(),
            next_commands=_next_commands(PullRequestWorkflowState.NEEDS_DRAFT_PR, issue.key, git),
            actions=(action,),
            checks=(),
            git=git,
            issue=remote_issue,
            pull_request=None,
        )

    pull_request = provider.create_pull_request(
        CreatePullRequestRequest(
            title=rendered.title,
            body=rendered.body,
            base_ref_name=base_ref_name,
            head_ref_name=git.branch or "",
            draft=True,
        )
    )
    project_item = provider.add_project_item_by_id(pull_request.id)
    actions = [
        _action(
            PullRequestActionType.CREATE_PULL_REQUEST,
            issue,
            pull_request,
            applied=True,
            message="created draft pull request",
        ),
        _action(
            PullRequestActionType.ATTACH_PROJECT_ITEM,
            issue,
            pull_request,
            applied=True,
            message="attached pull request to GitHub Project",
            project_item_id=project_item.id,
        ),
    ]
    _sync_pull_request_project_item_fields(
        provider=provider,
        issue=issue,
        pull_request=pull_request,
        project_item=project_item,
        actions=actions,
        apply_changes=True,
    )
    _write_cache(
        cache=cache,
        config=config,
        issue=issue,
        remote_issue=remote_issue,
        pull_request=pull_request,
        rendered=rendered,
        project_item_id=project_item.id,
        ready=False,
    )
    return _result(
        issue_key=issue.key,
        state=PullRequestWorkflowState.DRAFT_OPEN,
        apply_changes=True,
        blockers=(),
        next_commands=_next_commands(PullRequestWorkflowState.DRAFT_OPEN, issue.key, git),
        actions=tuple(actions),
        checks=(),
        git=git,
        issue=remote_issue,
        pull_request=pull_request,
    )


def _sync_existing_pull_request(
    *,
    provider: ProjectProvider,
    issue: IssuePlan,
    pull_request: PullRequestRef,
    rendered: RenderedPullRequest,
    project_item: ProjectItemRef | None,
    actions: list[PullRequestAction],
    apply_changes: bool,
) -> PullRequestRef:
    if pull_request.title != rendered.title or (pull_request.body or "") != rendered.body:
        if apply_changes:
            pull_request = provider.update_pull_request(
                pull_request_id=pull_request.id,
                title=rendered.title,
                body=rendered.body,
            )
        actions.append(
            _action(
                PullRequestActionType.UPDATE_PULL_REQUEST,
                issue,
                pull_request,
                applied=apply_changes,
                message=("updated pull request" if apply_changes else "would update pull request"),
            )
        )

    return pull_request


def _sync_pull_request_project_item_fields(
    *,
    provider: ProjectProvider,
    issue: IssuePlan,
    pull_request: PullRequestRef,
    project_item: ProjectItemRef,
    actions: list[PullRequestAction],
    apply_changes: bool,
) -> None:
    fields = provider.list_project_fields(first=50)
    if missing_required_fields(fields):
        raise ValueError(
            "GitHub Project is missing required contract fields; "
            f"run blackcell control-plane sync --issue-key {issue.key} --apply"
        )
    field_by_name = {field.name: field for field in fields}
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
            _project_field_action(
                issue=issue,
                pull_request=pull_request,
                project_item=project_item,
                field=field,
                desired_value=desired_value,
                applied=apply_changes,
            )
        )


def _discover_issue(
    *,
    provider: ProjectProvider,
    cache: ControlPlaneSyncCache | None,
    config: BlackcellConfig,
    issue: IssuePlan,
) -> IssueRef | None:
    repository_id = _repository_cache_id(config)
    if cache is not None:
        cache_entry = cache.get(
            issue.key,
            repository_id=repository_id,
            project_id=config.project.id,
        )
        if cache_entry is not None:
            remote_issue = provider.read_issue_by_id(cache_entry.issue_id)
            if remote_issue is not None:
                return remote_issue

    marker_matches = provider.find_issues_by_blackcell_marker(issue.key)
    if len(marker_matches) > 1:
        raise ValueError(f"multiple GitHub issues contain BlackCell marker for {issue.key}")
    if marker_matches:
        return marker_matches[0]

    title_matches = provider.find_issues_by_exact_title(issue.github_title)
    if len(title_matches) > 1:
        raise ValueError(
            f"multiple GitHub issues match title for {issue.key}: {issue.github_title}"
        )
    if title_matches:
        return title_matches[0]
    return None


def _discover_pull_request(
    *,
    provider: ProjectProvider,
    cache: ControlPlaneSyncCache | None,
    config: BlackcellConfig,
    issue: IssuePlan,
    head_ref_name: str,
) -> PullRequestRef | None:
    repository_id = _repository_cache_id(config)
    if cache is not None:
        cache_entry = cache.get_pull_request(
            issue.key,
            repository_id=repository_id,
            project_id=config.project.id,
        )
        if cache_entry is not None:
            pull_request = provider.read_pull_request_by_id(cache_entry.pull_request_id)
            if pull_request is not None:
                return pull_request

    marker_matches = provider.find_pull_requests_by_blackcell_marker(issue.key)
    if len(marker_matches) > 1:
        raise ValueError(f"multiple GitHub pull requests contain BlackCell marker for {issue.key}")
    if marker_matches:
        return marker_matches[0]

    head_matches = provider.find_pull_requests_by_head(head_ref_name)
    if len(head_matches) > 1:
        raise ValueError(f"multiple GitHub pull requests use head branch {head_ref_name}")
    if head_matches:
        return head_matches[0]
    return None


def _render_pull_request(
    *,
    issue: IssuePlan,
    remote_issue: IssueRef,
    head_ref_name: str,
) -> RenderedPullRequest:
    body = render_pull_request_body(
        issue,
        issue_number=remote_issue.number,
        head_ref_name=head_ref_name,
    )
    return RenderedPullRequest(
        title=issue.github_title,
        body=body,
        body_digest=pull_request_body_digest(body),
    )


def _ready_blockers(
    *,
    issue: IssuePlan,
    checks: tuple[CheckResult, ...],
    pull_request: PullRequestRef,
) -> list[str]:
    blockers: list[str] = []
    if issue.status is not IssueStatus.REVIEW_REQUIRED:
        blockers.append("issue_status_not_review_required")
    for check in checks:
        if not check.passed:
            blockers.append(f"check_failed:{check.name}")
    if not pull_request.is_draft:
        return blockers
    return blockers


def _local_blockers(git: GitState) -> list[str]:
    blockers: list[str] = []
    if git.branch is None:
        blockers.append("detached_head")
    if git.dirty:
        blockers.append("dirty_worktree")
    if git.branch and not git.pushed:
        blockers.append("branch_not_pushed")
    return blockers


def _next_commands(
    state: PullRequestWorkflowState,
    issue_key: str,
    git: GitState,
) -> tuple[str, ...]:
    if state is PullRequestWorkflowState.NEEDS_CHANGES:
        return ("git status", "git add <paths>", "git commit")
    if state is PullRequestWorkflowState.NEEDS_PUSH:
        if git.upstream_ref:
            return ("git push",)
        return (f"git push -u origin {git.branch or '<branch>'}",)
    if state is PullRequestWorkflowState.NEEDS_DRAFT_PR:
        return (f"blackcell control-plane pr sync --issue-key {issue_key} --apply",)
    if state is PullRequestWorkflowState.DRAFT_OPEN:
        return (f"blackcell control-plane pr ready --issue-key {issue_key} --apply",)
    return ()


def _write_cache(
    *,
    cache: ControlPlaneSyncCache | None,
    config: BlackcellConfig,
    issue: IssuePlan,
    remote_issue: IssueRef,
    pull_request: PullRequestRef,
    rendered: RenderedPullRequest,
    project_item_id: str | None,
    ready: bool,
) -> None:
    if cache is None:
        return
    cache.upsert_pull_request(
        PullRequestCacheEntry(
            issue_key=issue.key,
            repository_id=_repository_cache_id(config),
            project_id=config.project.id,
            pull_request_id=pull_request.id,
            pull_request_number=pull_request.number,
            pull_request_url=pull_request.url,
            issue_id=remote_issue.id,
            project_item_id=project_item_id,
            base_ref_name=pull_request.base_ref_name,
            head_ref_name=pull_request.head_ref_name,
            head_ref_oid=pull_request.head_ref_oid,
            body_digest=rendered.body_digest,
            is_draft=pull_request.is_draft,
            synced_at=now_timestamp(),
            ready_at=now_timestamp() if ready else None,
        )
    )


def _action(
    action_type: PullRequestActionType,
    issue: IssuePlan,
    pull_request: PullRequestRef,
    *,
    applied: bool,
    message: str,
    project_item_id: str | None = None,
) -> PullRequestAction:
    return PullRequestAction(
        type=action_type,
        issue_key=issue.key,
        pull_request_id=pull_request.id,
        pull_request_number=pull_request.number,
        pull_request_url=pull_request.url,
        project_item_id=project_item_id,
        applied=applied,
        message=message,
    )


def _project_field_action(
    *,
    issue: IssuePlan,
    pull_request: PullRequestRef,
    project_item: ProjectItemRef,
    field: ProjectFieldRef,
    desired_value: str | int,
    applied: bool,
) -> PullRequestAction:
    return PullRequestAction(
        type=PullRequestActionType.UPDATE_PROJECT_ITEM_FIELD,
        issue_key=issue.key,
        pull_request_id=pull_request.id,
        pull_request_number=pull_request.number,
        pull_request_url=pull_request.url,
        project_item_id=project_item.id,
        applied=applied,
        message=f"{'updated' if applied else 'would update'} GitHub Project {field.name}",
        field_name=field.name,
        field_value=desired_value,
    )


def _result(
    *,
    issue_key: str,
    state: PullRequestWorkflowState,
    apply_changes: bool,
    blockers: tuple[str, ...],
    next_commands: tuple[str, ...],
    actions: tuple[PullRequestAction, ...],
    checks: tuple[CheckResult, ...],
    git: GitState,
    issue: IssueRef | None,
    pull_request: PullRequestRef | None,
) -> PullRequestWorkflowResult:
    return PullRequestWorkflowResult(
        issue_key=issue_key,
        state=state,
        dry_run=not apply_changes,
        apply=apply_changes,
        blockers=blockers,
        next_commands=next_commands,
        actions=actions,
        checks=checks,
        git=git,
        issue=issue,
        pull_request=pull_request,
    )


def _project_items_by_content_id(items: list[ProjectItemRef]) -> dict[str, ProjectItemRef]:
    return {
        item.content_id: item
        for item in items
        if item.content_id is not None and not item.is_archived
    }


def _issue_by_key(contract: PlanContract, issue_key: str) -> IssuePlan:
    for issue in contract.issues:
        if issue.key == issue_key:
            return issue
    raise ValueError(f"unknown issue key: {issue_key}")


def _repository_cache_id(config: BlackcellConfig) -> str:
    return config.repository.node_id or config.repository.name_with_owner
