"""Semantic project lifecycle capabilities."""

import pytest

from blackcell.config.model import BlackcellConfig
from blackcell.contracts.errors import PolicyFailure
from blackcell.policy.lifecycle import (
    ProjectCapability,
    ProjectState,
    ProjectStateMachine,
)


def test_lifecycle_maps_provider_names_to_semantic_capabilities(
    config: BlackcellConfig,
) -> None:
    lifecycle = ProjectStateMachine(config.linear.project_statuses)

    assert (
        lifecycle.require(
            "Proposal",
            ProjectCapability.RECONCILE_PRESENTATION,
            message="not mutable",
        )
        is ProjectState.PROPOSAL
    )
    assert (
        lifecycle.require(
            "Approved",
            ProjectCapability.MATERIALIZE_ASSIGNMENTS,
            message="not approved",
        )
        is ProjectState.APPROVED
    )
    assert (
        lifecycle.require(
            "Active",
            ProjectCapability.VERIFY_IMMUTABLE,
            message="not immutable",
        )
        is ProjectState.ACTIVE
    )


@pytest.mark.parametrize("status", [None, "Triaged", "Active"])
def test_lifecycle_rejects_unknown_or_disallowed_state(
    config: BlackcellConfig,
    status: str | None,
) -> None:
    lifecycle = ProjectStateMachine(config.linear.project_statuses)

    with pytest.raises(PolicyFailure):
        lifecycle.require(
            status,
            ProjectCapability.MATERIALIZE_ASSIGNMENTS,
            message="not approved",
        )
