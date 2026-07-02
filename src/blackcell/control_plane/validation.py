from collections.abc import Iterable

from blackcell.control_plane.models import (
    IssuePlan,
    IssueStatus,
    PlanContract,
    ValidationLevel,
    ValidationMessage,
    ValidationResult,
)

ACTIVE_STATUSES = frozenset(
    {
        IssueStatus.IN_PROGRESS,
        IssueStatus.REVIEW_REQUIRED,
        IssueStatus.DONE,
    }
)

ALLOWED_STATUS_TRANSITIONS: dict[IssueStatus, frozenset[IssueStatus]] = {
    IssueStatus.BACKLOG: frozenset({IssueStatus.BACKLOG, IssueStatus.TODO}),
    IssueStatus.TODO: frozenset({IssueStatus.BACKLOG, IssueStatus.TODO, IssueStatus.IN_PROGRESS}),
    IssueStatus.IN_PROGRESS: frozenset(
        {IssueStatus.TODO, IssueStatus.IN_PROGRESS, IssueStatus.REVIEW_REQUIRED}
    ),
    IssueStatus.REVIEW_REQUIRED: frozenset(
        {IssueStatus.IN_PROGRESS, IssueStatus.REVIEW_REQUIRED, IssueStatus.DONE}
    ),
    IssueStatus.DONE: frozenset({IssueStatus.DONE, IssueStatus.REVIEW_REQUIRED}),
}


def validate_contract(contract: PlanContract) -> ValidationResult:
    messages: list[ValidationMessage] = []

    roadmaps = {roadmap.key: roadmap for roadmap in contract.roadmaps}
    epics = {epic.key: epic for epic in contract.epics}
    milestones = {milestone.key: milestone for milestone in contract.milestones}
    issues = {issue.key: issue for issue in contract.issues}

    _validate_unique("roadmaps", (roadmap.key for roadmap in contract.roadmaps), messages)
    _validate_unique("epics", (epic.key for epic in contract.epics), messages)
    _validate_unique("milestones", (milestone.key for milestone in contract.milestones), messages)
    _validate_unique("issues", (issue.key for issue in contract.issues), messages)
    _validate_unique(
        "native_automation",
        (automation.key for automation in contract.native_automation),
        messages,
    )
    if contract.agent_workflow:
        _validate_unique(
            "agent_workflow.workers",
            (worker.key for worker in contract.agent_workflow.workers),
            messages,
        )
        if contract.agent_workflow.codex_cli:
            _validate_unique(
                "agent_workflow.codex_cli.agents",
                (agent.key for agent in contract.agent_workflow.codex_cli.agents),
                messages,
            )
            if contract.agent_workflow.codex_cli.max_threads < 1:
                messages.append(
                    _error(
                        "invalid_codex_max_threads",
                        "agent_workflow.codex_cli.max_threads must be at least 1",
                        "$.agent_workflow.codex_cli.max_threads",
                    )
                )
            if contract.agent_workflow.codex_cli.max_depth > 1:
                messages.append(
                    _error(
                        "invalid_codex_max_depth",
                        "agent_workflow.codex_cli.max_depth must not be greater than 1",
                        "$.agent_workflow.codex_cli.max_depth",
                    )
                )
            if contract.agent_workflow.codex_cli.max_depth < 0:
                messages.append(
                    _error(
                        "invalid_codex_max_depth",
                        "agent_workflow.codex_cli.max_depth must not be negative",
                        "$.agent_workflow.codex_cli.max_depth",
                    )
                )

    for roadmap_index, roadmap in enumerate(contract.roadmaps):
        for epic_key in roadmap.epics:
            epic = epics.get(epic_key)
            if epic is None:
                messages.append(
                    _error(
                        "missing_epic",
                        f"roadmap {roadmap.key} references missing epic {epic_key}",
                        f"$.roadmaps[{roadmap_index}].epics",
                    )
                )
            elif epic.roadmap != roadmap.key:
                messages.append(
                    _error(
                        "hierarchy_mismatch",
                        f"epic {epic.key} declares roadmap {epic.roadmap}, not {roadmap.key}",
                        f"$.roadmaps[{roadmap_index}].epics",
                    )
                )

    for epic_index, epic in enumerate(contract.epics):
        if epic.roadmap not in roadmaps:
            messages.append(
                _error(
                    "missing_roadmap",
                    f"epic {epic.key} references missing roadmap {epic.roadmap}",
                    f"$.epics[{epic_index}].roadmap",
                )
            )
        for milestone_key in epic.milestones:
            milestone = milestones.get(milestone_key)
            if milestone is None:
                messages.append(
                    _error(
                        "missing_milestone",
                        f"epic {epic.key} references missing milestone {milestone_key}",
                        f"$.epics[{epic_index}].milestones",
                    )
                )
            elif milestone.epic != epic.key:
                messages.append(
                    _error(
                        "hierarchy_mismatch",
                        (
                            f"milestone {milestone.key} declares epic {milestone.epic}, "
                            f"not {epic.key}"
                        ),
                        f"$.epics[{epic_index}].milestones",
                    )
                )

    for milestone_index, milestone in enumerate(contract.milestones):
        if milestone.epic not in epics:
            messages.append(
                _error(
                    "missing_epic",
                    f"milestone {milestone.key} references missing epic {milestone.epic}",
                    f"$.milestones[{milestone_index}].epic",
                )
            )

    for issue_index, issue in enumerate(contract.issues):
        if issue.epic and issue.epic not in epics:
            messages.append(
                _error(
                    "missing_epic",
                    f"issue {issue.key} references missing epic {issue.epic}",
                    f"$.issues[{issue_index}].epic",
                )
            )
        if issue.milestone:
            milestone = milestones.get(issue.milestone)
            if milestone is None:
                messages.append(
                    _error(
                        "missing_milestone",
                        f"issue {issue.key} references missing milestone {issue.milestone}",
                        f"$.issues[{issue_index}].milestone",
                    )
                )
            elif issue.epic and issue.epic != milestone.epic:
                messages.append(
                    _error(
                        "hierarchy_mismatch",
                        (
                            f"issue {issue.key} declares epic {issue.epic}, "
                            f"but milestone {milestone.key} belongs to {milestone.epic}"
                        ),
                        f"$.issues[{issue_index}].milestone",
                    )
                )

        for dependency_key in issue.depends_on:
            dependency = issues.get(dependency_key)
            if dependency is None:
                messages.append(
                    _error(
                        "missing_dependency",
                        f"issue {issue.key} depends on missing issue {dependency_key}",
                        f"$.issues[{issue_index}].depends_on",
                    )
                )
                continue
            if dependency_key == issue.key:
                messages.append(
                    _error(
                        "self_dependency",
                        f"issue {issue.key} cannot depend on itself",
                        f"$.issues[{issue_index}].depends_on",
                    )
                )
            if issue.status in ACTIVE_STATUSES and dependency.status is not IssueStatus.DONE:
                messages.append(
                    _error(
                        "blocked_dependency",
                        (
                            f"issue {issue.key} is {issue.status} but dependency "
                            f"{dependency.key} is {dependency.status}"
                        ),
                        f"$.issues[{issue_index}].depends_on",
                    )
                )

    messages.extend(_dependency_cycle_messages(contract.issues))
    return ValidationResult.from_messages(messages)


def validate_status_transition(
    current: IssueStatus,
    target: IssueStatus,
    *,
    path: str = "$.status",
) -> ValidationResult:
    if target in ALLOWED_STATUS_TRANSITIONS[current]:
        return ValidationResult(valid=True)

    return ValidationResult.from_messages(
        (
            _error(
                "invalid_status_transition",
                f"cannot transition issue from {current} to {target}",
                path,
            ),
        )
    )


def blocked_dependencies(
    issue: IssuePlan, issues_by_key: dict[str, IssuePlan]
) -> tuple[IssuePlan, ...]:
    blocked: list[IssuePlan] = []
    for dependency_key in issue.depends_on:
        dependency = issues_by_key.get(dependency_key)
        if dependency and dependency.status is not IssueStatus.DONE:
            blocked.append(dependency)
    return tuple(blocked)


def _validate_unique(
    collection_name: str,
    keys: Iterable[str],
    messages: list[ValidationMessage],
) -> None:
    seen: set[str] = set()
    for index, key in enumerate(keys):
        if key in seen:
            messages.append(
                _error(
                    "duplicate_key",
                    f"{collection_name} contains duplicate key {key}",
                    f"$.{collection_name}[{index}].key",
                )
            )
        seen.add(key)


def _dependency_cycle_messages(issues: tuple[IssuePlan, ...]) -> tuple[ValidationMessage, ...]:
    issues_by_key = {issue.key: issue for issue in issues}
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []
    messages: list[ValidationMessage] = []
    reported: set[tuple[str, ...]] = set()

    def visit(issue_key: str) -> None:
        if issue_key in visited:
            return
        if issue_key in visiting:
            cycle_start = stack.index(issue_key)
            cycle = (*stack[cycle_start:], issue_key)
            normalized = tuple(sorted(set(cycle)))
            if normalized not in reported:
                reported.add(normalized)
                messages.append(
                    _error(
                        "dependency_cycle",
                        f"issue dependency cycle detected: {' -> '.join(cycle)}",
                        "$.issues",
                    )
                )
            return

        issue = issues_by_key.get(issue_key)
        if issue is None:
            return

        visiting.add(issue_key)
        stack.append(issue_key)
        for dependency_key in issue.depends_on:
            visit(dependency_key)
        stack.pop()
        visiting.remove(issue_key)
        visited.add(issue_key)

    for issue in issues:
        visit(issue.key)

    return tuple(messages)


def _error(code: str, message: str, path: str) -> ValidationMessage:
    return ValidationMessage(
        level=ValidationLevel.ERROR,
        code=code,
        message=message,
        path=path,
    )
