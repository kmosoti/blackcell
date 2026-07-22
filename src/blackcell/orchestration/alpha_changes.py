"""Closed alpha contracts for proposal-only text changes.

Provider context contains bounded repository evidence but no repository location or execution
handle. Provider output is inert until a separate host executor validates every declared effect.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath

from blackcell.gateway import DataClassification, GatewayBudget, LocalityPolicy
from blackcell.kernel import JsonInput, JsonValue
from blackcell.kernel._json import bytes_digest, json_digest

ALPHA_CHANGE_CONTEXT_SCHEMA = "alpha-change-context/v1"
ALPHA_CHANGE_PROPOSAL_SCHEMA_VERSION = "alpha-change-proposal/v1"
ALPHA_CHANGE_PROVIDER_RESULT_SCHEMA = "alpha-change-provider-result/v1"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_COMMIT_ID = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_OBJECTIVE_BYTES = 8 * 1024
_MAX_CONSTRAINTS = 64
_MAX_CONSTRAINT_BYTES = 2 * 1024
MAX_ALPHA_EVIDENCE_FILES = 64
MAX_ALPHA_EVIDENCE_FILE_BYTES = 256 * 1024
MAX_ALPHA_EVIDENCE_BYTES = 1024 * 1024
_MAX_ALLOWED_PATHS = 256
_MAX_CHANGED_PATHS = 10_000
_MAX_OPERATIONS = 256
_MAX_OPERATION_CONTENT_BYTES = 1024 * 1024
_MAX_PROPOSAL_CONTENT_BYTES = 4 * 1024 * 1024
_MAX_SUMMARY_BYTES = 4 * 1024
_MAX_PATH_CHARS = 4096


class AlphaChangeContractFailureCode(StrEnum):
    INVALID_EVIDENCE = "invalid-alpha-change-evidence"
    INVALID_PROPOSAL = "invalid-alpha-change-proposal"


class AlphaChangeContractError(ValueError):
    """A content-free contract failure."""

    def __init__(self, code: AlphaChangeContractFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class AlphaTextOperation(StrEnum):
    CREATE = "create"
    REPLACE = "replace"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class AlphaEvidenceFile:
    path: str
    content: str = field(repr=False)
    content_digest: str

    def __post_init__(self) -> None:
        path = _repository_path(self.path, AlphaChangeContractFailureCode.INVALID_EVIDENCE)
        content = _bounded_utf8(
            self.content,
            maximum=MAX_ALPHA_EVIDENCE_FILE_BYTES,
            code=AlphaChangeContractFailureCode.INVALID_EVIDENCE,
            allow_empty=True,
        )
        if self.content_digest != bytes_digest(content.encode("utf-8")):
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_EVIDENCE)
        object.__setattr__(self, "path", path)


@dataclass(frozen=True, slots=True)
class AlphaChangeContext:
    objective: str
    constraints: tuple[str, ...]
    base_commit: str
    allowed_paths: tuple[str, ...]
    max_changed_paths: int
    files: tuple[AlphaEvidenceFile, ...]
    schema_version: str = ALPHA_CHANGE_CONTEXT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != ALPHA_CHANGE_CONTEXT_SCHEMA:
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_EVIDENCE)
        _bounded_utf8(
            self.objective,
            maximum=_MAX_OBJECTIVE_BYTES,
            code=AlphaChangeContractFailureCode.INVALID_EVIDENCE,
        )
        if (
            not isinstance(self.constraints, tuple)
            or len(self.constraints) > _MAX_CONSTRAINTS
            or len(self.constraints) != len(set(self.constraints))
        ):
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_EVIDENCE)
        for constraint in self.constraints:
            _bounded_utf8(
                constraint,
                maximum=_MAX_CONSTRAINT_BYTES,
                code=AlphaChangeContractFailureCode.INVALID_EVIDENCE,
            )
        if not isinstance(self.base_commit, str) or _COMMIT_ID.fullmatch(self.base_commit) is None:
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_EVIDENCE)
        if (
            not isinstance(self.allowed_paths, tuple)
            or len(self.allowed_paths) > _MAX_ALLOWED_PATHS
        ):
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_EVIDENCE)
        allowed_paths = tuple(
            sorted(
                _policy_path(path, AlphaChangeContractFailureCode.INVALID_EVIDENCE)
                for path in self.allowed_paths
            )
        )
        if len(allowed_paths) != len(set(allowed_paths)):
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_EVIDENCE)
        if (
            isinstance(self.max_changed_paths, bool)
            or not isinstance(self.max_changed_paths, int)
            or not 0 <= self.max_changed_paths <= _MAX_CHANGED_PATHS
        ):
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_EVIDENCE)
        if not isinstance(self.files, tuple) or len(self.files) > MAX_ALPHA_EVIDENCE_FILES:
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_EVIDENCE)
        if not all(isinstance(item, AlphaEvidenceFile) for item in self.files):
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_EVIDENCE)
        files = tuple(sorted(self.files, key=lambda item: item.path))
        if len({item.path for item in files}) != len(files):
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_EVIDENCE)
        if sum(len(item.content.encode("utf-8")) for item in files) > MAX_ALPHA_EVIDENCE_BYTES:
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_EVIDENCE)
        object.__setattr__(self, "allowed_paths", allowed_paths)
        object.__setattr__(self, "files", files)

    @property
    def digest(self) -> str:
        return json_digest(alpha_change_context_payload(self))


@dataclass(frozen=True, slots=True)
class AlphaFileChange:
    operation: AlphaTextOperation
    path: str
    expected_digest: str | None
    content: str | None = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.operation, AlphaTextOperation):
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)
        path = _repository_path(self.path, AlphaChangeContractFailureCode.INVALID_PROPOSAL)
        if self.operation is AlphaTextOperation.CREATE:
            if self.expected_digest is not None or not isinstance(self.content, str):
                raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)
        elif self.operation is AlphaTextOperation.REPLACE:
            if _DIGEST.fullmatch(self.expected_digest or "") is None or not isinstance(
                self.content, str
            ):
                raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)
        elif _DIGEST.fullmatch(self.expected_digest or "") is None or self.content is not None:
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)
        if self.content is not None:
            content = _bounded_utf8(
                self.content,
                maximum=_MAX_OPERATION_CONTENT_BYTES,
                code=AlphaChangeContractFailureCode.INVALID_PROPOSAL,
                allow_empty=True,
            )
            if (
                self.operation is AlphaTextOperation.REPLACE
                and bytes_digest(content.encode("utf-8")) == self.expected_digest
            ):
                raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)
        object.__setattr__(self, "path", path)

    @property
    def content_digest(self) -> str | None:
        if self.content is None:
            return None
        return bytes_digest(self.content.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class AlphaChangeProposal:
    proposal_id: str
    evidence_digest: str
    operations: tuple[AlphaFileChange, ...]
    summary: str
    schema_version: str = ALPHA_CHANGE_PROPOSAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            self.schema_version != ALPHA_CHANGE_PROPOSAL_SCHEMA_VERSION
            or not isinstance(self.proposal_id, str)
            or _IDENTIFIER.fullmatch(self.proposal_id) is None
            or not isinstance(self.evidence_digest, str)
            or _DIGEST.fullmatch(self.evidence_digest) is None
        ):
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)
        if (
            not isinstance(self.operations, tuple)
            or not 1 <= len(self.operations) <= _MAX_OPERATIONS
        ):
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)
        if not all(isinstance(item, AlphaFileChange) for item in self.operations):
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)
        operations = tuple(sorted(self.operations, key=lambda item: item.path))
        if len({item.path for item in operations}) != len(operations):
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)
        if (
            sum(len((item.content or "").encode("utf-8")) for item in operations)
            > _MAX_PROPOSAL_CONTENT_BYTES
        ):
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)
        _bounded_utf8(
            self.summary,
            maximum=_MAX_SUMMARY_BYTES,
            code=AlphaChangeContractFailureCode.INVALID_PROPOSAL,
        )
        object.__setattr__(self, "operations", operations)

    @property
    def digest(self) -> str:
        return json_digest(alpha_change_proposal_payload(self))


@dataclass(frozen=True, slots=True)
class AlphaChangeProviderCall:
    request_id: str
    correlation_id: str
    run_id: str
    node_id: str
    context: AlphaChangeContext
    classification: DataClassification
    locality: LocalityPolicy
    budget: GatewayBudget
    estimated_input_tokens: int
    causation_id: str | None = None

    def __post_init__(self) -> None:
        for value in (self.request_id, self.correlation_id, self.run_id, self.node_id):
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_EVIDENCE)
        if self.causation_id is not None and (
            not isinstance(self.causation_id, str)
            or not self.causation_id.strip()
            or len(self.causation_id) > 256
        ):
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_EVIDENCE)
        if (
            not isinstance(self.context, AlphaChangeContext)
            or not isinstance(self.classification, DataClassification)
            or not isinstance(self.locality, LocalityPolicy)
            or not isinstance(self.budget, GatewayBudget)
            or isinstance(self.estimated_input_tokens, bool)
            or not isinstance(self.estimated_input_tokens, int)
            or self.estimated_input_tokens < 0
        ):
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_EVIDENCE)


@dataclass(frozen=True, slots=True)
class AlphaChangeProviderResult:
    proposal: AlphaChangeProposal
    provider_output_digest: str
    profile_id: str
    adapter_id: str
    model_id: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_microusd: int
    completed_at: datetime
    schema_version: str = ALPHA_CHANGE_PROVIDER_RESULT_SCHEMA

    def __post_init__(self) -> None:
        if (
            self.schema_version != ALPHA_CHANGE_PROVIDER_RESULT_SCHEMA
            or not isinstance(self.proposal, AlphaChangeProposal)
            or not isinstance(self.provider_output_digest, str)
            or _DIGEST.fullmatch(self.provider_output_digest) is None
        ):
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)
        for value in (self.profile_id, self.adapter_id, self.model_id):
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)
        usage = (self.input_tokens, self.output_tokens, self.latency_ms, self.cost_microusd)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in usage
        ):
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)
        if self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None:
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)


ALPHA_CHANGE_PROPOSAL_OUTPUT_SCHEMA: Mapping[str, JsonValue] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": (
        "schema_version",
        "proposal_id",
        "evidence_digest",
        "operations",
        "summary",
    ),
    "properties": {
        "schema_version": {"type": "string", "const": ALPHA_CHANGE_PROPOSAL_SCHEMA_VERSION},
        "proposal_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "evidence_digest": {"type": "string", "minLength": 71, "maxLength": 71},
        "operations": {
            "type": "array",
            "minItems": 1,
            "maxItems": _MAX_OPERATIONS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ("operation", "path", "expected_digest", "content"),
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": tuple(item.value for item in AlphaTextOperation),
                    },
                    "path": {"type": "string", "minLength": 1, "maxLength": _MAX_PATH_CHARS},
                    "expected_digest": {
                        "type": ("string", "null"),
                        "maxLength": 71,
                    },
                    "content": {
                        "type": ("string", "null"),
                        "maxLength": _MAX_OPERATION_CONTENT_BYTES,
                    },
                },
            },
        },
        "summary": {"type": "string", "minLength": 1, "maxLength": _MAX_SUMMARY_BYTES},
    },
}


def alpha_change_context_payload(context: AlphaChangeContext) -> dict[str, JsonInput]:
    if not isinstance(context, AlphaChangeContext):
        raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_EVIDENCE)
    return {
        "schema_version": context.schema_version,
        "objective": context.objective,
        "constraints": list(context.constraints),
        "base_commit": context.base_commit,
        "allowed_paths": list(context.allowed_paths),
        "max_changed_paths": context.max_changed_paths,
        "files": [
            {
                "path": item.path,
                "content": item.content,
                "content_digest": item.content_digest,
            }
            for item in context.files
        ],
    }


def alpha_change_proposal_payload(proposal: AlphaChangeProposal) -> dict[str, JsonInput]:
    if not isinstance(proposal, AlphaChangeProposal):
        raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)
    return {
        "schema_version": proposal.schema_version,
        "proposal_id": proposal.proposal_id,
        "evidence_digest": proposal.evidence_digest,
        "operations": [
            {
                "operation": item.operation.value,
                "path": item.path,
                "expected_digest": item.expected_digest,
                "content": item.content,
            }
            for item in proposal.operations
        ],
        "summary": proposal.summary,
    }


def alpha_change_provider_result_payload(
    result: AlphaChangeProviderResult,
) -> dict[str, JsonInput]:
    if not isinstance(result, AlphaChangeProviderResult):
        raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)
    return {
        "schema_version": result.schema_version,
        "proposal_digest": result.proposal.digest,
        "provider_output_digest": result.provider_output_digest,
        "profile_id": result.profile_id,
        "adapter_id": result.adapter_id,
        "model_id": result.model_id,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "latency_ms": result.latency_ms,
        "cost_microusd": result.cost_microusd,
        "completed_at": result.completed_at.isoformat(),
    }


def alpha_change_proposal_from_mapping(value: Mapping[str, object]) -> AlphaChangeProposal:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "proposal_id",
        "evidence_digest",
        "operations",
        "summary",
    }:
        raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)
    raw_operations = value.get("operations")
    if (
        not isinstance(raw_operations, Sequence)
        or isinstance(raw_operations, str | bytes | bytearray)
        or not 1 <= len(raw_operations) <= _MAX_OPERATIONS
    ):
        raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)
    operations: list[AlphaFileChange] = []
    for raw_operation in raw_operations:
        if not isinstance(raw_operation, Mapping) or set(raw_operation) != {
            "operation",
            "path",
            "expected_digest",
            "content",
        }:
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)
        try:
            operation = AlphaTextOperation(raw_operation.get("operation"))
        except TypeError, ValueError:
            raise AlphaChangeContractError(
                AlphaChangeContractFailureCode.INVALID_PROPOSAL
            ) from None
        path = raw_operation.get("path")
        expected_digest = raw_operation.get("expected_digest")
        content = raw_operation.get("content")
        if (
            not isinstance(path, str)
            or (expected_digest is not None and not isinstance(expected_digest, str))
            or (content is not None and not isinstance(content, str))
        ):
            raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)
        operations.append(AlphaFileChange(operation, path, expected_digest, content))
    schema_version = value.get("schema_version")
    proposal_id = value.get("proposal_id")
    evidence_digest = value.get("evidence_digest")
    summary = value.get("summary")
    if (
        not isinstance(schema_version, str)
        or not isinstance(proposal_id, str)
        or not isinstance(evidence_digest, str)
        or not isinstance(summary, str)
    ):
        raise AlphaChangeContractError(AlphaChangeContractFailureCode.INVALID_PROPOSAL)
    return AlphaChangeProposal(
        proposal_id=proposal_id,
        evidence_digest=evidence_digest,
        operations=tuple(operations),
        summary=summary,
        schema_version=schema_version,
    )


def _policy_path(value: str, code: AlphaChangeContractFailureCode) -> str:
    if value == ".":
        return value
    return _repository_path(value, code)


def _repository_path(value: str, code: AlphaChangeContractFailureCode) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_PATH_CHARS
        or "\x00" in value
        or "\\" in value
    ):
        raise AlphaChangeContractError(code)
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or ".git" in path.parts
    ):
        raise AlphaChangeContractError(code)
    return value


def _bounded_utf8(
    value: str,
    *,
    maximum: int,
    code: AlphaChangeContractFailureCode,
    allow_empty: bool = False,
) -> str:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value.strip())
        or "\x00" in value
        or len(value.encode("utf-8")) > maximum
    ):
        raise AlphaChangeContractError(code)
    return value


__all__ = [
    "ALPHA_CHANGE_CONTEXT_SCHEMA",
    "ALPHA_CHANGE_PROPOSAL_OUTPUT_SCHEMA",
    "ALPHA_CHANGE_PROPOSAL_SCHEMA_VERSION",
    "ALPHA_CHANGE_PROVIDER_RESULT_SCHEMA",
    "MAX_ALPHA_EVIDENCE_BYTES",
    "MAX_ALPHA_EVIDENCE_FILES",
    "MAX_ALPHA_EVIDENCE_FILE_BYTES",
    "AlphaChangeContext",
    "AlphaChangeContractError",
    "AlphaChangeContractFailureCode",
    "AlphaChangeProposal",
    "AlphaChangeProviderCall",
    "AlphaChangeProviderResult",
    "AlphaEvidenceFile",
    "AlphaFileChange",
    "AlphaTextOperation",
    "alpha_change_context_payload",
    "alpha_change_proposal_from_mapping",
    "alpha_change_proposal_payload",
    "alpha_change_provider_result_payload",
]
