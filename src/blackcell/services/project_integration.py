"""Linear Project presentation contracts and policy-aware reconciliation."""

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from blackcell.config.model import BlackcellConfig
from blackcell.contracts.errors import ConflictFailure
from blackcell.contracts.markers import plan_marker
from blackcell.contracts.plan import PlanSpec
from blackcell.services.rendering import (
    normalize_presentation_text,
    render_project_description,
    render_project_summary,
    repository_url,
)

_GITHUB_REPOSITORY_URL = re.compile(r"https://github\.com/([^)\s`>]+)")
_LEGACY_REPOSITORY = re.compile(r"Repository:\s*`([^`]+)`")


class ProjectAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity_drift: dict[str, Any] = Field(default_factory=dict)
    presentation_drift: dict[str, str] = Field(default_factory=dict)

    @property
    def matches(self) -> bool:
        return not self.identity_drift and not self.presentation_drift


class ProjectReconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project: dict[str, Any]
    reconciled_fields: list[str] = Field(default_factory=list)


class ProjectIntegration:
    def __init__(self, config: BlackcellConfig, linear: Any) -> None:
        self.config = config
        self.linear = linear

    def create(self, plan: PlanSpec, status_id: str) -> dict[str, Any]:
        presentation = self.config.linear.project_presentation
        project = self.linear.create_project(
            name=plan.linear.project_name,
            description=render_project_summary(plan),
            content=render_project_description(plan, self.config),
            team_id=self.config.linear.team_id,
            status_id=status_id,
            icon=presentation.icon,
            color=presentation.color,
        )
        link = self.linear.create_project_external_link(
            project["id"],
            url=repository_url(plan),
            label=presentation.repository_link_label,
        )
        project["externalLinks"] = {"nodes": [link]}
        self.verify(project, plan)
        return project

    def assess(self, project: dict[str, Any], plan: PlanSpec) -> ProjectAssessment:
        return ProjectAssessment(
            identity_drift=self._identity_drift(project, plan),
            presentation_drift=self._presentation_drift(project, plan),
        )

    def verify(self, project: dict[str, Any], plan: PlanSpec) -> None:
        assessment = self.assess(project, plan)
        if assessment.matches:
            return
        raise ConflictFailure(
            "Linear Project contract diverges from the directive.",
            details={
                "plan_id": plan.plan_id,
                "project_id": project.get("id"),
                **assessment.model_dump(mode="json"),
            },
        )

    def reconcile(self, project: dict[str, Any], plan: PlanSpec) -> ProjectReconciliation:
        assessment = self.assess(project, plan)
        if assessment.identity_drift:
            raise ConflictFailure(
                "Linear Project identity diverges from the directive.",
                details={
                    "plan_id": plan.plan_id,
                    "project_id": project.get("id"),
                    "drift": assessment.identity_drift,
                },
            )
        if not assessment.presentation_drift:
            return ProjectReconciliation(project=project)

        status_name = (project.get("status") or {}).get("name")
        if status_name != self.config.linear.project_statuses.proposal:
            raise ConflictFailure(
                "Linear Project presentation diverges after the Proposal gate.",
                details={
                    "plan_id": plan.plan_id,
                    "project_id": project.get("id"),
                    "status": status_name,
                    "drift": assessment.presentation_drift,
                },
            )

        drift = assessment.presentation_drift
        presentation = self.config.linear.project_presentation
        project_fields = {"description", "content", "icon", "color"}
        if project_fields.intersection(drift):
            project = self.linear.update_project_presentation(
                project["id"],
                description=render_project_summary(plan),
                content=render_project_description(plan, self.config),
                icon=presentation.icon,
                color=presentation.color,
            )

        expected_url = repository_url(plan)
        links = list((project.get("externalLinks") or {}).get("nodes", []))
        repository_links = [
            link for link in links if (link.get("url") or "").rstrip("/") == expected_url
        ]
        if "repository_link" in drift:
            link = self.linear.create_project_external_link(
                project["id"],
                url=expected_url,
                label=presentation.repository_link_label,
            )
            links.append(link)
        elif "repository_link_label" in drift:
            current = repository_links[0]
            updated = self.linear.update_project_external_link(
                current["id"],
                url=expected_url,
                label=presentation.repository_link_label,
            )
            links = [updated if link.get("id") == current["id"] else link for link in links]
        project["externalLinks"] = {"nodes": links}

        self.verify(project, plan)
        return ProjectReconciliation(
            project=project,
            reconciled_fields=sorted(drift),
        )

    def _identity_drift(self, project: dict[str, Any], plan: PlanSpec) -> dict[str, Any]:
        expected_marker = plan_marker(plan)
        directive_text = project.get("content") or project.get("description") or ""
        actual_team_ids = {team["id"] for team in (project.get("teams") or {}).get("nodes", [])}
        drift: dict[str, Any] = {}
        if project.get("name") != plan.linear.project_name:
            drift["name"] = {
                "expected": plan.linear.project_name,
                "actual": project.get("name"),
            }
        if self.config.linear.team_id not in actual_team_ids:
            drift["team_ids"] = {
                "expected": self.config.linear.team_id,
                "actual": sorted(actual_team_ids),
            }
        if expected_marker not in directive_text:
            drift["marker"] = "remote Project marker differs from the directive"
        repository_drift = self._repository_identity_drift(project, plan)
        if repository_drift:
            drift["repository"] = repository_drift
        return drift

    def _presentation_drift(self, project: dict[str, Any], plan: PlanSpec) -> dict[str, str]:
        expected_content = render_project_description(plan, self.config)
        presentation = self.config.linear.project_presentation
        drift: dict[str, str] = {}
        if project.get("description") != render_project_summary(plan):
            drift["description"] = "remote Project summary differs from the directive"
        if normalize_presentation_text(project.get("content")) != normalize_presentation_text(
            expected_content
        ):
            drift["content"] = "remote Project content differs from the directive"
        if presentation.icon is not None and project.get("icon") != presentation.icon:
            drift["icon"] = "remote Project icon differs from configuration"
        if project.get("color") != presentation.color:
            drift["color"] = "remote Project color differs from configuration"

        expected_url = repository_url(plan)
        links = (project.get("externalLinks") or {}).get("nodes", [])
        repository_links = [
            link for link in links if (link.get("url") or "").rstrip("/") == expected_url
        ]
        if not repository_links:
            drift["repository_link"] = "remote Project has no visible repository link"
        elif repository_links[0].get("label") != presentation.repository_link_label:
            drift["repository_link_label"] = (
                "remote Project repository link label differs from configuration"
            )
        return drift

    @staticmethod
    def _repository_identity_drift(project: dict[str, Any], plan: PlanSpec) -> dict[str, Any]:
        expected_slug = f"{plan.repository.owner}/{plan.repository.name}"
        expected_url = repository_url(plan)
        content = project.get("content") or ""
        declared_urls = {
            f"https://github.com/{match.rstrip('/')}"
            for match in _GITHUB_REPOSITORY_URL.findall(content)
        }
        legacy_match = _LEGACY_REPOSITORY.search(content)
        declared_slugs = {legacy_match.group(1)} if legacy_match else set()
        links = (project.get("externalLinks") or {}).get("nodes", [])
        linked_github_urls = {
            (link.get("url") or "").rstrip("/")
            for link in links
            if (link.get("url") or "").startswith("https://github.com/")
        }
        mismatched_urls = sorted(
            url for url in declared_urls | linked_github_urls if url != expected_url
        )
        mismatched_slugs = sorted(slug for slug in declared_slugs if slug != expected_slug)
        if mismatched_urls or mismatched_slugs:
            return {
                "expected": expected_url,
                "actual_urls": mismatched_urls,
                "actual_slugs": mismatched_slugs,
            }
        return {}
