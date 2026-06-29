"""Linear schema fixture contract tests."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from blackcell.schema.linear import (
    REQUIRED_INPUT_CAPABILITIES,
    REQUIRED_MUTATION_FIELDS,
    REQUIRED_OBJECT_CAPABILITIES,
    REQUIRED_QUERY_FIELDS,
    LinearSchemaContractError,
    canonical_linear_schema_sha256,
    load_linear_schema,
    parse_linear_schema,
)


@pytest.fixture
def linear_schema_payload() -> dict[str, Any]:
    return json.loads(
        (Path(__file__).resolve().parents[2] / "docs" / "linear_api_definition.json").read_text(
            encoding="utf-8"
        )
    )


def test_linear_schema_loader_validates_fixture_and_exposes_lookup_paths() -> None:
    schema = load_linear_schema()

    assert schema.lookup_query_field("team") is not None
    assert schema.lookup_mutation_field("issueCreate") is not None
    assert schema.lookup_input_field("IssueCreateInput", "stateId") is not None
    assert isinstance(schema.schema_sha256, str)
    assert len(schema.schema_sha256) == 64


def test_schema_hash_is_canonical_sha256(linear_schema_payload: dict) -> None:
    schema = parse_linear_schema(linear_schema_payload)
    expected = canonical_linear_schema_sha256(linear_schema_payload)

    assert schema.schema_sha256 == expected


@pytest.mark.parametrize(
    ("type_name", "field_name"),
    [
        (type_name, field)
        for type_name, fields in sorted(REQUIRED_INPUT_CAPABILITIES.items())
        for field in sorted(fields)
    ],
)
def test_required_inputs_are_present(type_name: str, field_name: str) -> None:
    schema = load_linear_schema()

    assert schema.lookup_input_field(type_name, field_name) is not None


@pytest.mark.parametrize(
    ("type_name", "field_name"),
    [
        (type_name, field)
        for type_name, fields in sorted(REQUIRED_OBJECT_CAPABILITIES.items())
        for field in sorted(fields)
    ],
)
def test_required_objects_and_fields_are_present(type_name: str, field_name: str) -> None:
    schema = load_linear_schema()

    assert schema.lookup_type_field(type_name, field_name) is not None


@pytest.mark.parametrize(
    "field_name",
    sorted(REQUIRED_QUERY_FIELDS),
)
def test_required_query_fields_are_present(field_name: str) -> None:
    schema = load_linear_schema()

    assert schema.lookup_query_field(field_name) is not None


@pytest.mark.parametrize(
    "field_name",
    sorted(REQUIRED_MUTATION_FIELDS),
)
def test_required_mutation_fields_are_present(field_name: str) -> None:
    schema = load_linear_schema()

    assert schema.lookup_mutation_field(field_name) is not None


@pytest.mark.parametrize(
    ("type_name", "field_name"),
    [
        ("IssueCreateInput", field)
        for field in [
            "assigneeId",
            "delegateId",
            "labelIds",
            "parentId",
            "priority",
            "projectId",
            "stateId",
            "teamId",
        ]
    ]
    + [
        ("IssueUpdateInput", field)
        for field in [
            "assigneeId",
            "delegateId",
            "labelIds",
            "parentId",
            "priority",
            "projectId",
            "stateId",
            "teamId",
        ]
    ]
    + [
        ("ProjectCreateInput", field)
        for field in ["leadId", "memberIds", "labelIds", "priority", "statusId"]
    ]
    + [
        ("ProjectUpdateInput", field)
        for field in ["leadId", "memberIds", "labelIds", "priority", "statusId"]
    ],
)
def test_required_fields_removed_or_renamed_fail_contract(
    linear_schema_payload: dict,
    type_name: str,
    field_name: str,
) -> None:
    schema_payload = copy.deepcopy(linear_schema_payload)
    _remove_type_input_field(schema_payload, type_name, field_name)

    with pytest.raises(LinearSchemaContractError, match="Missing required Linear"):
        parse_linear_schema(schema_payload)


@pytest.mark.parametrize(
    ("type_name", "field_name"),
    [
        ("Project", "lead"),
        ("Project", "members"),
        ("Project", "labels"),
        ("Project", "priority"),
        ("Issue", "delegate"),
        ("Issue", "assignee"),
        ("Issue", "relations"),
        ("User", "email"),
    ],
)
def test_required_object_fields_removed_or_renamed_fail_contract(
    linear_schema_payload: dict,
    type_name: str,
    field_name: str,
) -> None:
    schema_payload = copy.deepcopy(linear_schema_payload)
    _remove_type_field(schema_payload, type_name, field_name)

    with pytest.raises(LinearSchemaContractError, match="Missing required Linear"):
        parse_linear_schema(schema_payload)


def _remove_type_input_field(payload: dict, type_name: str, field_name: str) -> None:
    for schema_type in payload["__schema"]["types"]:
        if schema_type.get("name") != type_name:
            continue
        schema_type["inputFields"] = [
            field for field in schema_type["inputFields"] if field.get("name") != field_name
        ]
        break
    else:
        raise AssertionError(f"Type {type_name} not found in linear schema payload")


def _remove_type_field(payload: dict, type_name: str, field_name: str) -> None:
    for schema_type in payload["__schema"]["types"]:
        if schema_type.get("name") != type_name:
            continue
        schema_type["fields"] = [
            field for field in schema_type["fields"] if field.get("name") != field_name
        ]
        break
    else:
        raise AssertionError(f"Type {type_name} not found in linear schema payload")
