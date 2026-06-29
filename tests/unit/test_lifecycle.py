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
            config.linear.project_statuses.active,
            ProjectCapability.MATERIALIZE_ASSIGNMENTS,
            message="not materializable",
        )
        is ProjectState.ACTIVE
    )
    assert (
        lifecycle.require(
            config.linear.project_statuses.active,
            ProjectCapability.VERIFY_IMMUTABLE,
            message="not immutable",
        )
        is ProjectState.ACTIVE
    )


def test_lifecycle_rejects_unknown_or_disallowed_state(
    config: BlackcellConfig,
) -> None:
    lifecycle = ProjectStateMachine(config.linear.project_statuses)

    with pytest.raises(PolicyFailure):
        lifecycle.require(
            None,
            ProjectCapability.MATERIALIZE_ASSIGNMENTS,
            message="not approved",
        )
    with pytest.raises(PolicyFailure):
        lifecycle.require(
            "Triaged",
            ProjectCapability.MATERIALIZE_ASSIGNMENTS,
            message="not approved",
        )
    with pytest.raises(PolicyFailure):
        lifecycle.require(
            config.linear.project_statuses.proposal,
            ProjectCapability.MATERIALIZE_ASSIGNMENTS,
            message="not approved",
        )
    with pytest.raises(PolicyFailure):
        lifecycle.require(
            config.linear.project_statuses.completed,
            ProjectCapability.MATERIALIZE_ASSIGNMENTS,
            message="not approved",
        )
    with pytest.raises(PolicyFailure):
        lifecycle.require(
            config.linear.project_statuses.canceled,
            ProjectCapability.MATERIALIZE_ASSIGNMENTS,
            message="not approved",
        )
