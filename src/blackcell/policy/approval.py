"""Manual approval and immutable digest policy."""

from typing import Any

from blackcell.config.model import BlackcellConfig
from blackcell.contracts.errors import ConflictFailure, PolicyFailure
from blackcell.contracts.markers import plan_marker
from blackcell.contracts.plan import PlanSpec


def verify_approved_project(
    project: dict[str, Any], plan: PlanSpec, config: BlackcellConfig
) -> None:
    status_name = (project.get("status") or {}).get("name")
    if status_name != config.linear.project_statuses.approved:
        raise PolicyFailure(
            "Linear operation is not manually approved.",
            recovery=f"Move the Linear Project to {config.linear.project_statuses.approved}.",
            details={"actual_status": status_name},
        )
    expected_marker = plan_marker(plan)
    directive_text = project.get("content") or project.get("description") or ""
    if expected_marker not in directive_text:
        raise ConflictFailure(
            "Approved operation digest does not match the local directive.",
            recovery=f"Review the approved Linear Project for {plan.plan_id}.",
            details={"expected_marker": expected_marker},
        )
