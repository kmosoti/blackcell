"""Typed planning contracts and deterministic digests."""

import hashlib
import json
import re
import unicodedata
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blackcell.contracts.errors import ValidationFailure
from blackcell.graph.dag import topological_order

PLAN_ID_PATTERN = re.compile(r"^BCP-\d{4}$")
ITEM_KEY_PATTERN = re.compile(r"^BCP-\d{4}-\d{3}$")
LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")


def validate_plan_id(value: str) -> str:
    if not PLAN_ID_PATTERN.fullmatch(value):
        raise ValidationFailure("plan_id must match BCP-NNNN")
    return value


class WorkType(StrEnum):
    TASK = "task"
    BUG = "bug"
    SPIKE = "spike"
    CHORE = "chore"
    ADR = "adr"


class Priority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RepositorySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    owner: str = Field(min_length=1)
    name: str = Field(min_length=1)


class LinearTargetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    team_id: str | None = None
    team_key: str = Field(min_length=1)
    project_name: str = Field(min_length=1)


class WorkItemSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    type: WorkType
    priority: Priority
    labels: list[str]
    acceptance: list[str] = Field(min_length=1)
    parent_key: str | None = None
    blocked_by: list[str]

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not ITEM_KEY_PATTERN.fullmatch(value):
            raise ValueError("work item key must match BCP-NNNN-NNN")
        return value

    @field_validator("title", "description")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("acceptance")
    @classmethod
    def validate_acceptance(cls, value: list[str]) -> list[str]:
        cleaned = [criterion.strip() for criterion in value]
        if not cleaned or any(not criterion for criterion in cleaned):
            raise ValueError("acceptance criteria must contain non-blank entries")
        return cleaned

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("labels must be unique")
        for label in value:
            if not LABEL_PATTERN.fullmatch(label):
                raise ValueError(f"invalid label: {label!r}")
        return value

    @field_validator("blocked_by")
    @classmethod
    def validate_blocked_by(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("blocked_by references must be unique")
        return value


def _normalize(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        return {_normalize(key): _normalize(item) for key, item in value.items()}
    return value


class PlanDigest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm: str = "sha256"
    value: str

    def __str__(self) -> str:
        return f"{self.algorithm}:{self.value}"


class PlanSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    plan_id: str
    revision: int = Field(gt=0)
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    repository: RepositorySpec
    linear: LinearTargetSpec
    work_items: list[WorkItemSpec] = Field(min_length=1)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if value != "0.1":
            raise ValueError("unsupported schema_version; expected 0.1")
        return value

    @field_validator("plan_id")
    @classmethod
    def validate_plan_id(cls, value: str) -> str:
        try:
            return validate_plan_id(value)
        except ValidationFailure as error:
            raise ValueError(error.message) from error

    @field_validator("title", "objective")
    @classmethod
    def strip_plan_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        keys = [item.key for item in self.work_items]
        if len(keys) != len(set(keys)):
            raise ValueError("work item keys must be unique")
        known = set(keys)
        expected_prefix = f"{self.plan_id}-"
        for item in self.work_items:
            if not item.key.startswith(expected_prefix):
                raise ValueError(f"{item.key} does not belong to plan {self.plan_id}")
            if item.parent_key == item.key:
                raise ValueError(f"{item.key} cannot be its own parent")
            if item.parent_key is not None and item.parent_key not in known:
                raise ValueError(f"{item.key} references missing parent {item.parent_key}")
            for dependency in item.blocked_by:
                if dependency == item.key:
                    raise ValueError(f"{item.key} cannot block itself")
                if dependency not in known:
                    raise ValueError(f"{item.key} references missing dependency {dependency}")
        ordering_dependencies = {
            item.key: [*item.blocked_by, *([item.parent_key] if item.parent_key else [])]
            for item in self.work_items
        }
        topological_order(keys, ordering_dependencies)
        return self

    @classmethod
    def from_file(cls, path: str | Path) -> PlanSpec:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls.model_validate(data)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            if isinstance(error, ValidationFailure):
                raise
            raise ValidationFailure(f"Invalid PlanSpec: {error}") from error

    def canonical_bytes(self) -> bytes:
        data = _normalize(self.model_dump(mode="json"))
        return json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def digest(self) -> PlanDigest:
        return PlanDigest(value=hashlib.sha256(self.canonical_bytes()).hexdigest())

    def ordered_work_items(self) -> list[WorkItemSpec]:
        by_key = {item.key: item for item in self.work_items}
        keys = topological_order(
            by_key,
            {
                item.key: [*item.blocked_by, *([item.parent_key] if item.parent_key else [])]
                for item in self.work_items
            },
        )
        return [by_key[key] for key in keys]
