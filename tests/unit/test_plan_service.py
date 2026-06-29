"""Proposal-only Linear Project presentation reconciliation."""

from pathlib import Path
from typing import Any, cast

import pytest

from blackcell.adapters.linear_graphql import LinearGraphQLAdapter
from blackcell.config.model import BlackcellConfig
from blackcell.contracts.errors import ConflictFailure
from blackcell.contracts.markers import plan_marker
from blackcell.contracts.plan import PlanSpec
from blackcell.ledger.sqlite import Chronicle
from blackcell.services.plan_service import PlanService
from blackcell.services.plan_store import PlanStore
from blackcell.services.rendering import render_project_description, render_project_summary


class FakePlanStore:
    def __init__(self, plan: PlanSpec) -> None:
        self.plan = plan

    def save(self, plan: PlanSpec) -> Path:
        self.plan = plan
        return Path(f"/tmp/{plan.plan_id}.json")

    def load(self, plan_id: str) -> PlanSpec:
        assert plan_id == self.plan.plan_id
        return self.plan


class FakeLinear:
    def __init__(
        self,
        config: BlackcellConfig,
        plan: PlanSpec,
        *,
        status: str = "Proposal",
        repository: str = "kmosoti/blackcell",
        repository_link_label: str | None = None,
    ) -> None:
        self.config = config
        self.presentation_updates = 0
        self.workflow_updates = 0
        self.project_label_creations = 0
        self.link_creations = 0
        self.link_updates = 0
        self.project_label_registry = {
            name: {
                "id": f"project-label-{index}",
                "name": name,
                "color": config.linear.project_presentation.color,
                "archivedAt": None,
            }
            for index, name in enumerate(config.linear.project_workflow.label_names, start=1)
        }
        self.project: dict[str, Any] = {
            "id": "project-1",
            "name": plan.linear.project_name,
            "description": render_project_summary(plan),
            "content": (
                f"# {plan.title}\n\n{plan.objective}\n\n"
                f"Repository: `{repository}`\n\n"
                f"## Assignments\n\n- legacy presentation\n\n"
                f"{plan_marker(plan)}"
            ),
            "icon": None,
            "color": "#95a2b3",
            "url": "https://linear.test/project-1",
            "lead": {
                "id": config.linear.project_workflow.lead_user_id,
                "name": config.identity.planner_name,
                "email": config.identity.planner_email,
            },
            "members": {
                "nodes": [
                    {"id": user_id, "name": "Configured member", "email": "member@example.test"}
                    for user_id in config.linear.project_workflow.member_user_ids
                ]
            },
            "labels": {"nodes": list(self.project_label_registry.values())},
            "priority": 2,
            "priorityLabel": "High",
            "status": {"id": "status-1", "name": status, "type": "backlog"},
            "teams": {"nodes": [{"id": config.linear.team_id}]},
            "externalLinks": {
                "nodes": (
                    [
                        {
                            "id": "link-1",
                            "url": "https://github.com/kmosoti/blackcell",
                            "label": repository_link_label,
                            "archivedAt": None,
                        }
                    ]
                    if repository_link_label is not None
                    else []
                )
            },
        }

    def identity_snapshot(self, team_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        assert team_id == self.config.linear.team_id
        return (
            {
                "id": self.config.identity.planner_user_id,
                "name": self.config.identity.planner_name,
                "email": self.config.identity.planner_email,
            },
            {
                "id": self.config.linear.team_id,
                "key": self.config.linear.team_key,
                "name": self.config.linear.team_name,
                "archivedAt": None,
            },
        )

    def project_statuses(self) -> list[dict[str, str]]:
        return [{"id": "proposal-status", "name": "Proposal"}]

    def project_labels(self) -> list[dict[str, Any]]:
        return list(self.project_label_registry.values())

    def create_project_label(
        self,
        *,
        name: str,
        color: str,
        description: str | None = None,
    ) -> dict[str, Any]:
        self.project_label_creations += 1
        label = {
            "id": f"project-label-created-{self.project_label_creations}",
            "name": name,
            "color": color,
            "description": description,
            "archivedAt": None,
        }
        self.project_label_registry[name] = label
        return label

    def find_projects_by_marker(self, team_id: str, marker: str) -> list[dict[str, Any]]:
        assert team_id == self.config.linear.team_id
        assert marker in self.project["content"]
        return [self.project]

    def update_project_presentation(
        self,
        project_id: str,
        *,
        description: str,
        content: str,
        icon: str | None,
        color: str,
    ) -> dict[str, Any]:
        assert project_id == self.project["id"]
        self.presentation_updates += 1
        self.project.update(
            {
                "description": description,
                "content": content.replace("\n- ", "\n* "),
                "icon": icon,
                "color": color,
            }
        )
        return self.project

    def update_project_workflow(
        self,
        project_id: str,
        *,
        lead_id: str | None = None,
        member_ids: list[str] | None = None,
        label_ids: list[str] | None = None,
        priority: int | None = None,
        status_id: str | None = None,
    ) -> dict[str, Any]:
        del status_id
        assert project_id == self.project["id"]
        self.workflow_updates += 1
        if lead_id is not None:
            self.project["lead"] = {"id": lead_id, "name": "Lead", "email": "lead@example.test"}
        if member_ids is not None:
            self.project["members"] = {
                "nodes": [
                    {"id": user_id, "name": "Member", "email": "member@example.test"}
                    for user_id in member_ids
                ]
            }
        if label_ids is not None:
            labels_by_id = {label["id"]: label for label in self.project_label_registry.values()}
            self.project["labels"] = {
                "nodes": [
                    labels_by_id.get(
                        label_id,
                        {"id": label_id, "name": label_id, "archivedAt": None},
                    )
                    for label_id in label_ids
                ]
            }
        if priority is not None:
            self.project["priority"] = priority
        return self.project

    def create_project_external_link(
        self, project_id: str, *, url: str, label: str
    ) -> dict[str, Any]:
        assert project_id == self.project["id"]
        self.link_creations += 1
        return {"id": "link-1", "url": url, "label": label, "archivedAt": None}

    def update_project_external_link(self, link_id: str, *, url: str, label: str) -> dict[str, Any]:
        assert link_id == "link-1"
        self.link_updates += 1
        return {"id": link_id, "url": url, "label": label, "archivedAt": None}


def build_service(
    tmp_path: Path,
    config: BlackcellConfig,
    plan: PlanSpec,
    linear: FakeLinear,
) -> tuple[PlanService, Chronicle]:
    chronicle = Chronicle(tmp_path / "chronicle.sqlite3")
    return (
        PlanService(
            config,
            chronicle,
            cast(PlanStore, FakePlanStore(plan)),
            cast(LinearGraphQLAdapter, linear),
        ),
        chronicle,
    )


def test_proposal_presentation_is_reconciled_and_recorded(
    tmp_path: Path, config: BlackcellConfig, plan: PlanSpec
) -> None:
    linear = FakeLinear(config, plan)
    service, chronicle = build_service(tmp_path, config, plan, linear)

    result = service.propose(plan)

    assert result["created"] is False
    assert result["presentation_reconciled"] is True
    assert linear.presentation_updates == 1
    assert linear.link_creations == 1
    assert linear.project["icon"] is None
    assert linear.project["color"] == "#111827"
    assert linear.project["externalLinks"]["nodes"][0]["url"] == (
        "https://github.com/kmosoti/blackcell"
    )
    assert linear.project["externalLinks"]["nodes"][0]["label"] == "BlackCell repository"
    reconciled = [
        event
        for event in chronicle.events(plan.plan_id)
        if event.event_type == "operation_presentation_reconciled"
    ]
    assert reconciled[0].payload["fields"] == [
        "color",
        "content",
        "repository_link",
    ]


def test_approved_project_presentation_is_immutable(
    tmp_path: Path, config: BlackcellConfig, plan: PlanSpec
) -> None:
    linear = FakeLinear(config, plan, status="Approved")
    service, _ = build_service(tmp_path, config, plan, linear)

    with pytest.raises(ConflictFailure, match="after the Proposal gate"):
        service.propose(plan)

    assert linear.presentation_updates == 0
    assert linear.link_creations == 0


def test_proposal_repository_link_label_is_reconciled(
    tmp_path: Path, config: BlackcellConfig, plan: PlanSpec
) -> None:
    linear = FakeLinear(config, plan, repository_link_label="GitHub repository")
    service, _ = build_service(tmp_path, config, plan, linear)

    result = service.propose(plan)

    assert result["presentation_reconciled"] is True
    assert linear.link_creations == 0
    assert linear.link_updates == 1
    assert linear.project["externalLinks"]["nodes"][0]["label"] == "BlackCell repository"


def test_operation_inspect_reports_drift_without_mutation(
    tmp_path: Path, config: BlackcellConfig, plan: PlanSpec
) -> None:
    linear = FakeLinear(config, plan)
    service, _ = build_service(tmp_path, config, plan, linear)

    result = service.inspect_operation(plan.plan_id)

    assert result["matches"] is False
    assert set(result["presentation_drift"]) == {"color", "content", "repository_link"}
    assert result["workflow_drift"] == {}
    assert linear.presentation_updates == 0
    assert linear.workflow_updates == 0
    assert linear.link_creations == 0


def test_proposal_workflow_is_reconciled_and_recorded(
    tmp_path: Path, config: BlackcellConfig, plan: PlanSpec
) -> None:
    linear = FakeLinear(config, plan, repository_link_label="BlackCell repository")
    linear.project["lead"] = None
    linear.project["members"] = {"nodes": []}
    linear.project["labels"] = {"nodes": []}
    linear.project["priority"] = 4
    linear.project.update(
        {
            "content": render_project_description(plan, config).replace("\n- ", "\n* "),
            "color": config.linear.project_presentation.color,
        }
    )
    service, chronicle = build_service(tmp_path, config, plan, linear)

    result = service.propose(plan)

    assert result["workflow_reconciled"] is True
    assert result["presentation_reconciled"] is False
    assert linear.workflow_updates == 1
    assert linear.presentation_updates == 0
    lead = linear.project["lead"]
    assert isinstance(lead, dict)
    assert lead["id"] == config.linear.project_workflow.lead_user_id
    assert linear.project["priority"] == 2
    assert {label["name"] for label in linear.project["labels"]["nodes"]} == {
        "BlackCell",
        "BCP-0001",
    }
    reconciled = [
        event
        for event in chronicle.events(plan.plan_id)
        if event.event_type == "operation_workflow_reconciled"
    ]
    assert reconciled[0].payload["fields"] == [
        "label_names",
        "lead_id",
        "member_ids",
        "priority",
    ]


def test_unknown_project_lifecycle_is_contract_drift(
    tmp_path: Path, config: BlackcellConfig, plan: PlanSpec
) -> None:
    linear = FakeLinear(
        config,
        plan,
        status="Active",
        repository_link_label="BlackCell repository",
    )
    linear.project.update(
        {
            "content": render_project_description(plan, config).replace("\n- ", "\n* "),
            "color": config.linear.project_presentation.color,
        }
    )
    service, _ = build_service(tmp_path, config, plan, linear)

    result = service.inspect_operation(plan.plan_id)

    assert result["matches"] is False
    assert result["identity_drift"] == {}
    assert result["presentation_drift"] == {}
    assert result["lifecycle_drift"]["status"]["actual"] == "Active"
    with pytest.raises(ConflictFailure, match="lifecycle diverges"):
        service.reconcile_operation(plan.plan_id)


def test_linear_angle_bracket_repository_link_is_equivalent(
    tmp_path: Path, config: BlackcellConfig, plan: PlanSpec
) -> None:
    linear = FakeLinear(config, plan, repository_link_label="BlackCell repository")
    linear.project["content"] = linear.project["content"].replace(
        "Repository: `kmosoti/blackcell`",
        "Repository: <https://github.com/kmosoti/blackcell>",
    )
    service, _ = build_service(tmp_path, config, plan, linear)

    result = service.inspect_operation(plan.plan_id)

    assert result["identity_drift"] == {}


def test_proposal_repository_mismatch_is_not_rewritten(
    tmp_path: Path, config: BlackcellConfig, plan: PlanSpec
) -> None:
    linear = FakeLinear(config, plan, repository="kmosoti/other")
    service, _ = build_service(tmp_path, config, plan, linear)

    with pytest.raises(ConflictFailure, match="identity diverges"):
        service.propose(plan)

    assert linear.presentation_updates == 0
    assert linear.link_creations == 0
