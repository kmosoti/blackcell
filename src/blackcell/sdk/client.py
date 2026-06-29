"""Provider-neutral public BlackCell SDK client."""

import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from blackcell.adapters.github_rest import GitHubRestAdapter
from blackcell.adapters.linear_graphql import LinearGraphQLAdapter, LinearGraphQLTransport
from blackcell.adapters.local_publication import LocalPublicationAdapter
from blackcell.backends.capabilities import (
    PLANNING_PROTOCOL_CAPABILITIES,
    capability_is_schema_backed,
    missing_capabilities,
)
from blackcell.backends.publication import PublicationBackend
from blackcell.config.loader import load_config
from blackcell.config.model import BlackcellConfig, RuntimeSecrets
from blackcell.contracts.errors import (
    AuthenticationFailure,
    BlackcellError,
    ConflictFailure,
    NotFoundFailure,
    PolicyFailure,
)
from blackcell.contracts.facade import Credential, OperationSpec
from blackcell.contracts.plan import PlanSpec
from blackcell.contracts.publication import PublicationStage
from blackcell.contracts.result import ResultEnvelope
from blackcell.ledger.sqlite import Chronicle, EventType
from blackcell.policy.identity import verify_viewer_and_team
from blackcell.policy.lifecycle import ProjectCapability, ProjectStateMachine
from blackcell.policy.sync import sanitized_environment
from blackcell.runtime.execution import (
    AnomalyAspect,
    CredentialAspect,
    OperationExecutor,
    PendingOutcome,
    StructuredEventAspect,
    current_operation,
)
from blackcell.runtime.observability import EventSink, event_sink_from_environment
from blackcell.schema.linear import default_linear_schema_path, load_linear_schema
from blackcell.sdk.operations import OperationId, spec
from blackcell.services.materialization_service import (
    LINEAR_PRIORITY,
    MaterializationService,
)
from blackcell.services.plan_service import PlanService
from blackcell.services.plan_store import PlanStore
from blackcell.services.publication_service import PublicationService
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
        publication: PublicationBackend | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self.config = config
        self.secrets = secrets or RuntimeSecrets()
        self.chronicle = chronicle or Chronicle()
        self.store = store or PlanStore()
        self._linear = linear
        self._github = github
        self._publication = publication
        self._executor = OperationExecutor(
            (
                StructuredEventAspect(event_sink or event_sink_from_environment()),
                CredentialAspect(self._prepare_credential),
                AnomalyAspect(self.chronicle),
            )
        )

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

        return self._result(OperationId.DIRECTIVE_VALIDATE, operation)

    def propose_plan(self, plan: PlanSpec | str | Path) -> ResultEnvelope:
        return self._result(
            OperationId.DIRECTIVE_PROPOSE,
            lambda: self._plan_service(require_linear=True).propose(plan),
        )

    def get_plan_status(self, plan_id: str) -> ResultEnvelope:
        return self._result(
            OperationId.DIRECTIVE_STATUS,
            lambda: self._plan_service(require_linear=True).operation(plan_id),
            plan_id=plan_id,
        )

    def inspect_operation(self, plan_id: str) -> ResultEnvelope:
        return self._result(
            OperationId.OPERATION_INSPECT,
            lambda: self._plan_service(require_linear=True).inspect_operation(plan_id),
            plan_id=plan_id,
        )

    def reconcile_operation(self, plan_id: str) -> ResultEnvelope:
        return self._result(
            OperationId.OPERATION_RECONCILE,
            lambda: self._plan_service(require_linear=True).reconcile_operation(plan_id),
            plan_id=plan_id,
        )

    def materialize_plan(
        self, plan_id: str, *, projection_timeout: float | None = None
    ) -> ResultEnvelope:
        return self._materialize(
            plan_id,
            operation_id=OperationId.DIRECTIVE_MATERIALIZE,
            projection_timeout=projection_timeout,
        )

    def reconcile_plan(self, plan_id: str) -> ResultEnvelope:
        return self._materialize(
            plan_id,
            operation_id=OperationId.DIRECTIVE_RECONCILE,
        )

    def _materialize(
        self,
        plan_id: str,
        *,
        operation_id: OperationId,
        projection_timeout: float | None = None,
    ) -> ResultEnvelope:
        def operation() -> dict[str, Any] | PendingOutcome:
            result = self._materialization_service().materialize(
                plan_id, projection_timeout=projection_timeout
            )
            if result["pending_relations"]:
                return PendingOutcome(
                    code="pending_relation_verification",
                    message="One or more Linear blocking relations await readback verification.",
                    recovery=result["recovery"],
                    data=result,
                )
            if result["pending_echoes"]:
                return PendingOutcome(
                    code="pending_projection",
                    message="One or more GitHub Issue echoes are not visible yet.",
                    recovery=result["recovery"],
                    data=result,
                )
            return result

        return self._result(operation_id, operation, plan_id=plan_id)

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

        credentials = {
            "linear": frozenset({Credential.LINEAR}),
            "github": frozenset({Credential.GITHUB}),
            "echo": frozenset({Credential.GITHUB}),
        }.get(target, frozenset({Credential.LINEAR, Credential.GITHUB}))
        pulse_spec = replace(spec(OperationId.PULSE), credentials=credentials)
        return self._execute(pulse_spec, operation)

    def profile(self) -> ResultEnvelope:
        return self._result(
            OperationId.PROFILE_SHOW,
            lambda: self.config.model_dump(mode="json"),
        )

    def validate_profile(self) -> ResultEnvelope:
        def operation() -> dict[str, Any]:
            return {
                "valid": True,
                "schema_version": self.config.schema_version,
                "repository": f"{self.config.repository.owner}/{self.config.repository.name}",
                "linear_team": {
                    "id": self.config.linear.team_id,
                    "key": self.config.linear.team_key,
                    "name": self.config.linear.team_name,
                },
            }

        return self._result(OperationId.PROFILE_VALIDATE, operation)

    def schema_audit(self) -> ResultEnvelope:
        return self._result(OperationId.SCHEMA_AUDIT, self._schema_audit_data)

    def operation(self, plan_id: str) -> ResultEnvelope:
        return self._result(
            OperationId.OPERATION_VERIFY,
            lambda: self._plan_service(require_linear=True).operation(plan_id),
            plan_id=plan_id,
        )

    def workflow_run(self, plan_id: str) -> ResultEnvelope:
        return self._result(
            OperationId.WORKFLOW_RUN,
            lambda: self._workflow_run(plan_id),
            plan_id=plan_id,
        )

    def workflow_resume(self, plan_id: str) -> ResultEnvelope:
        return self._result(
            OperationId.WORKFLOW_RESUME,
            lambda: self._workflow_run(plan_id),
            plan_id=plan_id,
        )

    def workflow_status(self, plan_id: str) -> ResultEnvelope:
        return self._result(
            OperationId.WORKFLOW_STATUS,
            lambda: self._workflow_status_data(plan_id),
            plan_id=plan_id,
        )

    def assignments(self, plan_id: str) -> ResultEnvelope:
        def operation() -> dict[str, Any]:
            plan = self.store.load(plan_id)
            operation_data = self._plan_service(require_linear=True).operation(plan_id)
            issues = self._linear_adapter().project_issues(operation_data["project"]["id"])
            return {"plan_id": plan.plan_id, "assignments": issues}

        return self._result(OperationId.ASSIGNMENT_LIST, operation, plan_id=plan_id)

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
            self._verify_assignment_relations(plan, references)
            return {"plan_id": plan_id, "verified": verified}

        return self._result(OperationId.ASSIGNMENT_VERIFY, operation, plan_id=plan_id)

    def _verify_assignment_relations(
        self, plan: PlanSpec, references: dict[str, dict[str, Any]]
    ) -> None:
        plan_issue_ids = {issue["id"] for issue in references.values()}
        issue_keys_by_id = {issue["id"]: key for key, issue in references.items()}
        declared_relations: set[tuple[str, str]] = set()
        for item in plan.ordered_work_items():
            blocked_id = references[item.key]["id"]
            for dependency_key in item.blocked_by:
                declared_relations.add((references[dependency_key]["id"], blocked_id))

        observed_relations = set[tuple[str, str]]()
        for issue in references.values():
            observed_relations.update(self._extract_blocking_relations(issue))

        in_scope_observed = {
            relation
            for relation in observed_relations
            if relation[0] in plan_issue_ids and relation[1] in plan_issue_ids
        }
        missing = sorted(
            [
                {
                    "blocker_key": issue_keys_by_id[relation[0]],
                    "blocked_key": issue_keys_by_id[relation[1]],
                }
                for relation in sorted(declared_relations - in_scope_observed)
            ],
            key=lambda item: (item["blocker_key"], item["blocked_key"]),
        )
        extra = sorted(
            [
                {
                    "blocker_key": issue_keys_by_id[relation[0]],
                    "blocked_key": issue_keys_by_id[relation[1]],
                }
                for relation in sorted(in_scope_observed - declared_relations)
            ],
            key=lambda item: (item["blocker_key"], item["blocked_key"]),
        )
        wrong_direction = sorted(
            [
                {
                    "declared_blocker_key": issue_keys_by_id[declared_blocker_id],
                    "declared_blocked_key": issue_keys_by_id[declared_blocked_id],
                    "observed_blocker_key": issue_keys_by_id[declared_blocked_id],
                    "observed_blocked_key": issue_keys_by_id[declared_blocker_id],
                }
                for declared_blocker_id, declared_blocked_id in declared_relations
                if (declared_blocked_id, declared_blocker_id) in in_scope_observed
            ],
            key=lambda item: (
                item["declared_blocker_key"],
                item["declared_blocked_key"],
            ),
        )

        if missing or extra or wrong_direction:
            raise ConflictFailure(
                "Linear blocking relations diverge from the directive.",
                details={
                    "plan_id": plan.plan_id,
                    "relation_conflicts": {
                        "missing": missing,
                        "extra": extra,
                        "wrong_direction": wrong_direction,
                    },
                },
            )

    @staticmethod
    def _extract_blocking_relations(issue: dict[str, Any]) -> set[tuple[str, str]]:
        relations = []
        for relation in (issue.get("relations") or {}).get("nodes", []):
            if relation.get("type") != "blocks":
                continue
            issue_id = (relation.get("issue") or {}).get("id")
            related_issue_id = (relation.get("relatedIssue") or {}).get("id")
            if issue_id is None or related_issue_id is None:
                continue
            relations.append((str(issue_id), str(related_issue_id)))
        return set(relations)

    def echoes(self, plan_id: str) -> ResultEnvelope:
        def operation() -> dict[str, Any] | PendingOutcome:
            plan = self.store.load(plan_id)
            verified, pending = self._verification_service().verify_echoes(plan)
            data = {
                "plan_id": plan_id,
                "verified": verified,
                "pending": pending,
                "recovery": f"blackcell directive reconcile {plan_id}" if pending else None,
            }
            if pending:
                return PendingOutcome(
                    code="pending_projection",
                    message="One or more GitHub Issue echoes are not visible yet.",
                    recovery=f"blackcell directive reconcile {plan_id}",
                    data=data,
                )
            return data

        return self._result(OperationId.ECHO_VERIFY, operation, plan_id=plan_id)

    def recon_status(self, plan_id: str) -> ResultEnvelope:
        return self._result(
            OperationId.RECON_STATUS,
            lambda: self._sync_service().status(plan_id),
            plan_id=plan_id,
        )

    def chronicle_events(self, plan_id: str | None = None) -> ResultEnvelope:
        return self._result(
            OperationId.CHRONICLE_SHOW,
            lambda: {
                "events": [
                    event.model_dump(mode="json") for event in self.chronicle.events(plan_id)
                ]
            },
            plan_id=plan_id,
        )

    def anomalies(self, anomaly_id: int | None = None) -> ResultEnvelope:
        def operation() -> dict[str, Any]:
            all_events = self.chronicle.events()
            events = [
                event
                for event in all_events
                if event.event_type == EventType.ANOMALY_DETECTED
                and (anomaly_id is None or event.id == anomaly_id)
            ]
            if anomaly_id is not None and not events:
                raise NotFoundFailure(f"Anomaly event {anomaly_id} was not found.")
            resolutions = {
                event.payload["anomaly_id"]: event
                for event in all_events
                if event.event_type == EventType.ANOMALY_RESOLVED
            }
            return {
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

        return self._result(OperationId.ANOMALY_LIST, operation)

    def resolve_anomaly(self, anomaly_id: int, note: str) -> ResultEnvelope:
        def operation() -> dict[str, Any]:
            resolution_id = self.chronicle.resolve_anomaly(anomaly_id, note)
            return {"anomaly_id": anomaly_id, "resolution_event_id": resolution_id}

        return self._result(OperationId.ANOMALY_RESOLVE, operation)

    def publication_preflight(
        self,
        stage: PublicationStage = PublicationStage.PULL_REQUEST,
    ) -> ResultEnvelope:
        return self._result(
            OperationId.PUBLICATION_PREFLIGHT,
            lambda: self._publication_service().preflight(stage).model_dump(mode="json"),
        )

    def _schema_audit_data(self) -> dict[str, Any]:
        schema = load_linear_schema()
        protocols = {}
        for protocol in sorted(PLANNING_PROTOCOL_CAPABILITIES, key=lambda item: item.__name__):
            missing = missing_capabilities(schema, protocol)
            capabilities = sorted(
                PLANNING_PROTOCOL_CAPABILITIES[protocol],
                key=lambda capability: capability.label,
            )
            protocols[protocol.__name__] = {
                "capabilities": [capability.label for capability in capabilities],
                "missing": [capability.label for capability in missing],
                "schema_backed": all(
                    capability_is_schema_backed(capability, schema) for capability in capabilities
                ),
            }
        return {
            "schema": {
                "path": str(default_linear_schema_path()),
                "sha256": schema.schema_sha256,
                "query_type": schema.query_name,
                "mutation_type": schema.mutation_name,
                "types": len(schema.types),
            },
            "protocols": protocols,
            "valid": all(not item["missing"] for item in protocols.values()),
        }

    def _workflow_run(self, plan_id: str) -> dict[str, Any] | PendingOutcome:
        workflow_id = f"blackcell://workflow/{plan_id}"
        steps: list[dict[str, Any]] = []

        schema = self._schema_audit_data()
        steps.append(
            self._record_workflow_step(
                plan_id,
                workflow_id,
                "schema_audit",
                "schema",
                "ok",
                data={"schema_sha256": schema["schema"]["sha256"]},
            )
        )

        proposal = self._plan_service(require_linear=True).reconcile_operation(plan_id)
        project = proposal["project"]
        status_name = (project.get("status") or {}).get("name")
        steps.append(
            self._record_workflow_step(
                plan_id,
                workflow_id,
                "project_contract",
                "project_workflow",
                "ok",
                data={
                    "project_id": project["id"],
                    "status": status_name,
                    "workflow_reconciled": proposal["workflow_reconciled"],
                    "presentation_reconciled": proposal["presentation_reconciled"],
                },
            )
        )

        try:
            ProjectStateMachine(self.config.linear.project_statuses).require(
                status_name,
                ProjectCapability.MATERIALIZE_ASSIGNMENTS,
                message="Workflow requires a materializable Linear Project status.",
                recovery=(
                    f"Move the Linear Project for {plan_id} to "
                    f"{self.config.linear.project_statuses.approved} or "
                    f"{self.config.linear.project_statuses.active}."
                ),
            )
        except PolicyFailure as error:
            recovery = error.recovery or (
                f"Move the Linear Project for {plan_id} to "
                f"{self.config.linear.project_statuses.approved} or "
                f"{self.config.linear.project_statuses.active}."
            )
            steps.append(
                self._record_workflow_step(
                    plan_id,
                    workflow_id,
                    "approval_wait",
                    "lifecycle",
                    "pending",
                    recovery=recovery,
                    data={"status": status_name},
                )
            )
            return PendingOutcome(
                code="approval_wait",
                message="Workflow is waiting for manual Linear Project approval.",
                recovery=recovery,
                data={"workflow_id": workflow_id, "plan_id": plan_id, "steps": steps},
            )

        steps.append(
            self._record_workflow_step(
                plan_id,
                workflow_id,
                "approval_wait",
                "lifecycle",
                "ok",
                data={"status": status_name},
            )
        )

        materialized = self._materialization_service().materialize(plan_id)
        steps.append(
            self._record_workflow_step(
                plan_id,
                workflow_id,
                "assignment_materialize",
                "assignment_contract",
                "ok",
                data={
                    "assignment_mutations": materialized["assignment_mutations"],
                },
            )
        )
        if materialized["pending_relations"]:
            recovery = materialized["recovery"] or f"blackcell workflow resume {plan_id}"
            steps.append(
                self._record_workflow_step(
                    plan_id,
                    workflow_id,
                    "dependency_relations",
                    "assignment_contract",
                    "pending",
                    recovery=recovery,
                    data={
                        "relation_mutations": materialized["relation_mutations"],
                        "pending_relations": materialized["pending_relations"],
                    },
                )
            )
            return PendingOutcome(
                code="dependency_relations_pending",
                message="Workflow dependency relations are waiting for provider readback.",
                recovery=recovery,
                data={"workflow_id": workflow_id, "plan_id": plan_id, "steps": steps},
            )
        steps.append(
            self._record_workflow_step(
                plan_id,
                workflow_id,
                "dependency_relations",
                "assignment_contract",
                "ok",
                data={
                    "relation_mutations": materialized["relation_mutations"],
                },
            )
        )
        if materialized["pending_echoes"]:
            recovery = materialized["recovery"] or f"blackcell workflow resume {plan_id}"
            steps.append(
                self._record_workflow_step(
                    plan_id,
                    workflow_id,
                    "github_echoes",
                    "echo_contract",
                    "pending",
                    recovery=recovery,
                    data={
                        "verified_echoes": materialized["verified_echoes"],
                        "pending_echoes": materialized["pending_echoes"],
                    },
                )
            )
            return PendingOutcome(
                code="github_echoes_pending",
                message="Workflow GitHub echoes are waiting for Linear projection.",
                recovery=recovery,
                data={"workflow_id": workflow_id, "plan_id": plan_id, "steps": steps},
            )
        steps.append(
            self._record_workflow_step(
                plan_id,
                workflow_id,
                "github_echoes",
                "echo_contract",
                "ok",
                data={"echoes": len(materialized["verified_echoes"])},
            )
        )

        try:
            preflight = self._publication_service().preflight(PublicationStage.PUSH)
        except BlackcellError as error:
            recovery = error.recovery or "Correct publication identity before publishing."
            steps.append(
                self._record_workflow_step(
                    plan_id,
                    workflow_id,
                    "publication_preflight",
                    "publication_identity",
                    "pending",
                    recovery=recovery,
                    data={"code": error.code, "details": error.details},
                )
            )
            return PendingOutcome(
                code="publication_preflight_pending",
                message="Workflow stopped before publication because preflight failed.",
                recovery=recovery,
                data={"workflow_id": workflow_id, "plan_id": plan_id, "steps": steps},
            )
        steps.append(
            self._record_workflow_step(
                plan_id,
                workflow_id,
                "publication_preflight",
                "publication_identity",
                "ok",
                data=preflight.model_dump(mode="json"),
            )
        )
        return {"workflow_id": workflow_id, "plan_id": plan_id, "steps": steps}

    def _workflow_status_data(self, plan_id: str) -> dict[str, Any]:
        events = [
            event
            for event in self.chronicle.events(plan_id)
            if event.event_type
            in {
                EventType.WORKFLOW_STEP_COMPLETED,
                EventType.WORKFLOW_STEP_PENDING,
                EventType.WORKFLOW_STEP_FAILED,
            }
        ]
        steps = [event.payload for event in events]
        return {
            "workflow_id": f"blackcell://workflow/{plan_id}",
            "plan_id": plan_id,
            "steps": steps,
            "last_step": steps[-1] if steps else None,
        }

    def _record_workflow_step(
        self,
        plan_id: str,
        workflow_id: str,
        step_id: str,
        invariant_group: str,
        result: str,
        *,
        recovery: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = current_operation.get()
        payload = {
            "workflow_id": workflow_id,
            "step_id": step_id,
            "invariant_group": invariant_group,
            "result": result,
            "recovery": recovery,
            "correlation_id": context.correlation_id if context else None,
            "data": data or {},
        }
        event_type = {
            "ok": EventType.WORKFLOW_STEP_COMPLETED,
            "pending": EventType.WORKFLOW_STEP_PENDING,
        }.get(result, EventType.WORKFLOW_STEP_FAILED)
        self.chronicle.append(event_type, plan_id, payload)
        return payload

    def _result(
        self,
        operation_id: OperationId,
        operation: Callable[[], dict[str, Any] | PendingOutcome],
        *,
        plan_id: str | None = None,
    ) -> ResultEnvelope:
        return self._execute(spec(operation_id), operation, plan_id=plan_id)

    def _execute(
        self,
        operation_spec: OperationSpec,
        operation: Callable[[], dict[str, Any] | PendingOutcome],
        *,
        plan_id: str | None = None,
    ) -> ResultEnvelope:
        return self._executor.execute(operation_spec, operation, plan_id=plan_id)

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

    def _publication_service(self) -> PublicationService:
        backend = self._publication or LocalPublicationAdapter(self.config)
        return PublicationService(self.config, backend)

    def _prepare_credential(self, credential: Credential) -> None:
        if credential is Credential.LINEAR:
            self._linear_adapter()
        elif credential is Credential.GITHUB:
            self._github_adapter()

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
