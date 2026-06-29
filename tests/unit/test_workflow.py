"""Orchestrated workflow state-machine behavior."""

from pathlib import Path
from typing import Any, cast

from blackcell.config.model import BlackcellConfig
from blackcell.contracts.plan import PlanSpec
from blackcell.contracts.result import ResultEnvelope
from blackcell.ledger.sqlite import Chronicle, EventType
from blackcell.sdk.client import BlackcellClient
from blackcell.services.plan_store import PlanStore
from tests.unit.test_plan_service import FakeLinear, FakePlanStore


def test_workflow_run_records_steps_and_waits_for_manual_approval(
    tmp_path: Path,
    config: BlackcellConfig,
    plan: PlanSpec,
) -> None:
    chronicle = Chronicle(tmp_path / "chronicle.sqlite3")
    client = BlackcellClient(
        config,
        chronicle=chronicle,
        store=cast(PlanStore, FakePlanStore(plan)),
        linear=cast(Any, FakeLinear(config, plan)),
    )

    result: ResultEnvelope = client.workflow_run(plan.plan_id)

    assert result.status == "pending"
    assert result.error is not None
    assert result.error.code == "approval_wait"
    assert [step["step_id"] for step in result.data["steps"]] == [
        "schema_audit",
        "proposal_sync",
        "approval_wait",
    ]
    assert result.data["steps"][-1]["invariant_group"] == "lifecycle"
    events = [
        event
        for event in chronicle.events(plan.plan_id)
        if event.event_type
        in {
            EventType.WORKFLOW_STEP_COMPLETED,
            EventType.WORKFLOW_STEP_PENDING,
        }
    ]
    assert [event.payload["step_id"] for event in events] == [
        "schema_audit",
        "proposal_sync",
        "approval_wait",
    ]
