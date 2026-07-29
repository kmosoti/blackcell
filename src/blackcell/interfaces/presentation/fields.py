"""Explicit UI dispositions for every canonical runtime request field."""

from __future__ import annotations

from collections.abc import Mapping

from blackcell.interfaces.presentation.models import (
    ActionBinding,
    FieldBinding,
    FieldDisposition,
    FieldOption,
)


def _field(
    field_id: str,
    pointer: str,
    label: str,
    control: str,
    *,
    help: str = "",
    required: bool = True,
    default: object = None,
    options: tuple[FieldOption, ...] = (),
) -> FieldBinding:
    return FieldBinding.model_validate(
        {
            "field_id": field_id,
            "json_pointer": pointer,
            "label": label,
            "help": help,
            "control": control,
            "required": required,
            "default": default,
            "options": options,
        }
    )


ACTION_BINDINGS: Mapping[str, ActionBinding] = {
    "register-project": ActionBinding(
        action_id="register-project",
        operation="register-project",
        label="Register project",
        request_schema="project-request/v1",
        fields=(
            _field("project-id", "/project_id", "Project ID", "text"),
            _field("project-root", "/root", "Repository root", "text"),
            _field(
                "configuration-provider",
                "/configuration_provider",
                "Configuration provider",
                "select",
                default="kernform",
                options=(FieldOption(value="kernform", label="Kernform"),),
            ),
            _field(
                "configuration-version",
                "/configuration_version",
                "Configuration contract version",
                "text",
                default="0.2.0",
            ),
            _field(
                "configuration-digest",
                "/configuration_digest",
                "Configuration digest",
                "text",
                help="SHA-256 digest including the sha256: prefix.",
            ),
            _field("project-idempotency", "/idempotency_key", "Idempotency key", "text"),
        ),
    ),
    "accept-intent": ActionBinding(
        action_id="accept-intent",
        operation="accept-intent",
        label="Accept intent",
        request_schema="intent-request/v1",
        fields=(
            _field("intent-id", "/intent_id", "Intent ID", "text"),
            _field("intent-project-id", "/project_id", "Project ID", "text"),
            _field("intent-objective", "/objective", "Objective", "textarea"),
            _field("intent-constraints", "/constraints", "Constraints", "string-list"),
            _field("intent-assumptions", "/assumptions", "Assumptions", "string-list"),
            _field(
                "intent-questions",
                "/unresolved_questions",
                "Unresolved questions",
                "string-list",
            ),
            _field("intent-idempotency", "/idempotency_key", "Idempotency key", "text"),
        ),
    ),
    "accept-plan": ActionBinding(
        action_id="accept-plan",
        operation="accept-plan",
        label="Accept plan",
        request_schema="plan-request/v1",
        fields=(
            _field("plan-id", "/plan_id", "Plan ID", "text"),
            _field("plan-project-id", "/project_id", "Project ID", "text"),
            _field("plan-intent-id", "/intent_id", "Intent ID", "text"),
            _field("plan-base-commit", "/base_commit", "Base commit", "text"),
            _field(
                "plan-effects",
                "/allowed_effects",
                "Allowed effects",
                "structured-list",
                help="Select only effects explicitly authorized for this plan.",
                default=["repository-read", "process"],
            ),
            _field(
                "plan-nodes",
                "/nodes",
                "Plan nodes",
                "structured-list",
                help="Dependency-safe nodes with budgets, paths, effects, and direct checks.",
            ),
            _field("plan-idempotency", "/idempotency_key", "Idempotency key", "text"),
            _field(
                "planning-mode",
                "/planning_mode",
                "Planning mode",
                "select",
                default="declared",
                options=(
                    FieldOption(value="declared", label="Declared"),
                    FieldOption(value="generated", label="Generated"),
                ),
            ),
        ),
    ),
    "submit-run": ActionBinding(
        action_id="submit-run",
        operation="submit-run",
        label="Submit run",
        request_schema="run-request/v1",
        fields=(
            _field("run-id", "/run_id", "Run ID", "text"),
            _field("run-project-id", "/project_id", "Project ID", "text"),
            _field("run-intent-id", "/intent_id", "Intent ID", "text"),
            _field("run-plan-id", "/plan_id", "Plan ID", "text"),
            _field("run-idempotency", "/idempotency_key", "Idempotency key", "text"),
        ),
    ),
    "inspect-run": ActionBinding(
        action_id="inspect-run",
        operation="inspect-run",
        label="Inspect run",
        request_schema="run-lookup/v1",
        fields=(_field("inspect-run-id", "/run_id", "Run ID", "text"),),
    ),
}


_EDITABLE_REASON = "Rendered as a typed user-editable field and validated by the daemon."
_DERIVED_REASON = "Derived from the action's declared external schema identifier."

REQUEST_FIELD_DISPOSITIONS: tuple[FieldDisposition, ...] = tuple(
    [
        FieldDisposition(
            contract=action.request_schema,
            json_pointer="/schema_version",
            disposition="derived",
            reason=_DERIVED_REASON,
        )
        for action in ACTION_BINDINGS.values()
        if action.operation in {"register-project", "accept-intent", "accept-plan", "submit-run"}
    ]
    + [
        FieldDisposition(
            contract=action.request_schema,
            json_pointer=field.json_pointer,
            disposition="editable",
            reason=_EDITABLE_REASON,
        )
        for action in ACTION_BINDINGS.values()
        if action.operation in {"register-project", "accept-intent", "accept-plan", "submit-run"}
        for field in action.fields
    ]
    + [
        FieldDisposition(
            contract="execution-cancel-run-request/v1",
            json_pointer="/schema_version",
            disposition="derived",
            reason=_DERIVED_REASON,
        ),
        FieldDisposition(
            contract="execution-cancel-run-request/v1",
            json_pointer="/idempotency_key",
            disposition="derived",
            reason="Generated locally for one explicit cancellation request.",
        ),
    ]
)


def action_binding(operation: str) -> ActionBinding:
    try:
        return ACTION_BINDINGS[operation]
    except KeyError as error:
        raise ValueError("unknown presentation operation") from error


__all__ = ["ACTION_BINDINGS", "REQUEST_FIELD_DISPOSITIONS", "action_binding"]
