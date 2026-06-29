"""Facade classification and explicit execution aspects."""

import json
from io import StringIO
from pathlib import Path

from blackcell.contracts.errors import ConflictFailure
from blackcell.contracts.facade import (
    Authority,
    Effect,
    Facade,
    InvariantAspect,
    operation,
)
from blackcell.ledger.sqlite import Chronicle, EventType
from blackcell.runtime.execution import (
    AnomalyAspect,
    OperationExecutor,
    PendingOutcome,
    StructuredEventAspect,
)
from blackcell.runtime.observability import JsonLineEventSink
from blackcell.sdk.operations import OPERATIONS, OperationId


def test_operation_catalog_has_unique_names_and_baseline_aspects() -> None:
    assert len({item.name for item in OPERATIONS.values()}) == len(OPERATIONS)
    assert set(OPERATIONS) == set(OperationId)
    for item in OPERATIONS.values():
        assert InvariantAspect.OUTPUT in item.aspects
        assert InvariantAspect.OBSERVABILITY in item.aspects


def test_materialization_contract_classifies_remote_safety_invariants() -> None:
    contract = OPERATIONS[OperationId.DIRECTIVE_MATERIALIZE]

    assert contract.facade is Facade.DIRECTIVE
    assert contract.authority is Authority.LINEAR
    assert contract.effect is Effect.MUTATE
    assert {
        InvariantAspect.AUTHENTICATION,
        InvariantAspect.IDENTITY,
        InvariantAspect.STATE,
        InvariantAspect.IMMUTABILITY,
        InvariantAspect.IDEMPOTENCY,
    } <= contract.aspects


def test_executor_serializes_pending_outcome_and_structured_events() -> None:
    stream = StringIO()
    executor = OperationExecutor((StructuredEventAspect(JsonLineEventSink(stream)),))
    contract = operation(
        "echo.verify",
        Facade.ECHO,
        Authority.GITHUB,
        Effect.READ,
        InvariantAspect.IMMUTABILITY,
    )

    result = executor.execute(
        contract,
        lambda: PendingOutcome(
            code="pending_projection",
            message="Projection is pending.",
            recovery="blackcell directive reconcile BCP-0001",
            data={"plan_id": "BCP-0001"},
        ),
        plan_id="BCP-0001",
    )

    assert result.status == "pending"
    assert result.error is not None
    assert result.error.code == "pending_projection"
    assert result.meta is not None
    assert result.meta.operation == "echo.verify"
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["event"] for event in events] == [
        "operation.started",
        "operation.completed",
    ]
    assert events[0]["correlation_id"] == events[1]["correlation_id"]
    assert result.meta.correlation_id == events[0]["correlation_id"]


def test_conflicts_are_recorded_by_the_anomaly_aspect(tmp_path: Path) -> None:
    chronicle = Chronicle(tmp_path / "chronicle.sqlite3")
    executor = OperationExecutor((AnomalyAspect(chronicle),))
    contract = operation(
        "operation.verify",
        Facade.OPERATION,
        Authority.LINEAR,
        Effect.READ,
        InvariantAspect.IMMUTABILITY,
    )

    def conflict() -> dict[str, object]:
        raise ConflictFailure(
            "Digest diverged.",
            details={"plan_id": "BCP-0001"},
        )

    result = executor.execute(contract, conflict, plan_id="BCP-0001")

    assert result.status == "error"
    event = chronicle.events("BCP-0001")[-1]
    assert event.event_type == EventType.ANOMALY_DETECTED
    assert event.payload["operation"] == "operation.verify"
    assert event.payload["correlation_id"]
