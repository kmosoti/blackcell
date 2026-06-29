"""Echo verification capability consumed by materialization."""

from typing import Any, Protocol

from blackcell.contracts.plan import PlanSpec


class EchoVerifier(Protocol):
    def verify_echoes(
        self,
        plan: PlanSpec,
        *,
        timeout_seconds: float = 0,
        poll_interval: float = 2,
    ) -> tuple[list[dict[str, Any]], list[str]]: ...
