from dataclasses import replace
from datetime import UTC, datetime

import pytest

from blackcell.features.authorize_action import (
    ActionArgument,
    ActionProposal,
    AffordancePolicy,
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

    def get_by_authorization(self, decision_id: str) -> ExecutionResult | None:
        return next(
            (
                result
                for result in self.results.values()
                if result.authorization_decision_id == decision_id
            ),
            None,
        )

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
    assert first.authorization_decision_id == _decision().decision_id
    assert first.authorized_action_digest == _invocation().action_digest
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


def test_authorization_decision_cannot_be_replayed_as_a_fresh_invocation() -> None:
    adapter = Adapter()
    journal = Journal()
    handler = AffordanceExecutionHandler({"fixture": adapter}, journal)
    authorization = _decision()
    first = handler.handle(_invocation(), _definition(), authorization)
    replay = replace(
        _invocation(),
        invocation_id="invocation:2",
        idempotency_key="invoke-twice",
    )

    with pytest.raises(ExecutionDenied, match="already bound"):
        handler.handle(replay, _definition(), authorization)

    assert adapter.execute_calls == 1
    assert journal.results == {"invoke-once": first}


def test_completed_result_rejects_changed_payload_under_the_same_identity() -> None:
    adapter = Adapter()
    journal = Journal()
    handler = AffordanceExecutionHandler({"fixture": adapter}, journal)
    first = handler.handle(_invocation(), _definition(), _decision())
    changed = replace(_invocation(), arguments=(AffordanceArgument("path", "pyproject.toml"),))

    with pytest.raises(IdempotencyKeyConflict, match="execution_identity_digest"):
        handler.handle(changed, _definition(), _decision(invocation=changed))

    assert adapter.execute_calls == 1
    assert journal.results["invoke-once"] == first


@pytest.mark.parametrize(
    "change",
    ("arguments", "affordance"),
)
def test_authorization_for_same_proposal_id_rejects_changed_action_payload(
    change: str,
) -> None:
    adapter = Adapter()
    handler = AffordanceExecutionHandler({"fixture": adapter}, Journal())
    changed = (
        replace(_invocation(), arguments=(AffordanceArgument("path", "pyproject.toml"),))
        if change == "arguments"
        else replace(_invocation(), affordance="update")
    )

    with pytest.raises(ExecutionDenied, match="authorized action"):
        handler.handle(changed, replace(_definition(), name=changed.affordance), _decision())

    assert adapter.execute_calls == 0


def test_action_digest_is_independent_of_named_argument_order() -> None:
    left = replace(
        _invocation(),
        arguments=(
            AffordanceArgument("path", "README.md"),
            AffordanceArgument("encoding", "utf-8"),
        ),
    )
    right = replace(left, arguments=tuple(reversed(left.arguments)))

    assert left.action_digest == right.action_digest


def test_read_only_authorization_rejects_same_name_mutating_definition() -> None:
    adapter = Adapter()
    handler = AffordanceExecutionHandler({"fixture": adapter}, Journal())
    mutating = replace(_definition(), side_effect_class=SideEffectClass.REVERSIBLE)

    with pytest.raises(ExecutionDenied, match="side-effect class"):
        handler.handle(_invocation(), mutating, _decision())

    assert adapter.execute_calls == 0


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


def test_unknown_result_rejects_a_different_authorization_before_reconciliation() -> None:
    adapter = Adapter(uncertain=True)
    journal = Journal()
    handler = AffordanceExecutionHandler({"fixture": adapter}, journal)
    original = _decision()
    unknown = handler.handle(_invocation(), _definition(), original)
    replacement = _decision(constraint_evaluation_id="constraints:replacement")

    with pytest.raises(IdempotencyKeyConflict, match="authorization_decision_id"):
        handler.handle(_invocation(), _definition(), replacement)

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
            invocation, _definition(), _decision(invocation=invocation)
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
    *,
    invocation: AffordanceInvocation | None = None,
    constraint_evaluation_id: str = "constraints:1",
    read_only: bool = True,
) -> AuthorizationDecision:
    invocation = invocation or _invocation()
    proposal = ActionProposal(
        invocation.proposal_id,
        "frame:1",
        invocation.affordance,
        tuple(ActionArgument(item.name, item.value) for item in invocation.arguments),
        "fixture proposal",
    )
    policy = AffordancePolicy(
        invocation.affordance,
        read_only,
        mutates_state=not read_only,
        allowed_arguments=tuple(item.name for item in invocation.arguments),
    )
    return AuthorizationDecision(
        proposal_id=proposal.proposal_id,
        proposal_digest=proposal.proposal_digest,
        context_frame_id=proposal.context_frame_id,
        constraint_evaluation_id=constraint_evaluation_id,
        authorized_action_digest=proposal.action_digest,
        affordance_policy_digest=policy.policy_digest,
        authorized_read_only=policy.read_only,
        authorized_external=policy.external,
        authorized_mutates_state=policy.mutates_state,
        outcome=outcome,
        findings=(AuthorizationFinding(outcome, outcome.value, "fixture"),),
        evaluated_at=NOW,
        approval_granted=outcome is AuthorizationOutcome.ALLOW,
    )
