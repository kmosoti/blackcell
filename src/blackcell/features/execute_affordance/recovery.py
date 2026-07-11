from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from blackcell.features.execute_affordance.handler import (
    UncertainExecutionError,
    _result,
)
from blackcell.features.execute_affordance.models import (
    ExecutionRecoveryAuthorization,
    ExecutionResult,
    ExecutionStatus,
)
from blackcell.features.execute_affordance.ports import AdapterRegistry, ExecutionJournal
from blackcell.kernel.events import utc_now


class ManualAffordanceRecovery:
    """Explicitly reconcile one durably prepared execution after worker loss."""

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

    def recover(self, authorization: ExecutionRecoveryAuthorization) -> ExecutionResult:
        recovered_at = self._now()
        recovery = self._journal.recover(authorization, recovered_at=recovered_at)
        preparation = recovery.preparation
        claim = recovery.claim
        try:
            adapter = self._adapters[preparation.definition.adapter_id]
        except KeyError as error:
            raise LookupError(
                f"affordance adapter {preparation.definition.adapter_id!r} is not registered"
            ) from error
        if adapter.adapter_id != preparation.definition.adapter_id:
            raise ValueError("recovery adapter identity does not match the prepared definition")
        if adapter.contract_version != preparation.adapter_contract_version:
            raise ValueError("recovery adapter contract version does not match preparation")
        try:
            outcome = adapter.reconcile(
                preparation.invocation,
                preparation.definition,
                claim.previous,
            )
            result = _result(
                preparation.invocation,
                preparation.definition,
                outcome,
                authorization_decision_id=preparation.authorization_decision_id,
                identity_digest=preparation.binding.execution_identity_digest,
                reconciled=True,
            )
        except UncertainExecutionError:
            result = ExecutionResult(
                invocation_id=preparation.invocation.invocation_id,
                proposal_id=preparation.invocation.proposal_id,
                authorization_decision_id=preparation.authorization_decision_id,
                affordance=preparation.invocation.affordance,
                adapter_id=preparation.definition.adapter_id,
                idempotency_key=preparation.invocation.idempotency_key,
                authorized_action_digest=preparation.authorized_action_digest,
                execution_identity_digest=preparation.binding.execution_identity_digest,
                status=ExecutionStatus.UNKNOWN,
                started_at=preparation.invocation.requested_at,
                completed_at=self._now(),
                output_digest=None,
                observed_effects=(),
                error_code="outcome_unknown",
                reconciled=True,
            )
        return self._journal.complete(claim, result, recorded_at=self._now())

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("execution recovery clock must return a timezone-aware datetime")
        return value
