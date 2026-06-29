"""Pinned planner, team, and repository checks."""

from typing import Any

from blackcell.config.model import BlackcellConfig
from blackcell.contracts.errors import PermissionFailure, PolicyFailure
from blackcell.contracts.plan import PlanSpec


def verify_plan_target(plan: PlanSpec, config: BlackcellConfig) -> None:
    expected_repository = (config.repository.owner, config.repository.name)
    actual_repository = (plan.repository.owner, plan.repository.name)
    if actual_repository != expected_repository:
        raise PolicyFailure(
            "Directive repository does not match configured repository.",
            details={
                "expected": "/".join(expected_repository),
                "actual": "/".join(actual_repository),
            },
        )
    if plan.linear.team_key != config.linear.team_key:
        raise PolicyFailure(
            "Directive Linear team key does not match configured team.",
            details={"expected": config.linear.team_key, "actual": plan.linear.team_key},
        )
    if plan.linear.team_id is not None and plan.linear.team_id != config.linear.team_id:
        raise PolicyFailure(
            "Directive Linear team ID does not match configured team.",
            details={"expected": config.linear.team_id, "actual": plan.linear.team_id},
        )


def verify_viewer_and_team(
    viewer: dict[str, Any],
    team: dict[str, Any] | None,
    config: BlackcellConfig,
) -> None:
    expected = config.identity
    actual = {
        "id": viewer.get("id"),
        "name": viewer.get("name"),
        "email": viewer.get("email"),
    }
    if actual != {
        "id": expected.planner_user_id,
        "name": expected.planner_name,
        "email": expected.planner_email,
    }:
        raise PermissionFailure(
            "Linear credential belongs to an unexpected planner.",
            details={"expected_id": expected.planner_user_id, "actual": actual},
        )
    if team is None:
        raise PermissionFailure(
            "Configured Linear team was not found.",
            details={"team_id": config.linear.team_id},
        )
    actual_team = {
        "id": team.get("id"),
        "key": team.get("key"),
        "name": team.get("name"),
    }
    expected_team = {
        "id": config.linear.team_id,
        "key": config.linear.team_key,
        "name": config.linear.team_name,
    }
    if actual_team != expected_team or team.get("archivedAt") is not None:
        raise PermissionFailure(
            "Configured Linear team identity is missing, mismatched, or archived.",
            details={"expected": expected_team, "actual": actual_team},
        )
