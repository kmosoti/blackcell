"""Provider-neutral public Blackcell SDK client."""

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from blackcell.adapters.github_rest import GitHubRestAdapter
from blackcell.adapters.linear_graphql import LinearGraphQLAdapter, LinearGraphQLTransport
from blackcell.config.loader import load_config
from blackcell.config.model import BlackcellConfig, RuntimeSecrets
from blackcell.contracts.errors import (
    AuthenticationFailure,
    BlackcellError,
    ConflictFailure,
    NotFoundFailure,
    PolicyFailure,
)
from blackcell.contracts.plan import PlanSpec
from blackcell.contracts.result import ResultEnvelope
from blackcell.ledger.sqlite import Chronicle, EventType
from blackcell.policy.identity import verify_viewer_and_team
from blackcell.policy.sync import sanitized_environment
from blackcell.services.materialization_service import (
    LINEAR_PRIORITY,
    MaterializationService,
)
from blackcell.services.plan_service import PlanService
from blackcell.services.plan_store import PlanStore
from blackcell.services.rendering import normalize_presentation_text, render_issue_description
from blackcell.services.sync_service import SyncService
from blackcell.services.verification_service import VerificationService


class BlackcellClient:
    def __init__(
        self,
        config: BlackcellConfig,
        *,
        secrets: RuntimeSecrets | None = None,
        chronicle: Chronicle | None = None,
        store: PlanStore | None = None,
        linear: LinearGraphQLAdapter | None = None,
        github: GitHubRestAdapter | None = None,
    ) -> None:
        self.config = config
        self.secrets = secrets or RuntimeSecrets()
        self.chronicle = chronicle or Chronicle()
        self.store = store or PlanStore()
        self._linear = linear
        self._github = github

    def close(self) -> None:
        if self._linear is not None:
            close_transport = getattr(self._linear.transport, "close", None)
            if callable(close_transport):
                close_transport()
        if self._github is not None:
            self._github.close()

    def __enter__(self) -> BlackcellClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @classmethod
    def from_environment(cls, config_path: str | Path | None = None) -> BlackcellClient:
        return cls(load_config(config_path), secrets=RuntimeSecrets())

    def validate_plan(self, plan: PlanSpec | str | Path) -> ResultEnvelope:
        def operation() -> dict[str, Any]:
            validated = self._plan_service().validate(plan)
            return {
                "plan_id": validated.plan_id,
                "revision": validated.revision,
                "digest": str(validated.digest()),
                "work_items": len(validated.work_items),
            }

        return self._result(operation)

    def propose_plan(self, plan: PlanSpec | str | Path) -> ResultEnvelope:
        return self._result(lambda: self._plan_service(require_linear=True).propose(plan))

    def get_plan_status(self, plan_id: str) -> ResultEnvelope:
        return self._result(
            lambda: self._plan_service(require_linear=True).operation(plan_id),
            plan_id=plan_id,
        )

    def inspect_operation(self, plan_id: str) -> ResultEnvelope:
        return self._result(
            lambda: self._plan_service(require_linear=True).inspect_operation(plan_id),
            plan_id=plan_id,
        )

    def reconcile_operation(self, plan_id: str) -> ResultEnvelope:
        return self._result(
            lambda: self._plan_service(require_linear=True).reconcile_operation(plan_id),
            plan_id=plan_id,
        )

    def materialize_plan(
        self, plan_id: str, *, projection_timeout: float | None = None
    ) -> ResultEnvelope:
        def operation() -> dict[str, Any]:
            result = self._materialization_service().materialize(
                plan_id, projection_timeout=projection_timeout
            )
            if result["pending_relations"]:
                return {
                    **result,
                    "_pending": True,
                    "_pending_code": "pending_relation_verification",
                    "_pending_message": (
                        "One or more Linear blocking relations await readback verification."
                    ),
                }
            if result["pending_echoes"]:
                return {
                    **result,
                    "_pending": True,
                    "_pending_code": "pending_projection",
                    "_pending_message": ("One or more GitHub Issue echoes are not visible yet."),
                }
            return result

        return self._result(operation, plan_id=plan_id)

    def reconcile_plan(self, plan_id: str) -> ResultEnvelope:
        return self.materialize_plan(plan_id)

    def pulse(self, target: str | None = None) -> ResultEnvelope:
        def operation() -> dict[str, Any]:
            checks: dict[str, Any] = {"profile": {"status": "ok"}}
            if target in {None, "linear"}:
                linear = self._linear_adapter()
                viewer, team = linear.identity_snapshot(self.config.linear.team_id)
                verify_viewer_and_team(viewer, team, self.config)
                project_statuses = {status["name"] for status in linear.project_statuses()}
                issue_states = {
                    state["name"] for state in linear.workflow_states(self.config.linear.team_id)
                }
                integrations = linear.integrations()
                integration_services = sorted(
                    {integration["service"] for integration in integrations}
                )
                missing_project_statuses = sorted(
                    set(self.config.linear.project_statuses.model_dump().values())
                    - project_statuses
                )
                missing_issue_states = sorted(
                    set(self.config.linear.issue_states.model_dump().values()) - issue_states
                )
                if missing_project_statuses or missing_issue_states:
                    raise PolicyFailure(
                        "Linear workflow is missing required statuses.",
                        details={
                            "missing_project_statuses": missing_project_statuses,
                            "missing_issue_states": missing_issue_states,
                        },
                    )
                if "github" not in integration_services:
                    raise PolicyFailure(
                        "Linear's GitHub integration is not connected.",
                        recovery="Connect the Linear GitHub App before materialization.",
                    )
                checks["linear"] = {
                    "status": "ok",
                    "viewer": {
                        "id": viewer["id"],
                        "name": viewer["name"],
                        "email": viewer["email"],
                    },
                    "team": team,
                    "project_statuses": sorted(project_statuses),
                    "issue_states": sorted(issue_states),
                    "integration_services": integration_services,
                    "issue_sync": {
                        "provider": self.config.linear.issue_projection_provider,
                        "mode": self.config.linear.issue_sync_mode,
                        "repository_mapping_verified": None,
                        "manual_verification_required": True,
                        "readback": "manual_linear_ui_required",
                    },
                }
            if target in {None, "github", "echo"}:
                github = self._verification_service().github_readiness()
                expected_repository = (
                    f"{self.config.repository.owner}/{self.config.repository.name}"
                )
                if github["repository"] != expected_repository:
                    raise PolicyFailure(
                        "GitHub repository identity does not match configuration.",
                        details={
                            "expected": expected_repository,
                            "actual": github["repository"],
                        },
                    )
                if github["default_branch"] != self.config.repository.default_branch:
                    raise PolicyFailure(
                        "GitHub default branch does not match configuration.",
                        details={"actual": github["default_branch"]},
                    )
                if not github["branch_protected"]:
                    raise PolicyFailure("GitHub main branch is not protected.")
                if github["executor"]["permission"] != "write":
                    raise PolicyFailure(
                        "Executor GitHub permission must be exactly Write.",
                        details={"executor": github["executor"]},
                    )
                checks["github"] = {
                    "status": "ok",
                    **github,
                }
            return {"checks": checks}

        return self._result(operation)

    def profile(self) -> ResultEnvelope:
        return ResultEnvelope.ok(self.config.model_dump(mode="json"))

    def operation(self, plan_id: str) -> ResultEnvelope:
        return self.get_plan_status(plan_id)

    def assignments(self, plan_id: str) -> ResultEnvelope:
        def operation() -> dict[str, Any]:
            plan = self.store.load(plan_id)
            operation_data = self._plan_service(require_linear=True).operation(plan_id)
            issues = self._linear_adapter().project_issues(operation_data["project"]["id"])
            return {"plan_id": plan.plan_id, "assignments": issues}

        return self._result(operation, plan_id=plan_id)

    def verify_assignments(self, plan_id: str) -> ResultEnvelope:
        def operation() -> dict[str, Any]:
            plan = self.store.load(plan_id)
            operation_data = self._plan_service(require_linear=True).operation(plan_id)
            project_id = operation_data["project"]["id"]
            linear = self._linear_adapter()
            issues = linear.team_issues(self.config.linear.team_id)
            labels = {
                label["name"]: label["id"]
                for label in linear.issue_labels(self.config.linear.team_id)
            }
            references: dict[str, dict[str, Any]] = {}
            verified: list[dict[str, Any]] = []
            for item in plan.ordered_work_items():
                marker = f"blackcell://item/{plan.plan_id}/{item.key}?"
                matches = [issue for issue in issues if marker in (issue.get("description") or "")]
                if len(matches) != 1:
                    raise ConflictFailure(
                        "Expected exactly one Linear assignment marker.",
                        details={
                            "plan_id": plan_id,
                            "item_key": item.key,
                            "count": len(matches),
                        },
                    )
                issue = matches[0]
                parent_id = references[item.parent_key]["id"] if item.parent_key else None
                expected = {
                    "title": item.title,
                    "description": render_issue_description(plan, item),
                    "priority": LINEAR_PRIORITY[item.priority],
                    "project_id": project_id,
                    "team_id": self.config.linear.team_id,
                    "parent_id": parent_id,
                    "label_ids": {labels[label] for label in item.labels},
                }
                actual = {
                    "title": issue.get("title"),
                    "description": issue.get("description"),
                    "priority": issue.get("priority"),
                    "project_id": (issue.get("project") or {}).get("id"),
                    "team_id": (issue.get("team") or {}).get("id"),
                    "parent_id": (issue.get("parent") or {}).get("id"),
                    "label_ids": {
                        label["id"] for label in (issue.get("labels") or {}).get("nodes", [])
                    },
                }
                drift: dict[str, Any] = {}
                if normalize_presentation_text(
                    issue.get("description")
                ) != normalize_presentation_text(render_issue_description(plan, item)):
                    drift["description"] = {
                        "expected": expected["description"],
                        "actual": actual["description"],
                    }
                for key in (
                    "title",
                    "priority",
                    "project_id",
                    "team_id",
                    "parent_id",
                    "label_ids",
                ):
                    if actual[key] != expected[key]:
                        drift[key] = {"expected": expected[key], "actual": actual[key]}
                if drift:
                    raise ConflictFailure(
                        "Linear assignment diverges from the directive.",
                        details={
                            "plan_id": plan_id,
                            "item_key": item.key,
                            "drift": drift,
                        },
                    )
                references[item.key] = issue
                verified.append(
                    {
                        "item_key": item.key,
                        "identifier": issue["identifier"],
                        "url": issue["url"],
                    }
                )
            return {"plan_id": plan_id, "verified": verified}

        return self._result(operation, plan_id=plan_id)

    def echoes(self, plan_id: str) -> ResultEnvelope:
        def operation() -> dict[str, Any]:
            plan = self.store.load(plan_id)
            verified, pending = self._verification_service().verify_echoes(plan)
            return {
                "plan_id": plan_id,
                "verified": verified,
                "pending": pending,
                "_pending": bool(pending),
                "recovery": f"blackcell directive reconcile {plan_id}" if pending else None,
            }

        return self._result(operation, plan_id=plan_id)

    def recon_status(self, plan_id: str) -> ResultEnvelope:
        return self._result(
            lambda: self._sync_service().status(plan_id),
            plan_id=plan_id,
        )

    def chronicle_events(self, plan_id: str | None = None) -> ResultEnvelope:
        return ResultEnvelope.ok(
            {"events": [event.model_dump(mode="json") for event in self.chronicle.events(plan_id)]}
        )

    def anomalies(self, anomaly_id: int | None = None) -> ResultEnvelope:
        all_events = self.chronicle.events()
        events = [
            event
            for event in all_events
            if event.event_type == EventType.ANOMALY_DETECTED
            and (anomaly_id is None or event.id == anomaly_id)
        ]
        if anomaly_id is not None and not events:
            return ResultEnvelope.from_error(
                NotFoundFailure(f"Anomaly event {anomaly_id} was not found.")
            )
        resolutions = {
            event.payload["anomaly_id"]: event
            for event in all_events
            if event.event_type == EventType.ANOMALY_RESOLVED
        }
        return ResultEnvelope.ok(
            {
                "anomalies": [
                    {
                        **event.model_dump(mode="json"),
                        "resolved": event.id in resolutions,
                        "resolution": (
                            resolutions[event.id].model_dump(mode="json")
                            if event.id in resolutions
                            else None
                        ),
                    }
                    for event in events
                ]
            }
        )

    def resolve_anomaly(self, anomaly_id: int, note: str) -> ResultEnvelope:
        def operation() -> dict[str, Any]:
            resolution_id = self.chronicle.resolve_anomaly(anomaly_id, note)
            return {"anomaly_id": anomaly_id, "resolution_event_id": resolution_id}

        return self._result(operation)

    def _result(
        self,
        operation: Callable[[], dict[str, Any]],
        *,
        plan_id: str | None = None,
    ) -> ResultEnvelope:
        try:
            data = operation()
            if data.pop("_pending", False):
                result_plan_id = data.get("plan_id", "<plan-id>")
                code = data.pop("_pending_code", "pending_remote_read")
                message = data.pop(
                    "_pending_message",
                    "Remote verification is incomplete but safe to resume.",
                )
                return ResultEnvelope.pending(
                    code,
                    message,
                    data.get("recovery") or f"blackcell directive reconcile {result_plan_id}",
                    data,
                )
            return ResultEnvelope.ok(data)
        except BlackcellError as error:
            if isinstance(error, ConflictFailure):
                event_plan_id = str(error.details.get("plan_id") or plan_id or "BCP-0000")
                self.chronicle.append(
                    EventType.ANOMALY_DETECTED,
                    event_plan_id,
                    {
                        "code": error.code,
                        "message": error.message,
                        "details": error.details,
                    },
                )
            return ResultEnvelope.from_error(error)

    def _plan_service(self, *, require_linear: bool = False) -> PlanService:
        linear = self._linear_adapter() if require_linear else self._linear
        return PlanService(self.config, self.chronicle, self.store, linear)

    def _materialization_service(self) -> MaterializationService:
        return MaterializationService(
            self.config,
            self.chronicle,
            self.store,
            self._linear_adapter(),
            self._verification_service(),
        )

    def _verification_service(self) -> VerificationService:
        return VerificationService(self.config, self._github_adapter())

    def _sync_service(self) -> SyncService:
        return SyncService(
            self.store,
            self.chronicle,
            self._plan_service(require_linear=True),
            self._verification_service(),
        )

    def _linear_adapter(self) -> LinearGraphQLAdapter:
        if self._linear is not None:
            return self._linear
        if self.secrets.linear_api_key is None:
            raise AuthenticationFailure(
                "LINEAR_API_KEY is not set.",
                recovery="Source ~/.config/blackcell/env before running Linear commands.",
            )
        self._linear = LinearGraphQLAdapter(LinearGraphQLTransport(self.secrets.linear_api_key))
        return self._linear

    def _github_adapter(self) -> GitHubRestAdapter:
        if self._github is None:
            token = self.secrets.github_token or self._github_cli_token()
            self._github = GitHubRestAdapter(token)
        return self._github

    @staticmethod
    def _github_cli_token() -> SecretStr | None:
        try:
            completed = subprocess.run(
                ["gh", "auth", "token"],
                check=True,
                capture_output=True,
                text=True,
                env=sanitized_environment(),
                timeout=10,
            )
        except FileNotFoundError, subprocess.SubprocessError:
            return None
        token = completed.stdout.strip()
        return SecretStr(token) if token else None
