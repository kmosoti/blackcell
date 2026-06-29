"""Identity, target, and manual approval policy."""

from copy import deepcopy

import pytest

from blackcell.config.model import BlackcellConfig
from blackcell.contracts.errors import ConflictFailure, PermissionFailure, PolicyFailure
from blackcell.contracts.markers import plan_marker
from blackcell.contracts.plan import PlanSpec
from blackcell.policy.approval import verify_approved_project
from blackcell.policy.identity import verify_plan_target, verify_viewer_and_team


def viewer(config: BlackcellConfig) -> dict[str, str]:
    return {
        "id": config.identity.planner_user_id,
        "name": config.identity.planner_name,
        "email": config.identity.planner_email,
    }


def team(config: BlackcellConfig) -> dict[str, str | None]:
    return {
        "id": config.linear.team_id,
        "key": config.linear.team_key,
        "name": config.linear.team_name,
        "archivedAt": None,
    }


def test_identity_and_plan_target_accept_pinned_values(
    config: BlackcellConfig, plan: PlanSpec
) -> None:
    verify_viewer_and_team(viewer(config), team(config), config)
    verify_plan_target(plan, config)


def test_identity_rejects_wrong_planner(config: BlackcellConfig) -> None:
    actual = viewer(config)
    actual["id"] = "other-user"

    with pytest.raises(PermissionFailure, match="unexpected planner"):
        verify_viewer_and_team(actual, team(config), config)


@pytest.mark.parametrize(
    "actual_team",
    [
        None,
        {"id": "wrong", "key": "BLCELL", "name": "BlackCell", "archivedAt": None},
        {
            "id": "f200f3d3-ca1b-4cc8-a9c2-3d218a278332",
            "key": "BLCELL",
            "name": "BlackCell",
            "archivedAt": "2026-06-28T00:00:00Z",
        },
    ],
)
def test_identity_rejects_missing_mismatched_or_archived_team(
    config: BlackcellConfig, actual_team: dict[str, str | None] | None
) -> None:
    with pytest.raises(PermissionFailure):
        verify_viewer_and_team(viewer(config), actual_team, config)


def test_plan_target_rejects_wrong_repository(config: BlackcellConfig, plan: PlanSpec) -> None:
    changed = plan.model_copy(
        update={"repository": plan.repository.model_copy(update={"name": "other"})}
    )

    with pytest.raises(PolicyFailure, match="repository"):
        verify_plan_target(changed, config)


def test_approval_rejects_non_materializable_status(
    config: BlackcellConfig, plan: PlanSpec
) -> None:
    project = {
        "status": {"name": config.linear.project_statuses.proposal},
        "description": plan_marker(plan),
    }

    with pytest.raises(PolicyFailure, match="materializable project state"):
        verify_approved_project(project, plan, config)


def test_approval_rejects_completed_or_canceled_status(
    config: BlackcellConfig, plan: PlanSpec
) -> None:
    for status in (
        config.linear.project_statuses.completed,
        config.linear.project_statuses.canceled,
    ):
        project = {
            "status": {"name": status},
            "description": plan_marker(plan),
        }

        with pytest.raises(PolicyFailure, match="materializable project state"):
            verify_approved_project(project, plan, config)


def test_approval_rejects_digest_divergence(config: BlackcellConfig, plan: PlanSpec) -> None:
    project = {
        "status": {"name": config.linear.project_statuses.approved},
        "description": plan_marker(plan).replace(plan.digest().value, "0" * 64),
    }

    with pytest.raises(ConflictFailure, match="digest"):
        verify_approved_project(project, plan, config)


@pytest.mark.parametrize("status_attr", ["approved", "active"])
def test_approval_accepts_exact_marker_for_materializable_states(
    config: BlackcellConfig, plan: PlanSpec, status_attr: str
) -> None:
    status = getattr(config.linear.project_statuses, status_attr)
    project = {
        "status": {"name": status},
        "description": f"Human summary\n\n{plan_marker(plan)}",
    }

    verify_approved_project(deepcopy(project), plan, config)
