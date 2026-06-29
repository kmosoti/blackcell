"""Directive validation, proposal, and operation inspection."""

from pathlib import Path
from typing import Any

from blackcell.backends.planning import PlanWorkflowBackend
from blackcell.config.model import BlackcellConfig
from blackcell.contracts.errors import ConflictFailure, NotFoundFailure, PolicyFailure
from blackcell.contracts.plan import PlanSpec
from blackcell.ledger.sqlite import Chronicle, EventType
from blackcell.policy.identity import verify_plan_target, verify_viewer_and_team
from blackcell.services.plan_store import PlanStore
from blackcell.services.project_integration import ProjectIntegration


class PlanService:
    def __init__(
        self,
        config: BlackcellConfig,
        chronicle: Chronicle,
        store: PlanStore,
        linear: PlanWorkflowBackend | None = None,
    ) -> None:
        self.config = config
        self.chronicle = chronicle
        self.store = store
        self.linear = linear

    def validate(self, plan_or_path: PlanSpec | str | Path) -> PlanSpec:
        plan = (
            plan_or_path if isinstance(plan_or_path, PlanSpec) else PlanSpec.from_file(plan_or_path)
        )
        verify_plan_target(plan, self.config)
        return plan

    def propose(self, plan_or_path: PlanSpec | str | Path) -> dict[str, Any]:
        plan = self.validate(plan_or_path)
        linear = self._linear()
        viewer, team = linear.identity_snapshot(self.config.linear.team_id)
        verify_viewer_and_team(viewer, team, self.config)
        statuses = {status["name"]: status for status in linear.project_statuses()}
        proposal_name = self.config.linear.project_statuses.proposal
        if proposal_name not in statuses:
            raise PolicyFailure(
                "Required Linear Project status is missing.",
                details={"missing_status": proposal_name},
            )

        identity_marker = f"blackcell://plan/{plan.plan_id}?"
        matches = linear.find_projects_by_marker(self.config.linear.team_id, identity_marker)
        if len(matches) > 1:
            raise ConflictFailure(
                "Multiple Linear Projects contain the directive marker.",
                details={"plan_id": plan.plan_id, "count": len(matches)},
            )
        if matches:
            project = matches[0]
            reconciliation = self._projects().reconcile(project, plan)
            project = reconciliation.project
            reconciled_fields = reconciliation.reconciled_fields
            event_type = EventType.OPERATION_LOCATED
            created = False
        else:
            project = self._projects().create(plan, statuses[proposal_name]["id"])
            event_type = EventType.OPERATION_PROPOSED
            created = True
            reconciled_fields = []

        path = self.store.save(plan)
        self.chronicle.append(
            EventType.DIRECTIVE_VALIDATED,
            plan.plan_id,
            {"revision": plan.revision, "digest": str(plan.digest()), "path": str(path)},
        )
        self.chronicle.append(
            event_type,
            plan.plan_id,
            {"project_id": project["id"], "url": project["url"], "created": created},
        )
        if reconciled_fields:
            self.chronicle.append(
                EventType.OPERATION_PRESENTATION_RECONCILED,
                plan.plan_id,
                {
                    "project_id": project["id"],
                    "fields": sorted(reconciled_fields),
                },
            )
        return {
            "plan_id": plan.plan_id,
            "revision": plan.revision,
            "digest": str(plan.digest()),
            "project": project,
            "created": created,
            "presentation_reconciled": bool(reconciled_fields),
        }

    def reconcile_operation(self, plan_id: str) -> dict[str, Any]:
        return self.propose(self.store.load(plan_id))

    def inspect_operation(self, plan_id: str) -> dict[str, Any]:
        plan, project = self._load_operation(plan_id)
        assessment = self._projects().assess(project, plan)
        return {
            "plan_id": plan_id,
            "project": project,
            "matches": assessment.matches,
            **assessment.model_dump(mode="json"),
        }

    def operation(self, plan_id: str) -> dict[str, Any]:
        plan, project = self._load_operation(plan_id)
        self._projects().verify(project, plan)
        return {
            "plan_id": plan_id,
            "digest_matches": True,
            "project": project,
        }

    def _load_operation(self, plan_id: str) -> tuple[PlanSpec, dict[str, Any]]:
        plan = self.store.load(plan_id)
        linear = self._linear()
        viewer, team = linear.identity_snapshot(self.config.linear.team_id)
        verify_viewer_and_team(viewer, team, self.config)
        matches = linear.find_projects_by_marker(
            self.config.linear.team_id, f"blackcell://plan/{plan_id}?"
        )
        if not matches:
            raise NotFoundFailure(f"No Linear Project exists for {plan_id}.")
        if len(matches) > 1:
            raise ConflictFailure(
                "Multiple Linear Projects contain the directive marker.",
                details={"plan_id": plan_id, "count": len(matches)},
            )
        return plan, matches[0]

    def _linear(self) -> PlanWorkflowBackend:
        if self.linear is None:
            raise PolicyFailure("This operation requires LINEAR_API_KEY.")
        return self.linear

    def _projects(self) -> ProjectIntegration:
        return ProjectIntegration(self.config, self._linear())
