"""Shared deterministic fixtures for BlackCell tests."""

from collections.abc import Callable
from typing import Any

import pytest

from blackcell.config.model import BlackcellConfig
from blackcell.contracts.plan import PlanSpec


def config_data() -> dict[str, Any]:
    return {
        "schema_version": "0.1",
        "identity": {
            "planner_user_id": "1ed22c47-390f-41e6-b63d-497f58cccb3b",
            "planner_name": "Kennedy Mosoti",
            "planner_email": "kennedy.rmosoti@gmail.com",
            "owner_github_login": "kmosoti",
            "executor_github_login": "kz-harbringer",
        },
        "repository": {
            "owner": "kmosoti",
            "name": "blackcell",
            "default_branch": "main",
        },
        "linear": {
            "team_id": "f200f3d3-ca1b-4cc8-a9c2-3d218a278332",
            "team_key": "BLCELL",
            "team_name": "BlackCell",
            "planning_authority": "linear",
            "issue_projection_provider": "linear_github_sync",
            "issue_sync_mode": "two_way",
            "project_presentation": {
                "brand": "BlackCell",
                "color": "#111827",
                "repository_link_label": "BlackCell repository",
            },
            "project_workflow": {
                "lead_user_id": "1ed22c47-390f-41e6-b63d-497f58cccb3b",
                "member_user_ids": ["1ed22c47-390f-41e6-b63d-497f58cccb3b"],
                "priority": "high",
                "label_names": ["BlackCell", "BCP-0001"],
            },
            "project_statuses": {
                "proposal": "Proposal",
                "approved": "Approved",
                "active": "In Progress",
                "completed": "Completed",
                "canceled": "Canceled",
            },
            "issue_states": {
                "backlog": "Backlog",
                "ready": "Ready",
                "in_progress": "In Progress",
                "in_review": "In Review",
                "done": "Done",
                "canceled": "Canceled",
            },
        },
        "ledger": {"backend": "sqlite", "append_only": True},
        "materialization": {
            "marker_prefix": "blackcell",
            "projection_timeout_seconds": 120,
        },
        "publication": {
            "commit_email": "290864439+kz-harbringer@users.noreply.github.com",
            "push_remote": "origin",
            "push_ssh_host": "github.com-kz",
            "branch_prefix": "blackcell/",
            "pull_request_readiness": "ready_for_review",
        },
    }


def plan_data() -> dict[str, Any]:
    return {
        "schema_version": "0.1",
        "plan_id": "BCP-0001",
        "revision": 1,
        "title": "Planner proof",
        "objective": "Prove deterministic materialization.",
        "repository": {"owner": "kmosoti", "name": "blackcell"},
        "linear": {
            "team_id": "f200f3d3-ca1b-4cc8-a9c2-3d218a278332",
            "team_key": "BLCELL",
            "project_name": "Planner proof",
        },
        "work_items": [
            {
                "key": "BCP-0001-001",
                "title": "Foundation",
                "description": "Build the foundation.",
                "type": "task",
                "priority": "high",
                "labels": ["area:foundation"],
                "acceptance": ["Foundation is verified."],
                "parent_key": None,
                "blocked_by": [],
            },
            {
                "key": "BCP-0001-002",
                "title": "Dependent child",
                "description": "Build on the foundation.",
                "type": "task",
                "priority": "medium",
                "labels": ["area:contracts"],
                "acceptance": ["Dependency is represented."],
                "parent_key": "BCP-0001-001",
                "blocked_by": ["BCP-0001-001"],
            },
            {
                "key": "BCP-0001-003",
                "title": "Sibling",
                "description": "Build the sibling assignment.",
                "type": "chore",
                "priority": "low",
                "labels": ["area:sync"],
                "acceptance": ["Sibling is represented."],
                "parent_key": None,
                "blocked_by": [],
            },
        ],
    }


@pytest.fixture
def config() -> BlackcellConfig:
    return BlackcellConfig.model_validate(config_data())


@pytest.fixture
def plan() -> PlanSpec:
    return PlanSpec.model_validate(plan_data())


@pytest.fixture
def make_plan() -> Callable[[dict[str, Any] | None], PlanSpec]:
    def factory(overrides: dict[str, Any] | None = None) -> PlanSpec:
        data = plan_data()
        if overrides:
            data.update(overrides)
        return PlanSpec.model_validate(data)

    return factory
