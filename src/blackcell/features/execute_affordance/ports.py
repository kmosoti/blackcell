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
    def proposal_id(self) -> str: ...

    @property
    def outcome(self) -> object: ...


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

    def save(self, result: ExecutionResult) -> None: ...


type AdapterRegistry = Mapping[str, AffordanceAdapter]
