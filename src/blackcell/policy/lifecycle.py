"""Semantic Linear Project lifecycle and capability guards."""

from dataclasses import dataclass
from enum import StrEnum

from blackcell.config.model import ProjectStatusesConfig
from blackcell.contracts.errors import PolicyFailure


class ProjectState(StrEnum):
    PROPOSAL = "proposal"
    APPROVED = "approved"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELED = "canceled"


class ProjectCapability(StrEnum):
    RECONCILE_PRESENTATION = "reconcile_presentation"
    RECONCILE_WORKFLOW = "reconcile_workflow"
    MATERIALIZE_ASSIGNMENTS = "materialize_assignments"
    VERIFY_IMMUTABLE = "verify_immutable"


_CAPABILITIES = {
    ProjectState.PROPOSAL: frozenset(
        {
            ProjectCapability.RECONCILE_PRESENTATION,
            ProjectCapability.RECONCILE_WORKFLOW,
        }
    ),
    ProjectState.APPROVED: frozenset(
        {
            ProjectCapability.MATERIALIZE_ASSIGNMENTS,
            ProjectCapability.VERIFY_IMMUTABLE,
        }
    ),
    ProjectState.ACTIVE: frozenset({ProjectCapability.VERIFY_IMMUTABLE}),
    ProjectState.COMPLETED: frozenset({ProjectCapability.VERIFY_IMMUTABLE}),
    ProjectState.CANCELED: frozenset({ProjectCapability.VERIFY_IMMUTABLE}),
}


@dataclass(frozen=True, slots=True)
class ProjectStateMachine:
    names: ProjectStatusesConfig

    def resolve(self, provider_name: str | None) -> ProjectState:
        configured = {
            self.names.proposal: ProjectState.PROPOSAL,
            self.names.approved: ProjectState.APPROVED,
            self.names.active: ProjectState.ACTIVE,
            self.names.completed: ProjectState.COMPLETED,
            self.names.canceled: ProjectState.CANCELED,
        }
        if provider_name is None or provider_name not in configured:
            raise PolicyFailure(
                "Linear Project has an unknown lifecycle status.",
                details={
                    "actual_status": provider_name,
                    "configured_statuses": sorted(configured),
                },
            )
        return configured[provider_name]

    def require(
        self,
        provider_name: str | None,
        capability: ProjectCapability,
        *,
        message: str,
        recovery: str | None = None,
    ) -> ProjectState:
        state = self.resolve(provider_name)
        if capability not in _CAPABILITIES[state]:
            raise PolicyFailure(
                message,
                recovery=recovery,
                details={
                    "actual_status": provider_name,
                    "state": state,
                    "required_capability": capability,
                },
            )
        return state
