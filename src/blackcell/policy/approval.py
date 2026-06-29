"""Manual approval and immutable digest policy."""

from typing import Any

from blackcell.config.model import BlackcellConfig
from blackcell.contracts.errors import ConflictFailure
from blackcell.contracts.markers import plan_marker
from blackcell.contracts.plan import PlanSpec
from blackcell.policy.lifecycle import ProjectCapability, ProjectStateMachine


def verify_approved_project(
    project: dict[str, Any], plan: PlanSpec, config: BlackcellConfig
) -> None:
    status_name = (project.get("status") or {}).get("name")
    ProjectStateMachine(config.linear.project_statuses).require(
        status_name,
        ProjectCapability.MATERIALIZE_ASSIGNMENTS,
        message="Linear operation is not in a materializable project state.",
        recovery=(
            f"Move the Linear Project to {config.linear.project_statuses.approved} "
            f"or {config.linear.project_statuses.active}."
        ),
    )
    expected_marker = plan_marker(plan)
    directive_text = project.get("content") or project.get("description") or ""
    if expected_marker not in directive_text:
        raise ConflictFailure(
            "Approved operation digest does not match the local directive.",
            recovery=f"Review the approved Linear Project for {plan.plan_id}.",
            details={"expected_marker": expected_marker},
        )
