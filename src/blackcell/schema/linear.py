"""Repo-local Linear schema fixture loader and contract checks."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blackcell.contracts.errors import ValidationFailure


class LinearSchemaContractError(ValidationFailure):
    """Raised when the local schema fixture fails contract validation."""


@dataclass(frozen=True)
class LinearSchema:
    """Validated Linear schema fixture contract model."""

    payload: dict[str, Any]
    query_name: str
    mutation_name: str
    schema_sha256: str
    types: dict[str, dict[str, Any]]

    @property
    def query_type(self) -> dict[str, Any]:
        return self.types[self.query_name]

    @property
    def mutation_type(self) -> dict[str, Any]:
        return self.types[self.mutation_name]

    def lookup_type_field(self, type_name: str, field_name: str) -> dict[str, Any] | None:
        return _lookup_field(self.types.get(type_name), field_name, "fields")

    def lookup_input_field(self, type_name: str, field_name: str) -> dict[str, Any] | None:
        return _lookup_field(self.types.get(type_name), field_name, "inputFields")

    def lookup_query_field(self, field_name: str) -> dict[str, Any] | None:
        return self.lookup_type_field(self.query_name, field_name)

    def lookup_mutation_field(self, field_name: str) -> dict[str, Any] | None:
        return self.lookup_type_field(self.mutation_name, field_name)


REQUIRED_OBJECT_CAPABILITIES: dict[str, set[str]] = {
    "Issue": {
        "assignee",
        "delegate",
        "description",
        "id",
        "identifier",
        "labelIds",
        "labels",
        "parent",
        "priority",
        "priorityLabel",
        "project",
        "relations",
        "state",
        "team",
        "title",
        "url",
    },
    "IssueLabel": {"archivedAt", "color", "id", "name", "team"},
    "Project": {
        "color",
        "content",
        "description",
        "externalLinks",
        "icon",
        "id",
        "issues",
        "labelIds",
        "labels",
        "lead",
        "members",
        "name",
        "priority",
        "priorityLabel",
        "status",
        "teams",
        "url",
    },
    "ProjectLabel": {"archivedAt", "color", "id", "name", "projects"},
    "ProjectStatus": {"archivedAt", "id", "name", "type"},
    "Team": {"archivedAt", "id", "issues", "key", "labels", "name", "projects"},
    "User": {"archivedAt", "email", "id", "name"},
    "WorkflowState": {"archivedAt", "id", "name", "team", "type"},
}


REQUIRED_INPUT_CAPABILITIES: dict[str, set[str]] = {
    "IssueCreateInput": {
        "assigneeId",
        "delegateId",
        "description",
        "labelIds",
        "parentId",
        "priority",
        "projectId",
        "stateId",
        "teamId",
        "title",
    },
    "IssueUpdateInput": {
        "assigneeId",
        "delegateId",
        "description",
        "labelIds",
        "parentId",
        "priority",
        "projectId",
        "stateId",
        "teamId",
        "title",
    },
    "ProjectCreateInput": {
        "color",
        "content",
        "description",
        "icon",
        "labelIds",
        "leadId",
        "memberIds",
        "name",
        "priority",
        "statusId",
        "teamIds",
    },
    "ProjectUpdateInput": {
        "color",
        "content",
        "description",
        "icon",
        "labelIds",
        "leadId",
        "memberIds",
        "priority",
        "statusId",
        "teamIds",
    },
}

REQUIRED_QUERY_FIELDS: set[str] = {
    "integrations",
    "issueLabels",
    "issues",
    "project",
    "projectLabels",
    "projects",
    "projectStatuses",
    "team",
    "users",
    "viewer",
    "workflowStates",
}

REQUIRED_MUTATION_FIELDS: set[str] = {
    "entityExternalLinkCreate",
    "entityExternalLinkUpdate",
    "issueCreate",
    "issueRelationCreate",
    "issueUpdate",
    "projectAddLabel",
    "projectCreate",
    "projectLabelCreate",
    "projectRemoveLabel",
    "projectUpdate",
}


def canonical_linear_schema_sha256(payload: dict[str, Any]) -> str:
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _as_dict(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LinearSchemaContractError(f"Expected object for {context}.")
    return value


def _as_name(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise LinearSchemaContractError(f"Expected non-empty string for {context}.")
    return value


def _as_list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise LinearSchemaContractError(f"Expected array for {context}.")
    return value


def _lookup_field(
    type_payload: dict[str, Any] | None, field_name: str, field_key: str
) -> dict[str, Any] | None:
    if not type_payload or not isinstance(type_payload.get(field_key), list):
        return None
    for field in type_payload[field_key]:
        if not isinstance(field, dict):
            continue
        if field.get("name") == field_name:
            return field
    return None


def _index_types(types: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for entry in types:
        if not isinstance(entry, dict):
            raise LinearSchemaContractError("Expected object entries in __schema.types.")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise LinearSchemaContractError("Expected named type in __schema.types.")
        if name in index:
            raise LinearSchemaContractError(f"Duplicate type definition: {name}")
        index[name] = entry
    return index


def _assert_required_capabilities(schema: LinearSchema) -> None:
    for type_name, required_fields in REQUIRED_OBJECT_CAPABILITIES.items():
        for field in sorted(required_fields):
            if schema.lookup_type_field(type_name, field) is None:
                raise LinearSchemaContractError(
                    f"Missing required Linear object capability: {type_name}.{field}"
                )

    for type_name, required_fields in REQUIRED_INPUT_CAPABILITIES.items():
        for field in sorted(required_fields):
            if schema.lookup_input_field(type_name, field) is None:
                raise LinearSchemaContractError(
                    f"Missing required Linear input capability: {type_name}.{field}"
                )

    for field in sorted(REQUIRED_QUERY_FIELDS):
        if schema.lookup_query_field(field) is None:
            raise LinearSchemaContractError(
                f"Missing required Linear query capability: Query.{field}"
            )

    for field in sorted(REQUIRED_MUTATION_FIELDS):
        if schema.lookup_mutation_field(field) is None:
            raise LinearSchemaContractError(
                f"Missing required Linear mutation capability: Mutation.{field}"
            )


def parse_linear_schema(payload: dict[str, Any]) -> LinearSchema:
    raw = _as_dict(payload, "linear schema payload")
    schema_payload = _as_dict(raw.get("__schema"), "linear schema __schema")

    query = _as_dict(schema_payload.get("queryType"), "__schema.queryType")
    mutation = _as_dict(schema_payload.get("mutationType"), "__schema.mutationType")
    query_name = _as_name(query.get("name"), "__schema.queryType.name")
    mutation_name = _as_name(mutation.get("name"), "__schema.mutationType.name")

    types = _as_list(schema_payload.get("types"), "__schema.types")
    type_index = _index_types(types)

    if query_name not in type_index:
        raise LinearSchemaContractError(f"Unknown query type declared by schema: {query_name}")
    if mutation_name not in type_index:
        raise LinearSchemaContractError(
            f"Unknown mutation type declared by schema: {mutation_name}"
        )

    contract = LinearSchema(
        payload=raw,
        query_name=query_name,
        mutation_name=mutation_name,
        schema_sha256=canonical_linear_schema_sha256(raw),
        types=type_index,
    )
    _assert_required_capabilities(contract)
    return contract


def load_linear_schema(path: Path | None = None) -> LinearSchema:
    schema_path = Path(path) if path is not None else default_linear_schema_path()
    payload = json.loads(schema_path.read_text(encoding="utf-8"))
    return parse_linear_schema(payload)


def default_linear_schema_path() -> Path:
    return Path(__file__).resolve().parents[3] / "docs" / "linear_api_definition.json"
