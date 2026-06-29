"""Cross-system read-only reconciliation report."""

from typing import Any

from blackcell.ledger.sqlite import Chronicle
from blackcell.services.plan_service import PlanService
from blackcell.services.plan_store import PlanStore
from blackcell.services.verification_service import VerificationService


class SyncService:
    def __init__(
        self,
        store: PlanStore,
        chronicle: Chronicle,
        plans: PlanService,
        verification: VerificationService,
    ) -> None:
        self.store = store
        self.chronicle = chronicle
        self.plans = plans
        self.verification = verification

    def status(self, plan_id: str) -> dict[str, Any]:
        plan = self.store.load(plan_id)
        operation = self.plans.operation(plan_id)
        verified, pending = self.verification.verify_echoes(plan)
        return {
            "plan_id": plan_id,
            "digest": str(plan.digest()),
            "operation": operation,
            "chronicle_events": len(self.chronicle.events(plan_id)),
            "verified_echoes": verified,
            "pending_echoes": pending,
        }
