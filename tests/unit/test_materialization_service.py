"""Service-level materialization proof with deterministic in-memory providers."""

from pathlib import Path
from typing import Any

from blackcell.config.model import BlackcellConfig
from blackcell.contracts.plan import PlanSpec
from blackcell.ledger.sqlite import Chronicle
from blackcell.services.materialization_service import MaterializationService
from blackcell.services.rendering import (
    render_project_description,
    render_project_summary,
)


class FakePlanStore:
    def __init__(self, plan: PlanSpec) -> None:
        self.plan = plan

    def load(self, plan_id: str) -> PlanSpec:
        assert plan_id == self.plan.plan_id
        return self.plan


class FakeVerification:
    def verify_echoes(
        self,
        plan: PlanSpec,
        *,
        timeout_seconds: float = 0,
        poll_interval: float = 2,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        del timeout_seconds, poll_interval
        return (
            [
                {
                    "item_key": item.key,
                    "number": index,
                    "title": item.title,
                    "url": f"https://github.test/issues/{index}",
                }
                for index, item in enumerate(plan.work_items, start=1)
            ],
            [],
        )


class FakeLinear:
    def __init__(self, config: BlackcellConfig, plan: PlanSpec) -> None:
        self.config = config
        self.plan = plan
        self.issue_mutations = 0
        self.relation_mutations = 0
        self.issues: list[dict[str, Any]] = []
        self.project: dict[str, Any] = {
            "id": "project-1",
            "name": plan.linear.project_name,
            "url": "https://linear.test/project-1",
            "status": {"name": config.linear.project_statuses.approved},
            "description": render_project_summary(plan),
            "content": render_project_description(plan, config),
            "teams": {"nodes": [{"id": config.linear.team_id}]},
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

    def find_projects_by_marker(self, team_id: str, marker: str) -> list[dict[str, Any]]:
        assert team_id == self.config.linear.team_id
        assert marker in self.project["content"]
        return [self.project]

    def workflow_states(self, team_id: str) -> list[dict[str, str]]:
        assert team_id == self.config.linear.team_id
        return [{"id": "state-backlog", "name": self.config.linear.issue_states.backlog}]

    def issue_labels(self, team_id: str) -> list[dict[str, str]]:
        assert team_id == self.config.linear.team_id
        names = {label for item in self.plan.work_items for label in item.labels}
        return [{"id": f"label-{name}", "name": name} for name in sorted(names)]

    def project_issues(self, project_id: str) -> list[dict[str, Any]]:
        assert project_id == self.project["id"]
        return self.issues

    def team_issues(self, team_id: str) -> list[dict[str, Any]]:
        assert team_id == self.config.linear.team_id
        return self.issues

    def issue_relations(self, issue_id: str) -> list[dict[str, Any]]:
        issue = next(issue for issue in self.issues if issue["id"] == issue_id)
        return issue["relations"]["nodes"]

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
    ) -> dict[str, Any]:
        assert team_id == self.config.linear.team_id
        assert project_id == self.project["id"]
        assert state_id == "state-backlog"
        self.issue_mutations += 1
        issue = {
            "id": f"issue-{self.issue_mutations}",
            "identifier": f"BLCELL-{self.issue_mutations}",
            "title": title,
            "description": description,
            "priority": priority,
            "parent": {"id": parent_id} if parent_id else None,
            "team": {"id": team_id},
            "project": {"id": project_id},
            "state": {"id": state_id},
            "labels": {
                "nodes": [
                    {"id": label_id, "name": label_id.removeprefix("label-")}
                    for label_id in label_ids
                ]
            },
            "relations": {"nodes": []},
            "inverseRelations": {"nodes": []},
        }
        return issue

    def create_blocking_relation(self, blocker_id: str, blocked_id: str) -> dict[str, Any]:
        self.relation_mutations += 1
        blocker = next(issue for issue in self.issues if issue["id"] == blocker_id)
        relation = {
            "id": f"relation-{self.relation_mutations}",
            "type": "blocks",
            "issue": {"id": blocker_id},
            "relatedIssue": {"id": blocked_id},
        }
        blocker["relations"]["nodes"].append(relation)
        return relation


def build_service(
    tmp_path: Path, config: BlackcellConfig, plan: PlanSpec
) -> tuple[MaterializationService, FakeLinear, Chronicle]:
    chronicle = Chronicle(tmp_path / "chronicle.sqlite3")
    linear = FakeLinear(config, plan)
    service = MaterializationService(
        config,
        chronicle,
        FakePlanStore(plan),
        linear,
        FakeVerification(),
    )
    return service, linear, chronicle


def test_repeated_materialization_performs_zero_remote_mutations(
    tmp_path: Path, config: BlackcellConfig, plan: PlanSpec
) -> None:
    service, linear, chronicle = build_service(tmp_path, config, plan)

    first = service.materialize(plan.plan_id, projection_timeout=0)
    issue_mutations = linear.issue_mutations
    relation_mutations = linear.relation_mutations
    linear.project["content"] = linear.project["content"].replace("\n- ", "\n* ")
    for issue in linear.issues:
        issue["description"] = issue["description"].replace("\n- ", "\n* ")
    second = service.materialize(plan.plan_id, projection_timeout=0)

    assert first["assignment_mutations"] == len(plan.work_items)
    assert first["relation_mutations"] == 1
    assert second["assignment_mutations"] == 0
    assert second["relation_mutations"] == 0
    assert linear.issue_mutations == issue_mutations == len(plan.work_items)
    assert linear.relation_mutations == relation_mutations == 1
    assert len(linear.issues) == len(plan.work_items)
    completed = [
        event
        for event in chronicle.events(plan.plan_id)
        if event.event_type == "materialization_completed"
    ]
    assert len(completed) == 2
    assert completed[-1].payload["assignment_mutations"] == 0
    assert completed[-1].payload["relation_mutations"] == 0
