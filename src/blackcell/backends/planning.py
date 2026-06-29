"""Narrow planning capabilities consumed by BlackCell workflows."""

from typing import Any, Protocol


class PlanningIdentityReader(Protocol):
    def identity_snapshot(self, team_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]: ...


class PlanningProjectStatusReader(Protocol):
    def project_statuses(self) -> list[dict[str, Any]]: ...


class PlanningIssueWorkflowReader(Protocol):
    def workflow_states(self, team_id: str) -> list[dict[str, Any]]: ...

    def issue_labels(self, team_id: str) -> list[dict[str, Any]]: ...


class PlanningIntegrationReader(Protocol):
    def integrations(self) -> list[dict[str, Any]]: ...


class PlanningProjectLocator(Protocol):
    def find_projects_by_marker(self, team_id: str, marker: str) -> list[dict[str, Any]]: ...


class PlanningProjectIssueReader(Protocol):
    def project_issues(self, project_id: str) -> list[dict[str, Any]]: ...


class PlanningProjectReader(PlanningProjectLocator, PlanningProjectIssueReader, Protocol):
    """Complete read capabilities for Project-backed workflows."""


class PlanningProjectWriter(Protocol):
    def create_project(
        self,
        *,
        name: str,
        description: str,
        content: str,
        team_id: str,
        status_id: str,
        icon: str | None,
        color: str,
    ) -> dict[str, Any]: ...

    def update_project_presentation(
        self,
        project_id: str,
        *,
        description: str,
        content: str,
        icon: str | None,
        color: str,
    ) -> dict[str, Any]: ...

    def create_project_external_link(
        self,
        project_id: str,
        *,
        url: str,
        label: str,
    ) -> dict[str, Any]: ...

    def update_project_external_link(
        self,
        link_id: str,
        *,
        url: str,
        label: str,
    ) -> dict[str, Any]: ...


class PlanningAssignmentReader(Protocol):
    def team_issues(self, team_id: str) -> list[dict[str, Any]]: ...

    def issue_relations(self, issue_id: str) -> list[dict[str, Any]]: ...


class PlanningAssignmentWriter(Protocol):
    def create_issue(
        self,
        *,
        team_id: str,
        project_id: str,
        state_id: str,
        title: str,
        description: str,
        priority: int,
        label_ids: list[str],
        parent_id: str | None,
    ) -> dict[str, Any]: ...

    def create_blocking_relation(self, blocker_id: str, blocked_id: str) -> dict[str, Any]: ...


class MaterializationPlanningBackend(
    PlanningIdentityReader,
    PlanningIssueWorkflowReader,
    PlanningProjectLocator,
    PlanningAssignmentReader,
    PlanningAssignmentWriter,
    Protocol,
):
    """Capabilities required by approved directive materialization."""


class PlanWorkflowBackend(
    PlanningIdentityReader,
    PlanningProjectStatusReader,
    PlanningProjectLocator,
    PlanningProjectWriter,
    Protocol,
):
    """Capabilities required by Project proposal and reconciliation."""


class PlanningBackend(
    PlanningIdentityReader,
    PlanningProjectStatusReader,
    PlanningIssueWorkflowReader,
    PlanningIntegrationReader,
    PlanningProjectReader,
    PlanningProjectWriter,
    PlanningAssignmentReader,
    PlanningAssignmentWriter,
    Protocol,
):
    """Complete provider capability set for the current planning proof."""
