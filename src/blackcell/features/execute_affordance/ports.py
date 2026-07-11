from collections.abc import Mapping
from typing import Protocol

from blackcell.features.execute_affordance.models import (
    AdapterOutcome,
    AffordanceDefinition,
    AffordanceInvocation,
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

    def execute(
        self,
        invocation: AffordanceInvocation,
        definition: AffordanceDefinition,
    ) -> AdapterOutcome: ...

    def reconcile(
        self,
        invocation: AffordanceInvocation,
        definition: AffordanceDefinition,
        previous: ExecutionResult,
    ) -> AdapterOutcome: ...


class ExecutionJournal(Protocol):
    def get(self, idempotency_key: str) -> ExecutionResult | None: ...

    def get_by_authorization(self, decision_id: str) -> ExecutionResult | None: ...

    def save(self, result: ExecutionResult) -> None: ...


type AdapterRegistry = Mapping[str, AffordanceAdapter]
