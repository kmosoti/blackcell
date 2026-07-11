from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from blackcell.features.execute_affordance.errors import AuthorizationBindingConflict
from blackcell.features.execute_affordance.models import (
    AdapterOutcome,
    AffordanceDefinition,
    AffordanceInvocation,
    ExecutionOperation,
    ExecutionPreparation,
    ExecutionResult,
    ExecutionStatus,
    SideEffectClass,
)
from blackcell.features.execute_affordance.ports import (
    AdapterRegistry,
    AuthorizationDecisionLike,
    ExecutionJournal,
)
from blackcell.kernel.events import utc_now


class ExecutionDenied(RuntimeError):
    pass


class UncertainExecutionError(RuntimeError):
    """The adapter cannot determine whether the side effect happened."""


class AffordanceExecutionHandler:
    def __init__(
        self,
        adapters: AdapterRegistry,
        journal: ExecutionJournal,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapters = adapters
        self._journal = journal
        self._clock = clock or utc_now

    def handle(
        self,
        invocation: AffordanceInvocation,
        definition: AffordanceDefinition,
        authorization: AuthorizationDecisionLike,
        *,
        run_id: str,
    ) -> ExecutionResult:
        if not run_id.strip():
            raise ValueError("run_id must not be empty")
        if authorization.proposal_id != invocation.proposal_id:
            raise ExecutionDenied("authorization belongs to a different proposal")
        if str(authorization.outcome) != "allow":
            raise ExecutionDenied("only an allowed authorization decision may execute")
        if authorization.authorized_action_digest != invocation.action_digest:
            raise ExecutionDenied("invocation payload does not match the authorized action")
        if definition.name != invocation.affordance:
            raise ValueError("invocation affordance does not match its definition")
        definition_is_read_only = definition.side_effect_class is SideEffectClass.READ_ONLY
        if authorization.authorized_read_only != definition_is_read_only:
            raise ExecutionDenied(
                "affordance side-effect class does not match the authorized policy"
            )
        _validate_arguments(invocation, definition)
        try:
            adapter = self._adapters[definition.adapter_id]
        except KeyError as error:
            raise LookupError(
                f"affordance adapter {definition.adapter_id!r} is not registered"
            ) from error
        if adapter.adapter_id != definition.adapter_id:
            raise ValueError("affordance adapter identity does not match the registry key")
        if not adapter.contract_version.strip():
            raise ValueError("affordance adapter contract version must not be empty")
        preparation = ExecutionPreparation(
            run_id=run_id,
            invocation=invocation,
            definition=definition,
            authorization_decision_id=authorization.decision_id,
            authorized_action_digest=invocation.action_digest,
            adapter_contract_version=adapter.contract_version,
        )
        binding = preparation.binding
        acquired_at = self._now()
        try:
            acquired = self._journal.acquire(preparation, acquired_at=acquired_at)
        except AuthorizationBindingConflict as error:
            raise ExecutionDenied(
                "authorization decision is already bound to a different invocation"
            ) from error
        if isinstance(acquired, ExecutionResult):
            return acquired
        claim = acquired
        try:
            outcome = (
                adapter.reconcile(invocation, definition, claim.previous)
                if claim.operation is ExecutionOperation.RECONCILE
                else adapter.execute(invocation, definition)
            )
            result = _result(
                invocation,
                definition,
                outcome,
                authorization_decision_id=authorization.decision_id,
                identity_digest=binding.execution_identity_digest,
                reconciled=claim.operation is ExecutionOperation.RECONCILE,
            )
        except UncertainExecutionError:
            result = ExecutionResult(
                invocation_id=invocation.invocation_id,
                proposal_id=invocation.proposal_id,
                authorization_decision_id=authorization.decision_id,
                affordance=invocation.affordance,
                adapter_id=definition.adapter_id,
                idempotency_key=invocation.idempotency_key,
                authorized_action_digest=invocation.action_digest,
                execution_identity_digest=binding.execution_identity_digest,
                status=ExecutionStatus.UNKNOWN,
                started_at=invocation.requested_at,
                completed_at=self._now(),
                output_digest=None,
                observed_effects=(),
                error_code="outcome_unknown",
                reconciled=claim.operation is ExecutionOperation.RECONCILE,
            )
        return self._journal.complete(claim, result, recorded_at=self._now())

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("execution journal clock must return a timezone-aware datetime")
        return value


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
    authorization_decision_id: str,
    identity_digest: str,
    reconciled: bool,
) -> ExecutionResult:
    status = ExecutionStatus.SUCCEEDED if outcome.success else ExecutionStatus.FAILED
    return ExecutionResult(
        invocation_id=invocation.invocation_id,
        proposal_id=invocation.proposal_id,
        authorization_decision_id=authorization_decision_id,
        affordance=invocation.affordance,
        adapter_id=definition.adapter_id,
        idempotency_key=invocation.idempotency_key,
        authorized_action_digest=invocation.action_digest,
        execution_identity_digest=identity_digest,
        status=status,
        started_at=invocation.requested_at,
        completed_at=outcome.completed_at,
        output_digest=outcome.output_digest,
        observed_effects=outcome.observed_effects,
        error_code=outcome.error_code,
        reconciled=reconciled,
    )
