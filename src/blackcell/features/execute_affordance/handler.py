from __future__ import annotations

from blackcell.features.execute_affordance.models import (
    AdapterOutcome,
    AffordanceDefinition,
    AffordanceInvocation,
    ExecutionResult,
    ExecutionStatus,
)
from blackcell.features.execute_affordance.ports import (
    AdapterRegistry,
    AuthorizationDecisionLike,
    ExecutionJournal,
)


class ExecutionDenied(RuntimeError):
    pass


class UncertainExecutionError(RuntimeError):
    """The adapter cannot determine whether the side effect happened."""


class AffordanceExecutionHandler:
    def __init__(self, adapters: AdapterRegistry, journal: ExecutionJournal) -> None:
        self._adapters = adapters
        self._journal = journal

    def handle(
        self,
        invocation: AffordanceInvocation,
        definition: AffordanceDefinition,
        authorization: AuthorizationDecisionLike,
    ) -> ExecutionResult:
        if authorization.proposal_id != invocation.proposal_id:
            raise ExecutionDenied("authorization belongs to a different proposal")
        if str(authorization.outcome) != "allow":
            raise ExecutionDenied("only an allowed authorization decision may execute")
        if definition.name != invocation.affordance:
            raise ValueError("invocation affordance does not match its definition")
        _validate_arguments(invocation, definition)
        try:
            adapter = self._adapters[definition.adapter_id]
        except KeyError as error:
            raise LookupError(
                f"affordance adapter {definition.adapter_id!r} is not registered"
            ) from error
        if adapter.adapter_id != definition.adapter_id:
            raise ValueError("affordance adapter identity does not match the registry key")
        previous = self._journal.get(invocation.idempotency_key)
        if previous is not None and previous.status is not ExecutionStatus.UNKNOWN:
            return previous
        reconciled = previous is not None
        try:
            outcome = (
                adapter.reconcile(invocation, definition, previous)
                if previous is not None
                else adapter.execute(invocation, definition)
            )
            result = _result(invocation, definition, outcome, reconciled=reconciled)
        except UncertainExecutionError:
            result = ExecutionResult(
                invocation.invocation_id,
                invocation.proposal_id,
                invocation.affordance,
                definition.adapter_id,
                invocation.idempotency_key,
                ExecutionStatus.UNKNOWN,
                invocation.requested_at,
                invocation.requested_at,
                None,
                (),
                "outcome_unknown",
                reconciled,
            )
        self._journal.save(result)
        return result


def _validate_arguments(
    invocation: AffordanceInvocation,
    definition: AffordanceDefinition,
) -> None:
    supplied = {item.name for item in invocation.arguments}
    declared = {item.name for item in definition.arguments}
    unexpected = tuple(sorted(supplied - declared))
    missing = tuple(
        sorted(
            item.name
            for item in definition.arguments
            if item.required and item.name not in supplied
        )
    )
    if unexpected or missing:
        raise ValueError(
            f"invalid affordance arguments; unexpected={unexpected}, missing={missing}"
        )


def _result(
    invocation: AffordanceInvocation,
    definition: AffordanceDefinition,
    outcome: AdapterOutcome,
    *,
    reconciled: bool,
) -> ExecutionResult:
    status = ExecutionStatus.SUCCEEDED if outcome.success else ExecutionStatus.FAILED
    return ExecutionResult(
        invocation.invocation_id,
        invocation.proposal_id,
        invocation.affordance,
        definition.adapter_id,
        invocation.idempotency_key,
        status,
        invocation.requested_at,
        outcome.completed_at,
        outcome.output_digest,
        outcome.observed_effects,
        outcome.error_code,
        reconciled,
    )
