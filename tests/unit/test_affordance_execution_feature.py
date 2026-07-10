from dataclasses import replace
from datetime import UTC, datetime

import pytest

from blackcell.features.authorize_action import (
    AuthorizationDecision,
    AuthorizationFinding,
    AuthorizationOutcome,
)
from blackcell.features.execute_affordance import (
    AdapterOutcome,
    AffordanceArgument,
    AffordanceArgumentSpec,
    AffordanceDefinition,
    AffordanceExecutionHandler,
    AffordanceInvocation,
    ExecutionDenied,
    ExecutionResult,
    ExecutionStatus,
    IdempotencyKeyConflict,
    SideEffectClass,
    UncertainExecutionError,
)

NOW = datetime(2026, 7, 10, 21, tzinfo=UTC)


class Journal:
    def __init__(self) -> None:
        self.results: dict[str, ExecutionResult] = {}

    def get(self, idempotency_key: str) -> ExecutionResult | None:
        return self.results.get(idempotency_key)

    def save(self, result: ExecutionResult) -> None:
        self.results[result.idempotency_key] = result


class Adapter:
    adapter_id = "fixture"

    def __init__(self, *, uncertain: bool = False) -> None:
        self.uncertain = uncertain
        self.execute_calls = 0
        self.reconcile_calls = 0

    def execute(self, invocation, definition):
        self.execute_calls += 1
        if self.uncertain:
            self.uncertain = False
            raise UncertainExecutionError
        return AdapterOutcome(True, "sha256:result", NOW)

    def reconcile(self, invocation, definition, previous):
        self.reconcile_calls += 1
        return AdapterOutcome(True, "sha256:reconciled", NOW)


def test_denied_authorization_never_calls_adapter() -> None:
    adapter = Adapter()
    handler = AffordanceExecutionHandler({"fixture": adapter}, Journal())

    with pytest.raises(ExecutionDenied):
        handler.handle(_invocation(), _definition(), _decision(AuthorizationOutcome.DENY))

    assert adapter.execute_calls == 0


def test_allowed_execution_is_journaled_and_exact_retry_does_not_repeat() -> None:
    adapter = Adapter()
    journal = Journal()
    handler = AffordanceExecutionHandler({"fixture": adapter}, journal)

    first = handler.handle(_invocation(), _definition(), _decision())
    second = handler.handle(_invocation(), _definition(), _decision())

    assert first == second
    assert first.status is ExecutionStatus.SUCCEEDED
    assert adapter.execute_calls == 1
    assert journal.results["invoke-once"] == first


def test_completed_result_rejects_key_reused_by_another_invocation() -> None:
    adapter = Adapter()
    journal = Journal()
    handler = AffordanceExecutionHandler({"fixture": adapter}, journal)
    first = handler.handle(_invocation(), _definition(), _decision())

    with pytest.raises(IdempotencyKeyConflict, match="invocation_id"):
        handler.handle(
            replace(_invocation(), invocation_id="invocation:2"),
            _definition(),
            _decision(),
        )

    assert adapter.execute_calls == 1
    assert journal.results["invoke-once"] == first


def test_completed_result_rejects_changed_payload_under_the_same_identity() -> None:
    adapter = Adapter()
    journal = Journal()
    handler = AffordanceExecutionHandler({"fixture": adapter}, journal)
    first = handler.handle(_invocation(), _definition(), _decision())
    changed = replace(_invocation(), arguments=(AffordanceArgument("path", "pyproject.toml"),))

    with pytest.raises(IdempotencyKeyConflict, match="execution_identity_digest"):
        handler.handle(changed, _definition(), _decision())

    assert adapter.execute_calls == 1
    assert journal.results["invoke-once"] == first


def test_unknown_side_effect_is_reconciled_before_retry() -> None:
    adapter = Adapter(uncertain=True)
    handler = AffordanceExecutionHandler({"fixture": adapter}, Journal())

    unknown = handler.handle(_invocation(), _definition(), _decision())
    reconciled = handler.handle(_invocation(), _definition(), _decision())

    assert unknown.status is ExecutionStatus.UNKNOWN
    assert reconciled.status is ExecutionStatus.SUCCEEDED
    assert reconciled.reconciled
    assert (adapter.execute_calls, adapter.reconcile_calls) == (1, 1)


def test_unknown_result_rejects_changed_definition_before_reconciliation() -> None:
    adapter = Adapter(uncertain=True)
    journal = Journal()
    handler = AffordanceExecutionHandler({"fixture": adapter}, journal)
    unknown = handler.handle(_invocation(), _definition(), _decision())

    with pytest.raises(IdempotencyKeyConflict, match="execution_identity_digest"):
        handler.handle(
            _invocation(),
            replace(_definition(), timeout_seconds=20.0),
            _decision(),
        )

    assert adapter.execute_calls == 1
    assert adapter.reconcile_calls == 0
    assert journal.results["invoke-once"] == unknown


def test_arguments_are_validated_before_adapter_call() -> None:
    adapter = Adapter()
    invocation = AffordanceInvocation(
        "invocation:1",
        "proposal:1",
        "inspect",
        (AffordanceArgument("command", "unsafe"),),
        "invoke-once",
        NOW,
    )

    with pytest.raises(ValueError, match="invalid affordance arguments"):
        AffordanceExecutionHandler({"fixture": adapter}, Journal()).handle(
            invocation, _definition(), _decision()
        )
    assert adapter.execute_calls == 0


def _invocation() -> AffordanceInvocation:
    return AffordanceInvocation(
        "invocation:1",
        "proposal:1",
        "inspect",
        (AffordanceArgument("path", "README.md"),),
        "invoke-once",
        NOW,
    )


def _definition() -> AffordanceDefinition:
    return AffordanceDefinition(
        "inspect",
        "fixture",
        SideEffectClass.READ_ONLY,
        10.0,
        (AffordanceArgumentSpec("path"),),
    )


def _decision(
    outcome: AuthorizationOutcome = AuthorizationOutcome.ALLOW,
) -> AuthorizationDecision:
    return AuthorizationDecision(
        "proposal:1",
        "constraints:1",
        outcome,
        (AuthorizationFinding(outcome, outcome.value, "fixture"),),
        NOW,
        outcome is AuthorizationOutcome.ALLOW,
    )
