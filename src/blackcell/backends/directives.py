"""Canonical directive persistence capabilities."""

from typing import Protocol

from blackcell.contracts.plan import PlanSpec


class PlanReader(Protocol):
    def load(self, plan_id: str) -> PlanSpec: ...
