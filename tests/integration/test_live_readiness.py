"""Opt-in live readiness checks.

These tests are skipped before constructing a client unless
BLACKCELL_INTEGRATION=1 is explicitly set.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BLACKCELL_INTEGRATION") != "1",
    reason="set BLACKCELL_INTEGRATION=1 to enable live provider checks",
)


def test_live_linear_readiness() -> None:
    from blackcell.sdk.client import BlackcellClient

    with BlackcellClient.from_environment() as client:
        result = client.pulse("linear")
    assert result.status == "ok", result.model_dump(mode="json")


def test_live_github_readiness() -> None:
    from blackcell.sdk.client import BlackcellClient

    with BlackcellClient.from_environment() as client:
        result = client.pulse("github")
    assert result.status == "ok", result.model_dump(mode="json")


def test_live_linear_project_contract() -> None:
    from blackcell.contracts.plan import PlanSpec
    from blackcell.sdk.client import BlackcellClient

    plan = PlanSpec.from_file("examples/BCP-0001.json")
    with BlackcellClient.from_environment() as client:
        client.store.save(plan)
        result = client.inspect_operation(plan.plan_id)
    assert result.status == "ok", result.model_dump(mode="json")
    assert result.data["matches"] is True, result.model_dump(mode="json")
