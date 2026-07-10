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
from blackcell.kernel._json import json_digest


class ExecutionDenied(RuntimeError):
    pass


class UncertainExecutionError(RuntimeError):
    """The adapter cannot determine whether the side effect happened."""


class IdempotencyKeyConflict(RuntimeError):
    """An idempotency key is already bound to a different execution identity."""


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
        identity_digest = _execution_identity_digest(invocation, definition)
        if previous is not None:
            _validate_previous_identity(
                previous,
                invocation,
                definition,
                identity_digest=identity_digest,
            )
        if previous is not None and previous.status is not ExecutionStatus.UNKNOWN:
            return previous
        reconciled = previous is not None
        try:
            outcome = (
                adapter.reconcile(invocation, definition, previous)
                if previous is not None
                else adapter.execute(invocation, definition)
            )
            result = _result(
                invocation,
                definition,
                outcome,
                identity_digest=identity_digest,
                reconciled=reconciled,
            )
        except UncertainExecutionError:
            result = ExecutionResult(
                invocation_id=invocation.invocation_id,
                proposal_id=invocation.proposal_id,
                affordance=invocation.affordance,
                adapter_id=definition.adapter_id,
                idempotency_key=invocation.idempotency_key,
                execution_identity_digest=identity_digest,
                status=ExecutionStatus.UNKNOWN,
                started_at=invocation.requested_at,
                completed_at=invocation.requested_at,
                output_digest=None,
                observed_effects=(),
                error_code="outcome_unknown",
                reconciled=reconciled,
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
    identity_digest: str,
    reconciled: bool,
) -> ExecutionResult:
    status = ExecutionStatus.SUCCEEDED if outcome.success else ExecutionStatus.FAILED
    return ExecutionResult(
        invocation_id=invocation.invocation_id,
        proposal_id=invocation.proposal_id,
        affordance=invocation.affordance,
        adapter_id=definition.adapter_id,
        idempotency_key=invocation.idempotency_key,
        execution_identity_digest=identity_digest,
        status=status,
        started_at=invocation.requested_at,
        completed_at=outcome.completed_at,
        output_digest=outcome.output_digest,
        observed_effects=outcome.observed_effects,
        error_code=outcome.error_code,
        reconciled=reconciled,
    )


def _validate_previous_identity(
    previous: ExecutionResult,
    invocation: AffordanceInvocation,
    definition: AffordanceDefinition,
    *,
    identity_digest: str,
) -> None:
    expected = {
        "invocation_id": invocation.invocation_id,
        "proposal_id": invocation.proposal_id,
        "affordance": invocation.affordance,
        "adapter_id": definition.adapter_id,
        "idempotency_key": invocation.idempotency_key,
        "execution_identity_digest": identity_digest,
    }
    mismatches = tuple(name for name, value in expected.items() if getattr(previous, name) != value)
    if mismatches:
        fields = ", ".join(mismatches)
        raise IdempotencyKeyConflict(
            f"idempotency key {invocation.idempotency_key!r} is already bound to "
            f"a different execution identity; mismatched fields: {fields}"
        )


def _execution_identity_digest(
    invocation: AffordanceInvocation,
    definition: AffordanceDefinition,
) -> str:
    return json_digest(
        {
            "invocation": {
                "invocation_id": invocation.invocation_id,
                "proposal_id": invocation.proposal_id,
                "affordance": invocation.affordance,
                "arguments": [
                    {"name": item.name, "value": item.value}
                    for item in sorted(invocation.arguments, key=lambda item: item.name)
                ],
                "idempotency_key": invocation.idempotency_key,
                "requested_at": invocation.requested_at.isoformat(),
            },
            "definition": {
                "name": definition.name,
                "adapter_id": definition.adapter_id,
                "side_effect_class": definition.side_effect_class.value,
                "timeout_seconds": definition.timeout_seconds,
                "arguments": [
                    {"name": item.name, "required": item.required}
                    for item in sorted(definition.arguments, key=lambda item: item.name)
                ],
            },
        }
    )
