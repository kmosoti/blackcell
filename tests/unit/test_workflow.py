"""Orchestrated workflow state-machine behavior."""

from pathlib import Path
from typing import Any, cast

from blackcell.config.model import BlackcellConfig
from blackcell.contracts.plan import PlanSpec
from blackcell.contracts.result import ResultEnvelope
from blackcell.ledger.sqlite import Chronicle, EventType
from blackcell.sdk.client import BlackcellClient
from blackcell.services.plan_store import PlanStore
from blackcell.services.rendering import render_project_description
from tests.unit.test_plan_service import FakeLinear, FakePlanStore


class _FakeMaterializationService:
    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    def materialize(self, _plan_id: str) -> dict[str, Any]:
        return self._result


def _contract_clean_linear(config: BlackcellConfig, plan: PlanSpec, *, status: str) -> FakeLinear:
    linear = FakeLinear(
        config,
        plan,
        status=status,
        repository_link_label=config.linear.project_presentation.repository_link_label,
    )
    linear.project.update(
        {
            "content": render_project_description(plan, config).replace("\n- ", "\n* "),
            "color": config.linear.project_presentation.color,
        }
    )
    return linear


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
        "project_contract",
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
        "project_contract",
        "approval_wait",
    ]


def test_workflow_run_marks_dependency_relations_pending_step(
    tmp_path: Path,
    config: BlackcellConfig,
    plan: PlanSpec,
) -> None:
    chronicle = Chronicle(tmp_path / "chronicle.sqlite3")
    client = BlackcellClient(
        config,
        chronicle=chronicle,
        store=cast(PlanStore, FakePlanStore(plan)),
        linear=cast(
            Any,
            _contract_clean_linear(
                config,
                plan,
                status=config.linear.project_statuses.approved,
            ),
        ),
    )
    cast(Any, client)._materialization_service = lambda: _FakeMaterializationService(
        {
            "assignment_mutations": 0,
            "relation_mutations": 1,
            "verified_echoes": [],
            "pending_relations": [
                {
                    "item_key": "BCP-0001-002",
                    "blocker_id": "issue-1",
                    "blocked_id": "issue-2",
                }
            ],
            "pending_echoes": [],
            "recovery": "reconcile relations",
        }
    )

    result: ResultEnvelope = client.workflow_run(plan.plan_id)

    assert result.status == "pending"
    assert result.error is not None
    assert result.error.code == "dependency_relations_pending"
    assert [step["step_id"] for step in result.data["steps"]] == [
        "schema_audit",
        "project_contract",
        "approval_wait",
        "assignment_materialize",
        "dependency_relations",
    ]
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
        "project_contract",
        "approval_wait",
        "assignment_materialize",
        "dependency_relations",
    ]


def test_workflow_run_marks_github_echoes_pending_step(
    tmp_path: Path,
    config: BlackcellConfig,
    plan: PlanSpec,
) -> None:
    chronicle = Chronicle(tmp_path / "chronicle.sqlite3")
    client = BlackcellClient(
        config,
        chronicle=chronicle,
        store=cast(PlanStore, FakePlanStore(plan)),
        linear=cast(
            Any,
            _contract_clean_linear(
                config,
                plan,
                status=config.linear.project_statuses.approved,
            ),
        ),
    )
    cast(Any, client)._materialization_service = lambda: _FakeMaterializationService(
        {
            "assignment_mutations": 0,
            "relation_mutations": 0,
            "verified_echoes": [],
            "pending_relations": [],
            "pending_echoes": ["BCP-0001-002"],
            "recovery": "reconcile echoes",
        }
    )

    result: ResultEnvelope = client.workflow_run(plan.plan_id)

    assert result.status == "pending"
    assert result.error is not None
    assert result.error.code == "github_echoes_pending"
    assert [step["step_id"] for step in result.data["steps"]] == [
        "schema_audit",
        "project_contract",
        "approval_wait",
        "assignment_materialize",
        "dependency_relations",
        "github_echoes",
    ]
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
        "project_contract",
        "approval_wait",
        "assignment_materialize",
        "dependency_relations",
        "github_echoes",
    ]
