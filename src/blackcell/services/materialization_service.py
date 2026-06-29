"""Idempotent approved assignment and relation materialization."""

from typing import Any

from blackcell.config.model import BlackcellConfig
from blackcell.contracts.errors import ConflictFailure, NotFoundFailure, PolicyFailure
from blackcell.contracts.markers import item_marker
from blackcell.contracts.plan import PlanSpec, Priority, WorkItemSpec
from blackcell.ledger.sqlite import Chronicle, EventType
from blackcell.policy.approval import verify_approved_project
from blackcell.policy.identity import verify_viewer_and_team
from blackcell.services.rendering import (
    normalize_presentation_text,
    render_issue_description,
    render_project_description,
    render_project_summary,
)

LINEAR_PRIORITY = {
    Priority.CRITICAL: 1,
    Priority.HIGH: 2,
    Priority.MEDIUM: 3,
    Priority.LOW: 4,
}


class MaterializationService:
    def __init__(
        self,
        config: BlackcellConfig,
        chronicle: Chronicle,
        store: Any,
        linear: Any,
        verification: Any,
    ) -> None:
        self.config = config
        self.chronicle = chronicle
        self.store = store
        self.linear = linear
        self.verification = verification

    def materialize(
        self, plan_id: str, *, projection_timeout: float | None = None
    ) -> dict[str, Any]:
        with self.chronicle.plan_lock(plan_id):
            plan = self.store.load(plan_id)
            unresolved = self.chronicle.unresolved_anomalies(plan_id)
            if unresolved:
                raise PolicyFailure(
                    "Directive has unresolved anomalies.",
                    recovery=f"Run blackcell anomaly list and review {plan_id}.",
                    details={"anomaly_ids": [event.id for event in unresolved]},
                )
            project = self._approved_project(plan)
            states = {
                state["name"]: state
                for state in self.linear.workflow_states(self.config.linear.team_id)
            }
            backlog_name = self.config.linear.issue_states.backlog
            if backlog_name not in states:
                raise PolicyFailure(
                    "Required Linear issue state is missing.",
                    details={"missing_state": backlog_name},
                )
            labels = {
                label["name"]: label["id"]
                for label in self.linear.issue_labels(self.config.linear.team_id)
            }
            missing_labels = sorted(
                {label for item in plan.work_items for label in item.labels if label not in labels}
            )
            if missing_labels:
                raise PolicyFailure(
                    "Directive references Linear labels that do not exist.",
                    details={"missing_labels": missing_labels},
                )

            existing = self.linear.team_issues(self.config.linear.team_id)
            references: dict[str, dict[str, Any]] = {}
            created_count = 0
            for item in plan.ordered_work_items():
                identity_marker = f"blackcell://item/{plan.plan_id}/{item.key}?"
                matches = [
                    issue
                    for issue in existing
                    if identity_marker in (issue.get("description") or "")
                ]
                if len(matches) > 1:
                    raise ConflictFailure(
                        "Multiple Linear issues contain an assignment marker.",
                        details={
                            "plan_id": plan.plan_id,
                            "item_key": item.key,
                            "count": len(matches),
                        },
                    )
                expected_marker = item_marker(plan, item)
                parent_id = references[item.parent_key]["id"] if item.parent_key else None
                if matches:
                    issue = matches[0]
                    if expected_marker not in (issue.get("description") or ""):
                        raise ConflictFailure(
                            "Existing Linear issue has a different directive digest.",
                            details={
                                "plan_id": plan.plan_id,
                                "item_key": item.key,
                                "issue_id": issue.get("id"),
                            },
                        )
                    actual_parent = (issue.get("parent") or {}).get("id")
                    if actual_parent != parent_id:
                        raise ConflictFailure(
                            "Existing Linear issue parent contradicts the directive.",
                            details={
                                "plan_id": plan.plan_id,
                                "item_key": item.key,
                                "expected_parent_id": parent_id,
                                "actual_parent_id": actual_parent,
                            },
                        )
                    self._verify_issue_contract(
                        issue,
                        plan,
                        item,
                        project_id=project["id"],
                        parent_id=parent_id,
                        label_ids={labels[label] for label in item.labels},
                    )
                    self.chronicle.append(
                        EventType.ASSIGNMENT_LOCATED,
                        plan.plan_id,
                        {"issue_id": issue["id"], "identifier": issue["identifier"]},
                        item.key,
                    )
                    if parent_id:
                        self.chronicle.append(
                            EventType.PARENT_RELATION_VERIFIED,
                            plan.plan_id,
                            {"parent_id": parent_id, "issue_id": issue["id"]},
                            item.key,
                        )
                else:
                    issue = self.linear.create_issue(
                        team_id=self.config.linear.team_id,
                        project_id=project["id"],
                        state_id=states[backlog_name]["id"],
                        title=item.title,
                        description=render_issue_description(plan, item),
                        priority=LINEAR_PRIORITY[item.priority],
                        label_ids=[labels[label] for label in item.labels],
                        parent_id=parent_id,
                    )
                    existing.append(issue)
                    created_count += 1
                    self.chronicle.append(
                        EventType.ASSIGNMENT_CREATED,
                        plan.plan_id,
                        {"issue_id": issue["id"], "identifier": issue["identifier"]},
                        item.key,
                    )
                    if parent_id:
                        self.chronicle.append(
                            EventType.PARENT_RELATION_CREATED,
                            plan.plan_id,
                            {"parent_id": parent_id, "issue_id": issue["id"]},
                            item.key,
                        )
                references[item.key] = issue

            relation_mutations = 0
            pending_relations: list[dict[str, str]] = []
            declared_relations = {
                (references[dependency]["id"], references[item.key]["id"])
                for item in plan.work_items
                for dependency in item.blocked_by
            }
            self._verify_no_undeclared_relations(plan, references, declared_relations)
            for item in plan.work_items:
                blocked = references[item.key]
                for dependency_key in item.blocked_by:
                    blocker = references[dependency_key]
                    relations = self.linear.issue_relations(blocker["id"])
                    if self._has_blocking_relation(relations, blocker["id"], blocked["id"]):
                        self.chronicle.append(
                            EventType.BLOCKING_RELATION_VERIFIED,
                            plan.plan_id,
                            {"blocker_id": blocker["id"], "blocked_id": blocked["id"]},
                            item.key,
                        )
                        continue
                    if not self._relation_creation_recorded(
                        plan.plan_id, blocker["id"], blocked["id"]
                    ):
                        self.linear.create_blocking_relation(blocker["id"], blocked["id"])
                        relation_mutations += 1
                        self.chronicle.append(
                            EventType.BLOCKING_RELATION_CREATED,
                            plan.plan_id,
                            {"blocker_id": blocker["id"], "blocked_id": blocked["id"]},
                            item.key,
                        )
                    relations = self.linear.issue_relations(blocker["id"])
                    if self._has_blocking_relation(relations, blocker["id"], blocked["id"]):
                        self.chronicle.append(
                            EventType.BLOCKING_RELATION_VERIFIED,
                            plan.plan_id,
                            {"blocker_id": blocker["id"], "blocked_id": blocked["id"]},
                            item.key,
                        )
                    else:
                        pending = {
                            "item_key": item.key,
                            "blocker_id": blocker["id"],
                            "blocked_id": blocked["id"],
                        }
                        pending_relations.append(pending)
                        self.chronicle.append(
                            EventType.BLOCKING_RELATION_PENDING,
                            plan.plan_id,
                            pending,
                            item.key,
                        )

            timeout = (
                0
                if pending_relations
                else (
                    self.config.materialization.projection_timeout_seconds
                    if projection_timeout is None
                    else projection_timeout
                )
            )
            verified, pending = self.verification.verify_echoes(plan, timeout_seconds=timeout)
            for echo in verified:
                self.chronicle.append(
                    EventType.ECHO_VERIFIED,
                    plan.plan_id,
                    echo,
                    echo["item_key"],
                )
            for item_key in pending:
                self.chronicle.append(
                    EventType.ECHO_PENDING,
                    plan.plan_id,
                    {"recovery": f"blackcell directive reconcile {plan.plan_id}"},
                    item_key,
                )
            if not pending and not pending_relations:
                self.chronicle.append(
                    EventType.MATERIALIZATION_COMPLETED,
                    plan.plan_id,
                    {
                        "assignment_mutations": created_count,
                        "relation_mutations": relation_mutations,
                        "echoes": len(verified),
                    },
                )
            return {
                "plan_id": plan.plan_id,
                "project": {"id": project["id"], "url": project["url"]},
                "assignment_mutations": created_count,
                "relation_mutations": relation_mutations,
                "pending_relations": pending_relations,
                "verified_echoes": verified,
                "pending_echoes": pending,
                "recovery": (
                    f"blackcell directive reconcile {plan.plan_id}"
                    if pending or pending_relations
                    else None
                ),
            }

    def _approved_project(self, plan: PlanSpec) -> dict[str, Any]:
        viewer, team = self.linear.identity_snapshot(self.config.linear.team_id)
        verify_viewer_and_team(viewer, team, self.config)
        matches = self.linear.find_projects_by_marker(
            self.config.linear.team_id, f"blackcell://plan/{plan.plan_id}?"
        )
        if not matches:
            raise NotFoundFailure(f"No Linear Project exists for {plan.plan_id}.")
        if len(matches) > 1:
            raise ConflictFailure(
                "Multiple Linear Projects contain the directive marker.",
                details={"plan_id": plan.plan_id, "count": len(matches)},
            )
        project = matches[0]
        verify_approved_project(project, plan, self.config)
        expected_team_ids = {team["id"] for team in (project.get("teams") or {}).get("nodes", [])}
        if (
            project.get("name") != plan.linear.project_name
            or project.get("description") != render_project_summary(plan)
            or normalize_presentation_text(project.get("content"))
            != normalize_presentation_text(render_project_description(plan, self.config))
            or self.config.linear.team_id not in expected_team_ids
        ):
            raise ConflictFailure(
                "Approved Linear Project contract diverges from the directive.",
                details={"plan_id": plan.plan_id, "project_id": project.get("id")},
            )
        self.chronicle.append(
            EventType.OPERATION_VERIFIED,
            plan.plan_id,
            {"project_id": project["id"], "status": project["status"]["name"]},
        )
        return project

    def _verify_issue_contract(
        self,
        issue: dict[str, Any],
        plan: PlanSpec,
        item: WorkItemSpec,
        *,
        project_id: str,
        parent_id: str | None,
        label_ids: set[str],
    ) -> None:
        actual_label_ids = {label["id"] for label in (issue.get("labels") or {}).get("nodes", [])}
        expected = {
            "title": item.title,
            "description": render_issue_description(plan, item),
            "priority": LINEAR_PRIORITY[item.priority],
            "project_id": project_id,
            "team_id": self.config.linear.team_id,
            "parent_id": parent_id,
            "label_ids": label_ids,
        }
        actual = {
            "title": issue.get("title"),
            "description": issue.get("description"),
            "priority": issue.get("priority"),
            "project_id": (issue.get("project") or {}).get("id"),
            "team_id": (issue.get("team") or {}).get("id"),
            "parent_id": (issue.get("parent") or {}).get("id"),
            "label_ids": actual_label_ids,
        }
        drift: dict[str, Any] = {}
        if normalize_presentation_text(issue.get("description")) != normalize_presentation_text(
            render_issue_description(plan, item)
        ):
            drift["description"] = {
                "expected": expected["description"],
                "actual": actual["description"],
            }
        for key in ("title", "priority", "project_id", "team_id", "parent_id", "label_ids"):
            if actual[key] != expected[key]:
                drift[key] = {"expected": expected[key], "actual": actual[key]}
        if drift:
            raise ConflictFailure(
                "Existing Linear assignment diverges from the directive.",
                details={"plan_id": plan.plan_id, "item_key": item.key, "drift": drift},
            )

    def _relation_creation_recorded(self, plan_id: str, blocker_id: str, blocked_id: str) -> bool:
        return any(
            event.event_type == EventType.BLOCKING_RELATION_CREATED
            and event.payload.get("blocker_id") == blocker_id
            and event.payload.get("blocked_id") == blocked_id
            for event in self.chronicle.events(plan_id)
        )

    def _verify_no_undeclared_relations(
        self,
        plan: PlanSpec,
        references: dict[str, dict[str, Any]],
        declared: set[tuple[str, str]],
    ) -> None:
        plan_issue_ids = {reference["id"] for reference in references.values()}
        unexpected: list[dict[str, str]] = []
        for reference in references.values():
            for relation in self.linear.issue_relations(reference["id"]):
                pair = (
                    (relation.get("issue") or {}).get("id"),
                    (relation.get("relatedIssue") or {}).get("id"),
                )
                if (
                    relation.get("type") == "blocks"
                    and pair[0] in plan_issue_ids
                    and pair[1] in plan_issue_ids
                    and pair not in declared
                ):
                    unexpected.append({"blocker_id": str(pair[0]), "blocked_id": str(pair[1])})
        if unexpected:
            raise ConflictFailure(
                "Linear blocking relations contradict the directive.",
                details={"plan_id": plan.plan_id, "unexpected": unexpected},
            )

    @staticmethod
    def _has_blocking_relation(
        relations: list[dict[str, Any]], blocker_id: str, blocked_id: str
    ) -> bool:
        return any(
            relation.get("type") == "blocks"
            and (relation.get("issue") or {}).get("id") == blocker_id
            and (relation.get("relatedIssue") or {}).get("id") == blocked_id
            for relation in relations
        )
