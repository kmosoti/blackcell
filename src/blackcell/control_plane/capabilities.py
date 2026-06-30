import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from blackcell.config import find_repo_root
from blackcell.control_plane.models import (
    ValidationLevel,
    ValidationMessage,
    ValidationResult,
)

CAPABILITY_MANIFEST_PATH = Path("generated/cache/github_graphql_capabilities.json")
GITHUB_PUBLIC_SCHEMA_URL = "https://docs.github.com/public/fpt/schema.docs.graphql"
GITHUB_REFERENCE_URLS = (
    "https://docs.github.com/en/graphql/reference",
    "https://docs.github.com/en/graphql/reference/projects",
    "https://docs.github.com/en/graphql/reference/issues",
    "https://docs.github.com/en/graphql/overview/public-schema",
)


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    kind: str
    name: str
    purpose: str
    parent: str | None = None
    required: bool = True

    @property
    def path(self) -> str:
        if self.parent:
            return f"{self.kind}:{self.parent}.{self.name}"
        return f"{self.kind}:{self.name}"


@dataclass(frozen=True, slots=True)
class GraphQLCapabilityManifest:
    generated_at: str
    schema_url: str
    reference_urls: tuple[str, ...]
    mutations: tuple[str, ...]
    objects: Mapping[str, tuple[str, ...]]
    input_objects: Mapping[str, tuple[str, ...]]
    enums: Mapping[str, tuple[str, ...]]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> GraphQLCapabilityManifest:
        return cls(
            generated_at=_string(data, "generated_at"),
            schema_url=_string(data, "schema_url"),
            reference_urls=tuple(_strings(data, "reference_urls")),
            mutations=tuple(_strings(data, "mutations")),
            objects=_field_mapping(data, "objects"),
            input_objects=_field_mapping(data, "input_objects"),
            enums=_field_mapping(data, "enums"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "schema_url": self.schema_url,
            "reference_urls": list(self.reference_urls),
            "mutations": sorted(self.mutations),
            "objects": _sorted_mapping(self.objects),
            "input_objects": _sorted_mapping(self.input_objects),
            "enums": _sorted_mapping(self.enums),
        }


REQUIRED_GITHUB_CAPABILITIES: tuple[CapabilityRequirement, ...] = (
    CapabilityRequirement(
        kind="mutation",
        name="createProjectV2",
        purpose="project creation",
    ),
    CapabilityRequirement(
        kind="mutation",
        name="createProjectV2Field",
        purpose="project field creation",
    ),
    CapabilityRequirement(
        kind="mutation",
        name="updateProjectV2ItemFieldValue",
        purpose="item field updates and status transitions",
    ),
    CapabilityRequirement(
        kind="mutation",
        name="addProjectV2ItemById",
        purpose="add existing issue or pull request to project",
    ),
    CapabilityRequirement(
        kind="mutation",
        name="deleteProjectV2Item",
        purpose="delete a project item",
    ),
    CapabilityRequirement(
        kind="mutation",
        name="archiveProjectV2Item",
        purpose="archive a project item",
    ),
    CapabilityRequirement(
        kind="mutation",
        name="linkProjectV2ToRepository",
        purpose="repository linking",
    ),
    CapabilityRequirement(
        kind="mutation",
        name="createIssue",
        purpose="issue creation",
    ),
    CapabilityRequirement(
        kind="mutation",
        name="updateIssue",
        purpose="issue updates",
    ),
    CapabilityRequirement(
        kind="input_object",
        name="CreateProjectV2Input",
        purpose="project creation input",
    ),
    CapabilityRequirement(
        kind="input_object",
        name="CreateProjectV2FieldInput",
        purpose="field creation input",
    ),
    CapabilityRequirement(
        kind="input_object",
        name="UpdateProjectV2ItemFieldValueInput",
        purpose="field update input",
    ),
    CapabilityRequirement(
        kind="input_object",
        name="AddProjectV2ItemByIdInput",
        purpose="item add input",
    ),
    CapabilityRequirement(
        kind="input_object",
        name="DeleteProjectV2ItemInput",
        purpose="item delete input",
    ),
    CapabilityRequirement(
        kind="input_object",
        name="ArchiveProjectV2ItemInput",
        purpose="item archive input",
    ),
    CapabilityRequirement(
        kind="input_object",
        name="LinkProjectV2ToRepositoryInput",
        purpose="repository linking input",
    ),
    CapabilityRequirement(
        kind="input_object",
        name="CreateIssueInput",
        purpose="issue creation input",
    ),
    CapabilityRequirement(
        kind="input_object",
        name="UpdateIssueInput",
        purpose="issue update input",
    ),
    CapabilityRequirement(
        kind="object",
        name="ProjectV2",
        purpose="project shape planning",
    ),
    CapabilityRequirement(
        kind="object",
        name="ProjectV2Item",
        purpose="project item reads",
    ),
    CapabilityRequirement(
        kind="object",
        name="Issue",
        purpose="issue reads",
    ),
    CapabilityRequirement(
        kind="object",
        name="Repository",
        purpose="repository binding",
    ),
    CapabilityRequirement(
        kind="enum",
        name="ProjectV2ItemType",
        purpose="project item classification",
    ),
    CapabilityRequirement(
        kind="enum",
        name="ProjectV2CustomFieldType",
        purpose="custom field creation",
    ),
    CapabilityRequirement(
        kind="field",
        parent="Query",
        name="node",
        purpose="lookup project by node id",
    ),
    CapabilityRequirement(
        kind="field",
        parent="Query",
        name="repository",
        purpose="lookup repository by owner/name",
    ),
    CapabilityRequirement(
        kind="field",
        parent="Repository",
        name="issue",
        purpose="read issue by number",
    ),
    CapabilityRequirement(
        kind="field",
        parent="ProjectV2",
        name="items",
        purpose="read project items",
    ),
    CapabilityRequirement(
        kind="field",
        parent="ProjectV2",
        name="fields",
        purpose="read project fields",
    ),
    CapabilityRequirement(
        kind="field",
        parent="ProjectV2",
        name="repositories",
        purpose="read linked repositories",
    ),
)

OPTIONAL_GITHUB_CAPABILITIES: tuple[CapabilityRequirement, ...] = (
    CapabilityRequirement(
        kind="mutation",
        name="unarchiveProjectV2Item",
        purpose="unarchive a project item",
        required=False,
    ),
    CapabilityRequirement(
        kind="object",
        name="ProjectV2Workflow",
        purpose="project workflow declarations where supported",
        required=False,
    ),
    CapabilityRequirement(
        kind="field",
        parent="ProjectV2",
        name="workflows",
        purpose="read project workflows where supported",
        required=False,
    ),
)


def default_capability_manifest_path(start: Path | None = None) -> Path:
    return find_repo_root(start) / CAPABILITY_MANIFEST_PATH


def load_github_capabilities(
    start: Path | None = None,
    *,
    path: Path | None = None,
) -> GraphQLCapabilityManifest:
    manifest_path = path or default_capability_manifest_path(start)
    if not manifest_path.exists():
        raise FileNotFoundError(f"missing GitHub GraphQL capability manifest: {manifest_path}")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise ValueError(f"{manifest_path} must contain a JSON object")
    return GraphQLCapabilityManifest.from_mapping(data)


def write_github_capabilities(
    manifest: GraphQLCapabilityManifest,
    *,
    start: Path | None = None,
    path: Path | None = None,
) -> Path:
    manifest_path = path or default_capability_manifest_path(start)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest.to_mapping(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def refresh_github_capabilities(
    *,
    schema_url: str = GITHUB_PUBLIC_SCHEMA_URL,
    client: httpx.Client | None = None,
) -> GraphQLCapabilityManifest:
    http = client or httpx.Client(timeout=30)
    response = http.get(schema_url)
    response.raise_for_status()
    return manifest_from_schema(response.text, schema_url=schema_url)


def manifest_from_schema(
    schema: str,
    *,
    schema_url: str = GITHUB_PUBLIC_SCHEMA_URL,
) -> GraphQLCapabilityManifest:
    objects: dict[str, set[str]] = {}
    input_objects: dict[str, set[str]] = {}
    enums: dict[str, set[str]] = {}
    current_kind: str | None = None
    current_name: str | None = None
    in_description = False

    for raw_line in schema.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if in_description:
            if line.endswith('"""'):
                in_description = False
            continue
        if line.startswith('"""'):
            if not (line.endswith('"""') and len(line) > 3):
                in_description = True
            continue
        if line.startswith("#"):
            continue

        definition = re.match(r"^(type|interface|input|enum)\s+([_A-Za-z][_0-9A-Za-z]*)", line)
        if definition:
            current_kind = definition.group(1)
            current_name = definition.group(2)
            if current_kind in {"type", "interface"}:
                objects.setdefault(current_name, set())
            elif current_kind == "input":
                input_objects.setdefault(current_name, set())
            elif current_kind == "enum":
                enums.setdefault(current_name, set())
            continue

        if current_name is None or current_kind is None:
            continue
        if line.startswith("}"):
            current_kind = None
            current_name = None
            continue

        name_match = re.match(r"^([_A-Za-z][_0-9A-Za-z]*)\s*(?:\(|:|$)", line)
        if not name_match:
            continue
        name = name_match.group(1)
        if current_kind in {"type", "interface"}:
            objects[current_name].add(name)
        elif current_kind == "input":
            input_objects[current_name].add(name)
        elif current_kind == "enum":
            enums[current_name].add(name)

    return GraphQLCapabilityManifest(
        generated_at=datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        schema_url=schema_url,
        reference_urls=GITHUB_REFERENCE_URLS,
        mutations=tuple(sorted(objects.get("Mutation", ()))),
        objects={name: tuple(sorted(fields)) for name, fields in sorted(objects.items())},
        input_objects={
            name: tuple(sorted(fields)) for name, fields in sorted(input_objects.items())
        },
        enums={name: tuple(sorted(values)) for name, values in sorted(enums.items())},
    )


def validate_github_capabilities(
    start: Path | None = None,
    *,
    path: Path | None = None,
    manifest: GraphQLCapabilityManifest | None = None,
) -> ValidationResult:
    loaded_manifest = manifest or load_github_capabilities(start, path=path)
    messages: list[ValidationMessage] = []
    for requirement in REQUIRED_GITHUB_CAPABILITIES:
        if not _has_requirement(loaded_manifest, requirement):
            messages.append(_message(ValidationLevel.ERROR, requirement))
    for requirement in OPTIONAL_GITHUB_CAPABILITIES:
        if not _has_requirement(loaded_manifest, requirement):
            messages.append(_message(ValidationLevel.WARNING, requirement))
    return ValidationResult.from_messages(messages)


def capability_summary(manifest: GraphQLCapabilityManifest) -> dict[str, object]:
    return {
        "generated_at": manifest.generated_at,
        "schema_url": manifest.schema_url,
        "reference_urls": list(manifest.reference_urls),
        "mutation_count": len(manifest.mutations),
        "object_count": len(manifest.objects),
        "input_object_count": len(manifest.input_objects),
        "enum_count": len(manifest.enums),
    }


def _has_requirement(
    manifest: GraphQLCapabilityManifest,
    requirement: CapabilityRequirement,
) -> bool:
    if requirement.kind == "mutation":
        return requirement.name in manifest.mutations
    if requirement.kind == "object":
        return requirement.name in manifest.objects
    if requirement.kind == "input_object":
        return requirement.name in manifest.input_objects
    if requirement.kind == "enum":
        return requirement.name in manifest.enums
    if requirement.kind == "field" and requirement.parent:
        return requirement.name in manifest.objects.get(requirement.parent, ())
    return False


def _message(level: ValidationLevel, requirement: CapabilityRequirement) -> ValidationMessage:
    return ValidationMessage(
        level=level,
        code="missing_github_graphql_capability",
        message=(
            f"GitHub GraphQL manifest is missing {requirement.path} for {requirement.purpose}"
        ),
        path=f"$.github_graphql.{requirement.path}",
    )


def _field_mapping(data: Mapping[str, Any], key: str) -> dict[str, tuple[str, ...]]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be an object")
    return {str(name): tuple(_strings(value, name)) for name in value}


def _strings(data: Mapping[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of strings")
    return value


def _string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _sorted_mapping(values: Mapping[str, tuple[str, ...]]) -> dict[str, list[str]]:
    return {name: sorted(items) for name, items in sorted(values.items())}
