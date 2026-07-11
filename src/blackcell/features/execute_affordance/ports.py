from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from blackcell.features.execute_affordance.models import (
    AdapterOutcome,
    AffordanceDefinition,
    AffordanceInvocation,
    ExecutionClaim,
    ExecutionJournalEntry,
    ExecutionJournalStatus,
    ExecutionPreparation,
    ExecutionRecovery,
    ExecutionRecoveryAuthorization,
    ExecutionResult,
)


class AuthorizationDecisionLike(Protocol):
    @property
    def decision_id(self) -> str: ...

    @property
    def proposal_id(self) -> str: ...

    @property
    def outcome(self) -> object: ...

    @property
    def authorized_action_digest(self) -> str: ...

    @property
    def authorized_read_only(self) -> bool: ...


class AffordanceAdapter(Protocol):
    @property
    def adapter_id(self) -> str: ...

    @property
    def contract_version(self) -> str: ...

    def execute(
        self,
        invocation: AffordanceInvocation,
        definition: AffordanceDefinition,
    ) -> AdapterOutcome: ...

    def reconcile(
        self,
        invocation: AffordanceInvocation,
        definition: AffordanceDefinition,
        previous: ExecutionResult | None,
    ) -> AdapterOutcome: ...


class ExecutionJournal(Protocol):
    def acquire(
        self,
        preparation: ExecutionPreparation,
        *,
        acquired_at: datetime,
    ) -> ExecutionClaim | ExecutionResult: ...

    def recover(
        self,
        authorization: ExecutionRecoveryAuthorization,
        *,
        recovered_at: datetime,
    ) -> ExecutionRecovery: ...

    def complete(
        self,
        claim: ExecutionClaim,
        result: ExecutionResult,
        *,
        recorded_at: datetime,
    ) -> ExecutionResult: ...

    def get(self, idempotency_key: str) -> ExecutionResult | None: ...

    def get_by_authorization(self, decision_id: str) -> ExecutionResult | None: ...

    def get_by_invocation(self, invocation_id: str) -> ExecutionResult | None: ...

    def get_preparation(
        self,
        execution_identity_digest: str,
    ) -> ExecutionPreparation | None: ...

    def list_entries(
        self,
        *,
        after_position: int = 0,
        limit: int | None = None,
        status: ExecutionJournalStatus | None = None,
    ) -> tuple[ExecutionJournalEntry, ...]: ...


type AdapterRegistry = Mapping[str, AffordanceAdapter]
