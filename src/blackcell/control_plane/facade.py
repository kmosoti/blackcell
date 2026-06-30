from pathlib import Path
from typing import Protocol

from blackcell.config import load_config
from blackcell.control_plane.capabilities import validate_github_capabilities
from blackcell.control_plane.loader import load_contract
from blackcell.control_plane.models import (
    AgentIssueContext,
    DependencyContext,
    IssueStatus,
    PlanContract,
    ProjectFieldShape,
    ProjectShape,
    ValidationResult,
)
from blackcell.control_plane.sync import SyncResult, sync_issues
from blackcell.control_plane.validation import blocked_dependencies, validate_contract
from blackcell.providers import ProjectProvider, default_registry


class ControlPlane(Protocol):
    def load_contract(self) -> PlanContract:
        raise NotImplementedError

    def validate_contract(self) -> ValidationResult:
        raise NotImplementedError

    def validate_github_capabilities(self) -> ValidationResult:
        raise NotImplementedError

    def render_agent_context(self, issue_key: str) -> AgentIssueContext:
        raise NotImplementedError

    def plan_project_shape(self) -> ProjectShape:
        raise NotImplementedError

    def create_project(self) -> None:
        raise NotImplementedError

    def ensure_project_shape(self) -> None:
        raise NotImplementedError

    def create_issue(self) -> None:
        raise NotImplementedError

    def transition_issue(self) -> None:
        raise NotImplementedError

    def sync_contract(
        self,
        *,
        apply_changes: bool = False,
        issue_key: str | None = None,
        refresh_cache: bool = False,
        provider: ProjectProvider | None = None,
        cache_path: Path | None = None,
    ) -> SyncResult:
        raise NotImplementedError

    def reconcile(self) -> None:
        raise NotImplementedError


class LocalControlPlane:
    def __init__(
        self, *, start: Path | None = None, capability_manifest_path: Path | None = None
    ) -> None:
        self._start = start
        self._capability_manifest_path = capability_manifest_path

    def load_contract(self) -> PlanContract:
        return load_contract(self._start)

    def validate_contract(self) -> ValidationResult:
        return validate_contract(self.load_contract())

    def validate_github_capabilities(self) -> ValidationResult:
        return validate_github_capabilities(self._start, path=self._capability_manifest_path)

    def render_agent_context(self, issue_key: str) -> AgentIssueContext:
        contract = self.load_contract()
        issues_by_key = {issue.key: issue for issue in contract.issues}
        issue = issues_by_key.get(issue_key)
        if issue is None:
            raise ValueError(f"unknown issue key: {issue_key}")

        dependencies = tuple(
            DependencyContext(
                key=dependency.key,
                title=dependency.title,
                status=dependency.status,
            )
            for dependency_key in issue.depends_on
            if (dependency := issues_by_key.get(dependency_key))
        )
        blocked = tuple(
            DependencyContext(
                key=dependency.key,
                title=dependency.title,
                status=dependency.status,
            )
            for dependency in blocked_dependencies(issue, issues_by_key)
        )

        return AgentIssueContext(
            key=issue.key,
            title=issue.title,
            type=issue.type,
            status=issue.status,
            priority=issue.priority,
            complexity=issue.complexity,
            epic=issue.epic,
            milestone=issue.milestone,
            depends_on=dependencies,
            blocked_by=blocked,
            areas_of_responsibility=issue.areas_of_responsibility,
            scope=issue.scope,
            context=issue.context,
            change_spec=issue.change_spec,
            acceptance_criteria=(
                *contract.global_policy.acceptance_criteria,
                *issue.acceptance_criteria,
            ),
            definition_of_ready=(
                *contract.global_policy.definition_of_ready,
                *issue.definition_of_ready,
            ),
            definition_of_done=(
                *contract.global_policy.definition_of_done,
                *issue.definition_of_done,
            ),
            pr_policy=contract.pr_policy,
            agent_workflow=contract.agent_workflow,
        )

    def plan_project_shape(self) -> ProjectShape:
        contract = self.load_contract()
        return ProjectShape(
            project=contract.project,
            fields=(
                ProjectFieldShape(
                    name="Status",
                    type="single_select",
                    options=tuple(status.value for status in IssueStatus),
                ),
                ProjectFieldShape(
                    name="Priority",
                    type="single_select",
                    options=("P0", "P1", "P2", "P3"),
                ),
                ProjectFieldShape(
                    name="Complexity",
                    type="number",
                    options=(1, 3, 5, 8, 13),
                ),
                ProjectFieldShape(
                    name="Type",
                    type="single_select",
                    options=("feature", "bug", "refactor", "chore"),
                ),
            ),
            roadmaps=contract.roadmaps,
            epics=contract.epics,
            milestones=contract.milestones,
            issue_count=len(contract.issues),
            native_automation=contract.native_automation,
            pr_policy=contract.pr_policy,
            agent_workflow=contract.agent_workflow,
        )

    def create_project(self) -> None:
        raise NotImplementedError("mutating project operations are reserved for a later slice")

    def ensure_project_shape(self) -> None:
        raise NotImplementedError("mutating project operations are reserved for a later slice")

    def create_issue(self) -> None:
        raise NotImplementedError("mutating issue operations are reserved for a later slice")

    def transition_issue(self) -> None:
        raise NotImplementedError("mutating issue operations are reserved for a later slice")

    def sync_contract(
        self,
        *,
        apply_changes: bool = False,
        issue_key: str | None = None,
        refresh_cache: bool = False,
        provider: ProjectProvider | None = None,
        cache_path: Path | None = None,
    ) -> SyncResult:
        contract = self.load_contract()
        validation = validate_contract(contract)
        if not validation.valid:
            codes = ", ".join(error.code for error in validation.errors)
            raise ValueError(f"control-plane contract is invalid: {codes}")

        capability_validation = self.validate_github_capabilities()
        if not capability_validation.valid:
            codes = ", ".join(error.code for error in capability_validation.errors)
            raise ValueError(f"GitHub capability validation failed: {codes}")

        config = load_config(self._start)
        sync_provider = provider or default_registry().create(config.provider, config)
        return sync_issues(
            contract=contract,
            config=config,
            provider=sync_provider,
            start=self._start,
            cache_path=cache_path,
            apply_changes=apply_changes,
            issue_key=issue_key,
            refresh_cache=refresh_cache,
        )

    def reconcile(self) -> None:
        raise NotImplementedError("bidirectional reconcile is reserved for a later slice")
