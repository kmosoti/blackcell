"""Linear Project presentation contracts and policy-aware reconciliation."""

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from blackcell.backends.planning import PlanWorkflowBackend
from blackcell.config.model import BlackcellConfig
from blackcell.contracts.errors import ConflictFailure, PolicyFailure
from blackcell.contracts.markers import plan_marker
from blackcell.contracts.plan import PlanSpec
from blackcell.policy.lifecycle import ProjectCapability, ProjectStateMachine
from blackcell.services.rendering import (
    normalize_presentation_text,
    render_project_description,
    render_project_summary,
    repository_url,
)

_GITHUB_REPOSITORY_URL = re.compile(r"https://github\.com/([^)\s`>]+)")
_LEGACY_REPOSITORY = re.compile(r"Repository:\s*`([^`]+)`")
PROJECT_PRIORITY = {
    "critical": 1,
    "high": 2,
    "medium": 3,
    "low": 4,
}


class ProjectAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity_drift: dict[str, Any] = Field(default_factory=dict)
    lifecycle_drift: dict[str, Any] = Field(default_factory=dict)
    workflow_drift: dict[str, Any] = Field(default_factory=dict)
    presentation_drift: dict[str, str] = Field(default_factory=dict)

    @property
    def matches(self) -> bool:
        return (
            not self.identity_drift
            and not self.lifecycle_drift
            and not self.workflow_drift
            and not self.presentation_drift
        )


class ProjectReconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project: dict[str, Any]
    reconciled_fields: list[str] = Field(default_factory=list)
    workflow_fields: list[str] = Field(default_factory=list)
    presentation_fields: list[str] = Field(default_factory=list)


class ProjectIntegration:
    def __init__(self, config: BlackcellConfig, linear: PlanWorkflowBackend) -> None:
        self.config = config
        self.linear = linear

    def create(self, plan: PlanSpec, status_id: str) -> dict[str, Any]:
        presentation = self.config.linear.project_presentation
        workflow = self.config.linear.project_workflow
        label_ids = (
            list(self._required_project_label_ids().values()) if workflow.label_names else []
        )
        project = self.linear.create_project(
            name=plan.linear.project_name,
            description=render_project_summary(plan),
            content=render_project_description(plan, self.config),
            team_id=self.config.linear.team_id,
            status_id=status_id,
            icon=presentation.icon,
            color=presentation.color,
            lead_id=workflow.lead_user_id,
            member_ids=list(workflow.member_user_ids) or None,
            label_ids=label_ids or None,
            priority=PROJECT_PRIORITY[workflow.priority],
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
            lifecycle_drift=self._lifecycle_drift(project),
            workflow_drift=self._workflow_drift(project),
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
        if assessment.lifecycle_drift:
            raise ConflictFailure(
                "Linear Project lifecycle diverges from configured BlackCell gates.",
                details={
                    "plan_id": plan.plan_id,
                    "project_id": project.get("id"),
                    "drift": assessment.lifecycle_drift,
                },
            )
        if not assessment.workflow_drift and not assessment.presentation_drift:
            return ProjectReconciliation(project=project)

        status_name = (project.get("status") or {}).get("name")
        state_machine = ProjectStateMachine(self.config.linear.project_statuses)
        if assessment.workflow_drift:
            try:
                state_machine.require(
                    status_name,
                    ProjectCapability.RECONCILE_WORKFLOW,
                    message="Linear Project workflow diverges after the Proposal gate.",
                )
            except PolicyFailure as error:
                raise ConflictFailure(
                    "Linear Project workflow diverges after the Proposal gate.",
                    details={
                        "plan_id": plan.plan_id,
                        "project_id": project.get("id"),
                        "status": status_name,
                        "drift": assessment.workflow_drift,
                    },
                ) from error
            project = self._reconcile_workflow(project)

        if assessment.presentation_drift:
            try:
                state_machine.require(
                    status_name,
                    ProjectCapability.RECONCILE_PRESENTATION,
                    message="Linear Project presentation diverges after the Proposal gate.",
                )
            except PolicyFailure as error:
                raise ConflictFailure(
                    "Linear Project presentation diverges after the Proposal gate.",
                    details={
                        "plan_id": plan.plan_id,
                        "project_id": project.get("id"),
                        "status": status_name,
                        "drift": assessment.presentation_drift,
                    },
                ) from error

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
            reconciled_fields=sorted({*assessment.workflow_drift, *assessment.presentation_drift}),
            workflow_fields=sorted(assessment.workflow_drift),
            presentation_fields=sorted(assessment.presentation_drift),
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

    def _lifecycle_drift(self, project: dict[str, Any]) -> dict[str, Any]:
        status_name = (project.get("status") or {}).get("name")
        try:
            ProjectStateMachine(self.config.linear.project_statuses).resolve(status_name)
        except PolicyFailure as error:
            return {
                "status": {
                    "actual": status_name,
                    "configured": self.config.linear.project_statuses.model_dump(mode="json"),
                    "reason": error.message,
                }
            }
        return {}

    def _workflow_drift(self, project: dict[str, Any]) -> dict[str, Any]:
        workflow = self.config.linear.project_workflow
        expected_priority = PROJECT_PRIORITY[workflow.priority]
        actual_lead_id = (project.get("lead") or {}).get("id")
        actual_member_ids = {
            member["id"] for member in (project.get("members") or {}).get("nodes", [])
        }
        actual_label_names = {
            label["name"] for label in (project.get("labels") or {}).get("nodes", [])
        }
        expected_member_ids = set(workflow.member_user_ids)
        expected_label_names = set(workflow.label_names)
        drift: dict[str, Any] = {}
        if actual_lead_id != workflow.lead_user_id:
            drift["lead_id"] = {
                "expected": workflow.lead_user_id,
                "actual": actual_lead_id,
            }
        missing_member_ids = sorted(expected_member_ids - actual_member_ids)
        if missing_member_ids:
            drift["member_ids"] = {
                "expected_present": sorted(expected_member_ids),
                "actual": sorted(actual_member_ids),
                "missing": missing_member_ids,
            }
        missing_label_names = sorted(expected_label_names - actual_label_names)
        if missing_label_names:
            drift["label_names"] = {
                "expected_present": sorted(expected_label_names),
                "actual": sorted(actual_label_names),
                "missing": missing_label_names,
            }
        if project.get("priority") != expected_priority:
            drift["priority"] = {
                "expected": expected_priority,
                "expected_label": workflow.priority,
                "actual": project.get("priority"),
            }
        return drift

    def _reconcile_workflow(self, project: dict[str, Any]) -> dict[str, Any]:
        workflow = self.config.linear.project_workflow
        current_member_ids = {
            member["id"] for member in (project.get("members") or {}).get("nodes", [])
        }
        current_label_ids = {
            label["id"] for label in (project.get("labels") or {}).get("nodes", [])
        }
        label_ids = None
        if workflow.label_names:
            required_label_ids = set(self._required_project_label_ids().values())
            label_ids = sorted(current_label_ids | required_label_ids)
        member_ids = None
        if workflow.member_user_ids:
            member_ids = sorted(current_member_ids | set(workflow.member_user_ids))
        return self.linear.update_project_workflow(
            project["id"],
            lead_id=workflow.lead_user_id,
            member_ids=member_ids,
            label_ids=label_ids,
            priority=PROJECT_PRIORITY[workflow.priority],
        )

    def _required_project_label_ids(self) -> dict[str, str]:
        workflow = self.config.linear.project_workflow
        labels_by_name = {label["name"]: label for label in self.linear.project_labels()}
        for label_name in workflow.label_names:
            if label_name in labels_by_name:
                continue
            labels_by_name[label_name] = self.linear.create_project_label(
                name=label_name,
                color=self.config.linear.project_presentation.color,
                description=f"{self.config.linear.project_presentation.brand} Project label",
            )
        return {label_name: labels_by_name[label_name]["id"] for label_name in workflow.label_names}

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
