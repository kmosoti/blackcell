from pathlib import Path
from typing import Any, cast

from blackcell.config.model import BlackcellConfig
from blackcell.contracts.plan import PlanSpec
from blackcell.ledger.sqlite import Chronicle
from blackcell.sdk.client import BlackcellClient
from blackcell.services.materialization_service import LINEAR_PRIORITY
from blackcell.services.plan_store import PlanStore
from blackcell.services.rendering import (
    render_issue_description,
    render_project_description,
    render_project_summary,
)


class FakePlanStore:
    def __init__(self, plan: PlanSpec) -> None:
        self.plan = plan

    def load(self, plan_id: str) -> PlanSpec:
        assert plan_id == self.plan.plan_id
        return self.plan


class FakeLinear:
    def __init__(
        self,
        config: BlackcellConfig,
        plan: PlanSpec,
        relation_map: set[tuple[str, str]],
    ) -> None:
        self.config = config
        self.project: dict[str, Any] = {
            "id": "project-1",
            "name": plan.linear.project_name,
            "description": render_project_summary(plan),
            "content": render_project_description(plan, config),
            "icon": None,
            "color": config.linear.project_presentation.color,
            "url": "https://linear.test/project-1",
            "lead": {
                "id": config.linear.project_workflow.lead_user_id,
                "name": config.identity.planner_name,
                "email": config.identity.planner_email,
            },
            "members": {
                "nodes": [
                    {"id": user_id, "name": "Member", "email": "member@example.test"}
                    for user_id in config.linear.project_workflow.member_user_ids
                ]
            },
            "labels": {
                "nodes": [
                    {"id": f"project-label-{name}", "name": name, "archivedAt": None}
                    for name in config.linear.project_workflow.label_names
                ]
            },
            "priority": 2,
            "priorityLabel": "High",
            "status": {"name": config.linear.project_statuses.approved},
            "teams": {"nodes": [{"id": config.linear.team_id}]},
            "externalLinks": {
                "nodes": [
                    {
                        "id": "link-1",
                        "url": "https://github.com/kmosoti/blackcell",
                        "label": config.linear.project_presentation.repository_link_label,
                        "archivedAt": None,
                    }
                ]
            },
        }
        label_names = sorted({label for item in plan.work_items for label in item.labels})
        self.labels = [{"id": f"label-{name}", "name": name} for name in label_names]
        self.issues: list[dict[str, Any]] = []
        self.by_key: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(plan.ordered_work_items(), start=1):
            issue_id = f"issue-{index}"
            issue = {
                "id": issue_id,
                "identifier": f"IT-{index}",
                "title": item.title,
                "description": render_issue_description(plan, item),
                "priority": LINEAR_PRIORITY[item.priority],
                "project": {"id": self.project["id"]},
                "team": {"id": config.linear.team_id},
                "parent": {"id": self.by_key[item.parent_key]["id"]} if item.parent_key else None,
                "labels": {"nodes": [{"id": f"label-{label}"} for label in item.labels]},
                "relations": {"nodes": []},
                "inverseRelations": {"nodes": []},
                "url": f"https://linear.test/{issue_id}",
            }
            self.issues.append(issue)
            self.by_key[item.key] = issue

        self._relation_id = 0
        for blocker_key, blocked_key in relation_map:
            blocker = self.by_key[blocker_key]
            self._relation_id += 1
            blocker["relations"]["nodes"].append(
                {
                    "id": f"relation-{self._relation_id}",
                    "type": "blocks",
                    "issue": {"id": self.by_key[blocker_key]["id"]},
                    "relatedIssue": {"id": self.by_key[blocked_key]["id"]},
                }
            )

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

    def find_projects_by_marker(self, team_id: str, marker: str) -> list[dict[str, Any]]:
        del team_id
        assert marker in self.project["content"]
        return [self.project]

    def issue_labels(self, team_id: str) -> list[dict[str, Any]]:
        assert team_id == self.config.linear.team_id
        return cast(list[dict[str, Any]], self.labels)

    def team_issues(self, team_id: str) -> list[dict[str, Any]]:
        del team_id
        return self.issues


def plan_relation_map(plan: PlanSpec) -> set[tuple[str, str]]:
    return {(dependency, item.key) for item in plan.work_items for dependency in item.blocked_by}


def test_verify_assignments_succeeds_with_matching_relation_directions(
    tmp_path: Path, config: BlackcellConfig, plan: PlanSpec
) -> None:
    client = BlackcellClient(
        config,
        chronicle=Chronicle(tmp_path / "chronicle.sqlite3"),
        store=cast(PlanStore, FakePlanStore(plan)),
        linear=cast(Any, FakeLinear(config, plan, plan_relation_map(plan))),
    )

    result = client.verify_assignments(plan.plan_id)

    assert result.status == "ok"
    assert len(result.data["verified"]) == len(plan.work_items)


def test_verify_assignments_conflicts_on_missing_declared_relation(
    tmp_path: Path, config: BlackcellConfig, plan: PlanSpec
) -> None:
    client = BlackcellClient(
        config,
        chronicle=Chronicle(tmp_path / "chronicle.sqlite3"),
        store=cast(PlanStore, FakePlanStore(plan)),
        linear=cast(Any, FakeLinear(config, plan, set())),
    )

    result = client.verify_assignments(plan.plan_id)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "conflict"
    relation_conflicts = result.error.details["relation_conflicts"]
    assert relation_conflicts["missing"] == [
        {
            "blocker_key": "BCP-0001-001",
            "blocked_key": "BCP-0001-002",
        }
    ]


def test_verify_assignments_conflicts_on_extra_relation(
    tmp_path: Path, config: BlackcellConfig, plan: PlanSpec
) -> None:
    client = BlackcellClient(
        config,
        chronicle=Chronicle(tmp_path / "chronicle.sqlite3"),
        store=cast(PlanStore, FakePlanStore(plan)),
        linear=cast(
            Any,
            FakeLinear(
                config,
                plan,
                plan_relation_map(plan) | {("BCP-0001-003", "BCP-0001-002")},
            ),
        ),
    )

    result = client.verify_assignments(plan.plan_id)

    assert result.status == "error"
    assert result.error is not None
    relation_conflicts = result.error.details["relation_conflicts"]
    assert relation_conflicts["missing"] == []
    assert relation_conflicts["extra"] == [
        {
            "blocker_key": "BCP-0001-003",
            "blocked_key": "BCP-0001-002",
        }
    ]


def test_verify_assignments_conflicts_on_wrong_relation_direction(
    tmp_path: Path, config: BlackcellConfig, plan: PlanSpec
) -> None:
    client = BlackcellClient(
        config,
        chronicle=Chronicle(tmp_path / "chronicle.sqlite3"),
        store=cast(PlanStore, FakePlanStore(plan)),
        linear=cast(
            Any,
            FakeLinear(config, plan, {("BCP-0001-002", "BCP-0001-001")}),
        ),
    )

    result = client.verify_assignments(plan.plan_id)

    assert result.status == "error"
    assert result.error is not None
    relation_conflicts = result.error.details["relation_conflicts"]
    assert relation_conflicts["missing"] == [
        {
            "blocker_key": "BCP-0001-001",
            "blocked_key": "BCP-0001-002",
        }
    ]
    assert relation_conflicts["extra"] == [
        {
            "blocker_key": "BCP-0001-002",
            "blocked_key": "BCP-0001-001",
        }
    ]
    assert relation_conflicts["wrong_direction"] == [
        {
            "declared_blocker_key": "BCP-0001-001",
            "declared_blocked_key": "BCP-0001-002",
            "observed_blocker_key": "BCP-0001-002",
            "observed_blocked_key": "BCP-0001-001",
        }
    ]
