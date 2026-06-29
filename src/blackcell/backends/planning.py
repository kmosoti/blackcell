"""Planning provider interface used by Blackcell services."""

from typing import Any, Protocol


class PlanningBackend(Protocol):
    def identity_snapshot(self, team_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]: ...

    def project_statuses(self) -> list[dict[str, Any]]: ...

    def workflow_states(self, team_id: str) -> list[dict[str, Any]]: ...

    def integrations(self) -> list[dict[str, Any]]: ...

    def find_projects_by_marker(self, team_id: str, marker: str) -> list[dict[str, Any]]: ...

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
