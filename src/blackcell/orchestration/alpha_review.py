"""Closed, source-cited alpha reviewer contracts and deterministic admission."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import cast

from blackcell.gateway import (
    DataClassification,
    GatewayBudget,
    LocalityPolicy,
)
from blackcell.kernel import JsonInput, JsonValue
from blackcell.kernel._json import canonical_json_bytes, json_digest
from blackcell.orchestration.alpha_changes import AlphaTextOperation

ALPHA_REVIEW_ACCEPTANCE_SCHEMA = "alpha-review-acceptance/v1"
ALPHA_REVIEW_CONTEXT_SCHEMA = "alpha-review-context/v1"
ALPHA_REVIEW_PROPOSAL_SCHEMA = "alpha-review-proposal/v1"
ALPHA_ADMITTED_REVIEW_SCHEMA = "alpha-admitted-review/v1"
ALPHA_REVIEW_PROVIDER_RESULT_SCHEMA = "alpha-review-provider-result/v1"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_MAX_OBJECTIVE_BYTES = 8 * 1024
_MAX_TEXT_BYTES = 4 * 1024
_MAX_CONSTRAINTS = 64
_MAX_NODES = 64
_MAX_CHECKS = 64
_MAX_ARGV = 32
_MAX_ARG_BYTES = 2 * 1024
_MAX_PATHS = 256
_MAX_PATH_CHARS = 4_096
_MAX_EVIDENCE_ITEMS = 128
_MAX_EVIDENCE_EXCERPT_BYTES = 32 * 1024
_MAX_EVIDENCE_BYTES = 512 * 1024
_MAX_ACCEPTANCE_BYTES = 2 * 1024 * 1024
_MAX_CONTEXT_BYTES = 3 * 1024 * 1024
_MAX_PROPOSAL_BYTES = 1024 * 1024
_MAX_FINDINGS = 64
_MAX_CITATIONS = 8
_MAX_LINE = 10_000_000


class AlphaReviewContractFailureCode(StrEnum):
    INVALID_CONTEXT = "invalid-alpha-review-context"
    INVALID_PROPOSAL = "invalid-alpha-review-proposal"
    ADMISSION_REJECTED = "alpha-review-admission-rejected"


class AlphaReviewContractError(ValueError):
    """Content-free rejection of an invalid reviewer contract."""

    def __init__(self, code: AlphaReviewContractFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class AlphaReviewFindingCategory(StrEnum):
    CORRECTNESS = "correctness"
    WEAKENED_TEST = "weakened-test"
    ALTERED_EXPECTED_OUTPUT = "altered-expected-output"
    HIDDEN_SHORTCUT = "hidden-shortcut"
    SUPPRESSED_FAILURE = "suppressed-failure"
    SCOPE_DRIFT = "scope-drift"


class AlphaReviewSeverity(StrEnum):
    BLOCKER = "blocker"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AlphaReviewEvidenceKind(StrEnum):
    SOURCE_BEFORE = "source-before"
    SOURCE_AFTER = "source-after"
    EFFECT = "effect"
    CHECK_COMMAND = "check-command"
    CHECK_RESULT = "check-result"
    CHECK_STDOUT = "check-stdout"
    CHECK_STDERR = "check-stderr"
    OUTCOME = "outcome"


@dataclass(frozen=True, slots=True)
class AlphaReviewCheck:
    check_id: str
    argv: tuple[str, ...] = field(repr=False)
    expected_exit_code: int
    command_digest: str
    result_digest: str
    passed: bool

    def __post_init__(self) -> None:
        if (
            _IDENTIFIER.fullmatch(self.check_id) is None
            or not isinstance(self.argv, tuple)
            or not 1 <= len(self.argv) <= _MAX_ARGV
            or any(
                not isinstance(token, str)
                or not token
                or "\x00" in token
                or len(token.encode("utf-8")) > _MAX_ARG_BYTES
                for token in self.argv
            )
            or isinstance(self.expected_exit_code, bool)
            or not isinstance(self.expected_exit_code, int)
            or not 0 <= self.expected_exit_code <= 255
            or _DIGEST.fullmatch(self.command_digest) is None
            or _DIGEST.fullmatch(self.result_digest) is None
            or not isinstance(self.passed, bool)
        ):
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)


@dataclass(frozen=True, slots=True)
class AlphaReviewPlanNode:
    node_id: str
    objective: str
    depends_on: tuple[str, ...]
    effects: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    max_changed_files: int
    checks: tuple[AlphaReviewCheck, ...]

    def __post_init__(self) -> None:
        if (
            _IDENTIFIER.fullmatch(self.node_id) is None
            or not _bounded_text(self.objective, _MAX_TEXT_BYTES)
            or not _unique_identifiers(self.depends_on, _MAX_NODES)
            or self.node_id in self.depends_on
            or not isinstance(self.effects, tuple)
            or not self.effects
            or len(self.effects) != len(set(self.effects))
            or any(
                effect not in {"repository-read", "repository-write", "process", "network"}
                for effect in self.effects
            )
            or not isinstance(self.allowed_paths, tuple)
            or len(self.allowed_paths) > _MAX_PATHS
            or isinstance(self.max_changed_files, bool)
            or not isinstance(self.max_changed_files, int)
            or not 0 <= self.max_changed_files <= 10_000
            or not isinstance(self.checks, tuple)
            or not 1 <= len(self.checks) <= _MAX_CHECKS
            or not all(isinstance(check, AlphaReviewCheck) for check in self.checks)
            or len({check.check_id for check in self.checks}) != len(self.checks)
        ):
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)
        paths = tuple(sorted(_repository_scope_path(path) for path in self.allowed_paths))
        if len(paths) != len(set(paths)):
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)
        repository_write = "repository-write" in self.effects
        if repository_write != bool(paths) or repository_write != (self.max_changed_files > 0):
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)
        object.__setattr__(self, "allowed_paths", paths)


@dataclass(frozen=True, slots=True)
class AlphaReviewAcceptance:
    run_id: str
    project_id: str
    intent_id: str
    plan_id: str
    objective: str
    constraints: tuple[str, ...]
    base_commit: str
    nodes: tuple[AlphaReviewPlanNode, ...]
    schema_version: str = ALPHA_REVIEW_ACCEPTANCE_SCHEMA

    def __post_init__(self) -> None:
        if (
            self.schema_version != ALPHA_REVIEW_ACCEPTANCE_SCHEMA
            or any(
                _IDENTIFIER.fullmatch(value) is None
                for value in (self.run_id, self.project_id, self.intent_id, self.plan_id)
            )
            or not _bounded_text(self.objective, _MAX_OBJECTIVE_BYTES)
            or not isinstance(self.constraints, tuple)
            or len(self.constraints) > _MAX_CONSTRAINTS
            or len(self.constraints) != len(set(self.constraints))
            or any(not _bounded_text(value, _MAX_TEXT_BYTES) for value in self.constraints)
            or _COMMIT.fullmatch(self.base_commit) is None
            or not isinstance(self.nodes, tuple)
            or not 1 <= len(self.nodes) <= _MAX_NODES
            or not all(isinstance(node, AlphaReviewPlanNode) for node in self.nodes)
        ):
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)
        nodes = tuple(sorted(self.nodes, key=lambda node: node.node_id))
        node_ids = {node.node_id for node in nodes}
        if (
            len(node_ids) != len(nodes)
            or any(not set(node.depends_on).issubset(node_ids) for node in nodes)
            or _has_dependency_cycle(nodes)
        ):
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)
        object.__setattr__(self, "nodes", nodes)
        if len(canonical_json_bytes(alpha_review_acceptance_payload(self))) > _MAX_ACCEPTANCE_BYTES:
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)

    @property
    def digest(self) -> str:
        return json_digest(alpha_review_acceptance_payload(self))


@dataclass(frozen=True, slots=True)
class AlphaReviewEvidence:
    kind: AlphaReviewEvidenceKind
    node_id: str
    artifact_digest: str
    excerpt: str = field(repr=False)
    start_line: int
    path: str | None = None
    check_id: str | None = None
    operation: AlphaTextOperation | None = None
    end_line: int = field(init=False)
    evidence_id: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.kind, AlphaReviewEvidenceKind)
            or _IDENTIFIER.fullmatch(self.node_id) is None
            or _DIGEST.fullmatch(self.artifact_digest) is None
            or not isinstance(self.excerpt, str)
            or "\x00" in self.excerpt
            or len(self.excerpt.encode("utf-8")) > _MAX_EVIDENCE_EXCERPT_BYTES
            or isinstance(self.start_line, bool)
            or not isinstance(self.start_line, int)
            or not 1 <= self.start_line <= _MAX_LINE
            or (self.check_id is not None and _IDENTIFIER.fullmatch(self.check_id) is None)
            or (self.operation is not None and not isinstance(self.operation, AlphaTextOperation))
        ):
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)
        path = None if self.path is None else _repository_evidence_path(self.path)
        source_kind = self.kind in {
            AlphaReviewEvidenceKind.SOURCE_BEFORE,
            AlphaReviewEvidenceKind.SOURCE_AFTER,
            AlphaReviewEvidenceKind.EFFECT,
        }
        check_kind = self.kind in {
            AlphaReviewEvidenceKind.CHECK_COMMAND,
            AlphaReviewEvidenceKind.CHECK_RESULT,
            AlphaReviewEvidenceKind.CHECK_STDOUT,
            AlphaReviewEvidenceKind.CHECK_STDERR,
        }
        if (
            source_kind != (path is not None)
            or source_kind != (self.operation is not None)
            or check_kind != (self.check_id is not None)
            or (
                self.kind is AlphaReviewEvidenceKind.SOURCE_BEFORE
                and self.operation is AlphaTextOperation.CREATE
            )
            or (
                self.kind is AlphaReviewEvidenceKind.SOURCE_AFTER
                and self.operation is AlphaTextOperation.DELETE
            )
        ):
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)
        if self.kind is AlphaReviewEvidenceKind.OUTCOME and (
            path is not None or self.check_id is not None
        ):
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)
        line_count = len(self.excerpt.splitlines()) or 1
        end_line = self.start_line + line_count - 1
        if end_line > _MAX_LINE:
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "end_line", end_line)
        object.__setattr__(
            self,
            "evidence_id",
            json_digest(
                {
                    "kind": self.kind.value,
                    "node_id": self.node_id,
                    "artifact_digest": self.artifact_digest,
                    "operation": (None if self.operation is None else self.operation.value),
                    "path": path,
                    "check_id": self.check_id,
                    "start_line": self.start_line,
                    "end_line": end_line,
                    "excerpt": self.excerpt,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class AlphaReviewContext:
    acceptance: AlphaReviewAcceptance
    state_digest: str
    artifact_evidence_digest: str
    evidence: tuple[AlphaReviewEvidence, ...]
    schema_version: str = ALPHA_REVIEW_CONTEXT_SCHEMA

    def __post_init__(self) -> None:
        if (
            self.schema_version != ALPHA_REVIEW_CONTEXT_SCHEMA
            or not isinstance(self.acceptance, AlphaReviewAcceptance)
            or _DIGEST.fullmatch(self.state_digest) is None
            or _DIGEST.fullmatch(self.artifact_evidence_digest) is None
            or not isinstance(self.evidence, tuple)
            or not 1 <= len(self.evidence) <= _MAX_EVIDENCE_ITEMS
            or not all(isinstance(item, AlphaReviewEvidence) for item in self.evidence)
            or sum(len(item.excerpt.encode("utf-8")) for item in self.evidence)
            > _MAX_EVIDENCE_BYTES
        ):
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)
        evidence = tuple(sorted(self.evidence, key=lambda item: item.evidence_id))
        if len({item.evidence_id for item in evidence}) != len(evidence):
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)
        nodes = {node.node_id: node for node in self.acceptance.nodes}
        for item in evidence:
            node = nodes.get(item.node_id)
            if node is None:
                raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)
            if item.path is not None and not _path_allowed(item.path, node.allowed_paths):
                raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)
            if item.check_id is not None and item.check_id not in {
                check.check_id for check in node.checks
            }:
                raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)
        object.__setattr__(self, "evidence", evidence)
        if len(canonical_json_bytes(alpha_review_context_payload(self))) > _MAX_CONTEXT_BYTES:
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)

    @property
    def digest(self) -> str:
        return json_digest(alpha_review_context_payload(self))


@dataclass(frozen=True, slots=True)
class AlphaReviewCitation:
    evidence_id: str
    start_line: int
    end_line: int

    def __post_init__(self) -> None:
        if (
            _DIGEST.fullmatch(self.evidence_id) is None
            or isinstance(self.start_line, bool)
            or not isinstance(self.start_line, int)
            or isinstance(self.end_line, bool)
            or not isinstance(self.end_line, int)
            or not 1 <= self.start_line <= self.end_line <= _MAX_LINE
        ):
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL)


@dataclass(frozen=True, slots=True)
class AlphaProposedReviewFinding:
    finding_id: str
    category: AlphaReviewFindingCategory
    severity: AlphaReviewSeverity
    claim: str
    impact: str
    recommendation: str
    citations: tuple[AlphaReviewCitation, ...]

    def __post_init__(self) -> None:
        if (
            _IDENTIFIER.fullmatch(self.finding_id) is None
            or not isinstance(self.category, AlphaReviewFindingCategory)
            or not isinstance(self.severity, AlphaReviewSeverity)
            or any(
                not _bounded_text(value, _MAX_TEXT_BYTES)
                for value in (self.claim, self.impact, self.recommendation)
            )
            or not isinstance(self.citations, tuple)
            or not 1 <= len(self.citations) <= _MAX_CITATIONS
            or not all(isinstance(item, AlphaReviewCitation) for item in self.citations)
            or len(set(self.citations)) != len(self.citations)
        ):
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL)


@dataclass(frozen=True, slots=True)
class AlphaReviewProposal:
    context_digest: str
    findings: tuple[AlphaProposedReviewFinding, ...]
    summary: str
    schema_version: str = ALPHA_REVIEW_PROPOSAL_SCHEMA

    def __post_init__(self) -> None:
        if (
            self.schema_version != ALPHA_REVIEW_PROPOSAL_SCHEMA
            or _DIGEST.fullmatch(self.context_digest) is None
            or not isinstance(self.findings, tuple)
            or len(self.findings) > _MAX_FINDINGS
            or not all(isinstance(item, AlphaProposedReviewFinding) for item in self.findings)
            or len({item.finding_id for item in self.findings}) != len(self.findings)
            or not _bounded_text(self.summary, _MAX_TEXT_BYTES)
        ):
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL)
        if len(canonical_json_bytes(alpha_review_proposal_payload(self))) > _MAX_PROPOSAL_BYTES:
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL)

    @property
    def digest(self) -> str:
        return json_digest(alpha_review_proposal_payload(self))


@dataclass(frozen=True, slots=True)
class AlphaAdmittedReview:
    context_digest: str
    acceptance_digest: str
    findings: tuple[AlphaProposedReviewFinding, ...]
    summary: str
    schema_version: str = ALPHA_ADMITTED_REVIEW_SCHEMA

    def __post_init__(self) -> None:
        if (
            self.schema_version != ALPHA_ADMITTED_REVIEW_SCHEMA
            or _DIGEST.fullmatch(self.context_digest) is None
            or _DIGEST.fullmatch(self.acceptance_digest) is None
            or not isinstance(self.findings, tuple)
            or len(self.findings) > _MAX_FINDINGS
            or not all(isinstance(item, AlphaProposedReviewFinding) for item in self.findings)
            or len({item.finding_id for item in self.findings}) != len(self.findings)
            or not _bounded_text(self.summary, _MAX_TEXT_BYTES)
        ):
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.ADMISSION_REJECTED)

    @property
    def digest(self) -> str:
        return json_digest(alpha_admitted_review_payload(self))


@dataclass(frozen=True, slots=True)
class AlphaReviewProviderCall:
    request_id: str
    correlation_id: str
    review_id: str
    context: AlphaReviewContext
    classification: DataClassification
    locality: LocalityPolicy
    budget: GatewayBudget
    estimated_input_tokens: int
    causation_id: str | None = None

    def __post_init__(self) -> None:
        if (
            any(
                not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None
                for value in (self.request_id, self.correlation_id, self.review_id)
            )
            or not isinstance(self.context, AlphaReviewContext)
            or not isinstance(self.classification, DataClassification)
            or not isinstance(self.locality, LocalityPolicy)
            or not isinstance(self.budget, GatewayBudget)
            or isinstance(self.estimated_input_tokens, bool)
            or not isinstance(self.estimated_input_tokens, int)
            or self.estimated_input_tokens < 0
            or (
                self.causation_id is not None
                and (
                    not isinstance(self.causation_id, str)
                    or _IDENTIFIER.fullmatch(self.causation_id) is None
                )
            )
        ):
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)


@dataclass(frozen=True, slots=True)
class AlphaReviewProviderResult:
    proposal: AlphaReviewProposal
    provider_output_digest: str
    profile_id: str
    adapter_id: str
    model_id: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_microusd: int
    completed_at: datetime
    schema_version: str = ALPHA_REVIEW_PROVIDER_RESULT_SCHEMA

    def __post_init__(self) -> None:
        usage = (self.input_tokens, self.output_tokens, self.latency_ms, self.cost_microusd)
        if (
            self.schema_version != ALPHA_REVIEW_PROVIDER_RESULT_SCHEMA
            or not isinstance(self.proposal, AlphaReviewProposal)
            or _DIGEST.fullmatch(self.provider_output_digest) is None
            or any(
                not isinstance(value, str) or not value or len(value) > 256
                for value in (self.profile_id, self.adapter_id, self.model_id)
            )
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in usage
            )
            or not isinstance(self.completed_at, datetime)
            or self.completed_at.tzinfo is None
            or self.completed_at.utcoffset() is None
        ):
            raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL)


def admit_alpha_review(
    context: AlphaReviewContext,
    proposal: AlphaReviewProposal,
) -> AlphaAdmittedReview:
    """Structurally admit cited claims without treating them as verified truth."""

    if (
        not isinstance(context, AlphaReviewContext)
        or not isinstance(proposal, AlphaReviewProposal)
        or proposal.context_digest != context.digest
    ):
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.ADMISSION_REJECTED)
    evidence = {item.evidence_id: item for item in context.evidence}
    for finding in proposal.findings:
        for citation in finding.citations:
            item = evidence.get(citation.evidence_id)
            if (
                item is None
                or citation.start_line < item.start_line
                or citation.end_line > item.end_line
            ):
                raise AlphaReviewContractError(AlphaReviewContractFailureCode.ADMISSION_REJECTED)
    return AlphaAdmittedReview(
        context_digest=context.digest,
        acceptance_digest=context.acceptance.digest,
        findings=proposal.findings,
        summary=proposal.summary,
    )


def alpha_review_acceptance_payload(value: AlphaReviewAcceptance) -> dict[str, JsonInput]:
    if not isinstance(value, AlphaReviewAcceptance):
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)
    return {
        "schema_version": value.schema_version,
        "run_id": value.run_id,
        "project_id": value.project_id,
        "intent_id": value.intent_id,
        "plan_id": value.plan_id,
        "objective": value.objective,
        "constraints": list(value.constraints),
        "base_commit": value.base_commit,
        "nodes": [
            {
                "node_id": node.node_id,
                "objective": node.objective,
                "depends_on": list(node.depends_on),
                "effects": list(node.effects),
                "allowed_paths": list(node.allowed_paths),
                "max_changed_files": node.max_changed_files,
                "checks": [
                    {
                        "check_id": check.check_id,
                        "argv": list(check.argv),
                        "expected_exit_code": check.expected_exit_code,
                        "command_digest": check.command_digest,
                        "result_digest": check.result_digest,
                        "passed": check.passed,
                    }
                    for check in node.checks
                ],
            }
            for node in value.nodes
        ],
    }


def alpha_review_context_payload(value: AlphaReviewContext) -> dict[str, JsonInput]:
    if not isinstance(value, AlphaReviewContext):
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)
    return {
        "schema_version": value.schema_version,
        "acceptance_digest": value.acceptance.digest,
        "acceptance": alpha_review_acceptance_payload(value.acceptance),
        "state_digest": value.state_digest,
        "artifact_evidence_digest": value.artifact_evidence_digest,
        "review_categories": [category.value for category in AlphaReviewFindingCategory],
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "kind": item.kind.value,
                "node_id": item.node_id,
                "artifact_digest": item.artifact_digest,
                "operation": None if item.operation is None else item.operation.value,
                "path": item.path,
                "check_id": item.check_id,
                "start_line": item.start_line,
                "end_line": item.end_line,
                "excerpt": item.excerpt,
            }
            for item in value.evidence
        ],
    }


def alpha_review_proposal_payload(value: AlphaReviewProposal) -> dict[str, JsonInput]:
    if not isinstance(value, AlphaReviewProposal):
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL)
    return {
        "schema_version": value.schema_version,
        "context_digest": value.context_digest,
        "findings": [_finding_payload(finding) for finding in value.findings],
        "summary": value.summary,
    }


def alpha_review_provider_result_payload(
    value: AlphaReviewProviderResult,
) -> dict[str, JsonInput]:
    if not isinstance(value, AlphaReviewProviderResult):
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL)
    return {
        "schema_version": value.schema_version,
        "proposal_digest": value.proposal.digest,
        "provider_output_digest": value.provider_output_digest,
        "profile_id": value.profile_id,
        "adapter_id": value.adapter_id,
        "model_id": value.model_id,
        "input_tokens": value.input_tokens,
        "output_tokens": value.output_tokens,
        "latency_ms": value.latency_ms,
        "cost_microusd": value.cost_microusd,
        "completed_at": value.completed_at.isoformat(),
    }


def alpha_admitted_review_payload(value: AlphaAdmittedReview) -> dict[str, JsonInput]:
    if not isinstance(value, AlphaAdmittedReview):
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.ADMISSION_REJECTED)
    return {
        "schema_version": value.schema_version,
        "context_digest": value.context_digest,
        "acceptance_digest": value.acceptance_digest,
        "findings": [_finding_payload(finding) for finding in value.findings],
        "summary": value.summary,
    }


def alpha_admitted_review_from_mapping(value: Mapping[str, object]) -> AlphaAdmittedReview:
    raw = _mapping(value)
    if (
        set(raw)
        != {
            "schema_version",
            "context_digest",
            "acceptance_digest",
            "findings",
            "summary",
        }
        or raw.get("schema_version") != ALPHA_ADMITTED_REVIEW_SCHEMA
    ):
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.ADMISSION_REJECTED)
    findings = tuple(
        _finding_from_mapping(item)
        for item in _sequence(raw.get("findings"), maximum=_MAX_FINDINGS)
    )
    context_digest = raw.get("context_digest")
    acceptance_digest = raw.get("acceptance_digest")
    summary = raw.get("summary")
    if not all(isinstance(item, str) for item in (context_digest, acceptance_digest, summary)):
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.ADMISSION_REJECTED)
    return AlphaAdmittedReview(
        context_digest=cast("str", context_digest),
        acceptance_digest=cast("str", acceptance_digest),
        findings=findings,
        summary=cast("str", summary),
    )


def alpha_review_provider_result_from_mapping(
    value: Mapping[str, object],
    *,
    proposal: AlphaReviewProposal,
) -> AlphaReviewProviderResult:
    raw = _mapping(value)
    if (
        set(raw)
        != {
            "schema_version",
            "proposal_digest",
            "provider_output_digest",
            "profile_id",
            "adapter_id",
            "model_id",
            "input_tokens",
            "output_tokens",
            "latency_ms",
            "cost_microusd",
            "completed_at",
        }
        or raw.get("schema_version") != ALPHA_REVIEW_PROVIDER_RESULT_SCHEMA
    ):
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL)
    if (
        not isinstance(proposal, AlphaReviewProposal)
        or raw.get("proposal_digest") != proposal.digest
    ):
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL)
    completed_at = raw.get("completed_at")
    try:
        completed = datetime.fromisoformat(cast("str", completed_at))
    except TypeError, ValueError:
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL) from None
    text_values = tuple(
        raw.get(key) for key in ("provider_output_digest", "profile_id", "adapter_id", "model_id")
    )
    integer_values = tuple(
        raw.get(key) for key in ("input_tokens", "output_tokens", "latency_ms", "cost_microusd")
    )
    if not all(isinstance(item, str) for item in text_values) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in integer_values
    ):
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL)
    return AlphaReviewProviderResult(
        proposal=proposal,
        provider_output_digest=cast("str", text_values[0]),
        profile_id=cast("str", text_values[1]),
        adapter_id=cast("str", text_values[2]),
        model_id=cast("str", text_values[3]),
        input_tokens=cast("int", integer_values[0]),
        output_tokens=cast("int", integer_values[1]),
        latency_ms=cast("int", integer_values[2]),
        cost_microusd=cast("int", integer_values[3]),
        completed_at=completed,
    )


def alpha_review_proposal_from_mapping(value: Mapping[str, object]) -> AlphaReviewProposal:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "context_digest",
        "findings",
        "summary",
    }:
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL)
    if value.get("schema_version") != ALPHA_REVIEW_PROPOSAL_SCHEMA:
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL)
    raw_findings = _sequence(value.get("findings"), maximum=_MAX_FINDINGS)
    findings = tuple(_finding_from_mapping(item) for item in raw_findings)
    context_digest = value.get("context_digest")
    summary = value.get("summary")
    if not isinstance(context_digest, str) or not isinstance(summary, str):
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL)
    return AlphaReviewProposal(
        context_digest=context_digest,
        findings=findings,
        summary=summary,
    )


def _finding_payload(finding: AlphaProposedReviewFinding) -> dict[str, JsonInput]:
    return {
        "finding_id": finding.finding_id,
        "category": finding.category.value,
        "severity": finding.severity.value,
        "claim": finding.claim,
        "impact": finding.impact,
        "recommendation": finding.recommendation,
        "citations": [
            {
                "evidence_id": citation.evidence_id,
                "start_line": citation.start_line,
                "end_line": citation.end_line,
            }
            for citation in finding.citations
        ],
    }


def _finding_from_mapping(value: object) -> AlphaProposedReviewFinding:
    raw = _mapping(value)
    if set(raw) != {
        "finding_id",
        "category",
        "severity",
        "claim",
        "impact",
        "recommendation",
        "citations",
    }:
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL)
    try:
        category = AlphaReviewFindingCategory(raw.get("category"))
        severity = AlphaReviewSeverity(raw.get("severity"))
    except TypeError, ValueError:
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL) from None
    raw_citations = _sequence(raw.get("citations"), maximum=_MAX_CITATIONS)
    citations = tuple(_citation_from_mapping(item) for item in raw_citations)
    finding_id = raw.get("finding_id")
    claim = raw.get("claim")
    impact = raw.get("impact")
    recommendation = raw.get("recommendation")
    if not all(isinstance(item, str) for item in (finding_id, claim, impact, recommendation)):
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL)
    return AlphaProposedReviewFinding(
        finding_id=cast("str", finding_id),
        category=category,
        severity=severity,
        claim=cast("str", claim),
        impact=cast("str", impact),
        recommendation=cast("str", recommendation),
        citations=citations,
    )


def _citation_from_mapping(value: object) -> AlphaReviewCitation:
    raw = _mapping(value)
    if set(raw) != {"evidence_id", "start_line", "end_line"}:
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL)
    evidence_id = raw.get("evidence_id")
    start_line = raw.get("start_line")
    end_line = raw.get("end_line")
    if (
        not isinstance(evidence_id, str)
        or isinstance(start_line, bool)
        or not isinstance(start_line, int)
        or isinstance(end_line, bool)
        or not isinstance(end_line, int)
    ):
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL)
    return AlphaReviewCitation(evidence_id, start_line, end_line)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL)
    return cast("Mapping[str, object]", value)


def _sequence(value: object, *, maximum: int) -> Sequence[object]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, str | bytes | bytearray)
        or len(value) > maximum
    ):
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_PROPOSAL)
    return cast("Sequence[object]", value)


def _bounded_text(value: object, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and "\x00" not in value
        and len(value.encode("utf-8")) <= maximum
    )


def _unique_identifiers(value: object, maximum: int) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) <= maximum
        and len(value) == len(set(value))
        and all(isinstance(item, str) and _IDENTIFIER.fullmatch(item) for item in value)
    )


def _repository_scope_path(value: object) -> str:
    return _repository_path(value, allow_root=True)


def _repository_evidence_path(value: object) -> str:
    return _repository_path(value, allow_root=False)


def _repository_path(value: object, *, allow_root: bool) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_PATH_CHARS
        or "\x00" in value
        or "\\" in value
    ):
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)
    if value == ".":
        if allow_root:
            return value
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or ".git" in path.parts
    ):
        raise AlphaReviewContractError(AlphaReviewContractFailureCode.INVALID_CONTEXT)
    return value


def _path_allowed(path: str, allowed_paths: tuple[str, ...]) -> bool:
    candidate = PurePosixPath(path)
    return any(
        allowed == "."
        or candidate == PurePosixPath(allowed)
        or PurePosixPath(allowed) in candidate.parents
        for allowed in allowed_paths
    )


def _has_dependency_cycle(nodes: tuple[AlphaReviewPlanNode, ...]) -> bool:
    remaining = {node.node_id: set(node.depends_on) for node in nodes}
    while remaining:
        ready = {node_id for node_id, dependencies in remaining.items() if not dependencies}
        if not ready:
            return True
        for node_id in ready:
            del remaining[node_id]
        for dependencies in remaining.values():
            dependencies.difference_update(ready)
    return False


ALPHA_REVIEW_PROPOSAL_OUTPUT_SCHEMA: Mapping[str, JsonValue] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ("schema_version", "context_digest", "findings", "summary"),
    "properties": {
        "schema_version": {"type": "string", "const": ALPHA_REVIEW_PROPOSAL_SCHEMA},
        "context_digest": {"type": "string", "minLength": 71, "maxLength": 71},
        "findings": {
            "type": "array",
            "maxItems": _MAX_FINDINGS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": (
                    "finding_id",
                    "category",
                    "severity",
                    "claim",
                    "impact",
                    "recommendation",
                    "citations",
                ),
                "properties": {
                    "finding_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "category": {
                        "type": "string",
                        "enum": tuple(item.value for item in AlphaReviewFindingCategory),
                    },
                    "severity": {
                        "type": "string",
                        "enum": tuple(item.value for item in AlphaReviewSeverity),
                    },
                    "claim": {"type": "string", "minLength": 1, "maxLength": _MAX_TEXT_BYTES},
                    "impact": {"type": "string", "minLength": 1, "maxLength": _MAX_TEXT_BYTES},
                    "recommendation": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _MAX_TEXT_BYTES,
                    },
                    "citations": {
                        "type": "array",
                        "maxItems": _MAX_CITATIONS,
                        "uniqueItems": True,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ("evidence_id", "start_line", "end_line"),
                            "properties": {
                                "evidence_id": {
                                    "type": "string",
                                    "minLength": 71,
                                    "maxLength": 71,
                                },
                                "start_line": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": _MAX_LINE,
                                },
                                "end_line": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": _MAX_LINE,
                                },
                            },
                        },
                    },
                },
            },
        },
        "summary": {"type": "string", "minLength": 1, "maxLength": _MAX_TEXT_BYTES},
    },
}


__all__ = [
    "ALPHA_ADMITTED_REVIEW_SCHEMA",
    "ALPHA_REVIEW_ACCEPTANCE_SCHEMA",
    "ALPHA_REVIEW_CONTEXT_SCHEMA",
    "ALPHA_REVIEW_PROPOSAL_OUTPUT_SCHEMA",
    "ALPHA_REVIEW_PROPOSAL_SCHEMA",
    "ALPHA_REVIEW_PROVIDER_RESULT_SCHEMA",
    "AlphaAdmittedReview",
    "AlphaProposedReviewFinding",
    "AlphaReviewAcceptance",
    "AlphaReviewCheck",
    "AlphaReviewCitation",
    "AlphaReviewContext",
    "AlphaReviewContractError",
    "AlphaReviewContractFailureCode",
    "AlphaReviewEvidence",
    "AlphaReviewEvidenceKind",
    "AlphaReviewFindingCategory",
    "AlphaReviewPlanNode",
    "AlphaReviewProposal",
    "AlphaReviewProviderCall",
    "AlphaReviewProviderResult",
    "AlphaReviewSeverity",
    "admit_alpha_review",
    "alpha_admitted_review_from_mapping",
    "alpha_admitted_review_payload",
    "alpha_review_acceptance_payload",
    "alpha_review_context_payload",
    "alpha_review_proposal_from_mapping",
    "alpha_review_proposal_payload",
    "alpha_review_provider_result_from_mapping",
    "alpha_review_provider_result_payload",
]
