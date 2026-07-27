"""Bounded, live-free verification of execution worker outcome artifacts."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, cast

from blackcell.kernel import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactRef,
    JsonInput,
)
from blackcell.kernel._json import bytes_digest, canonical_json_bytes, json_digest
from blackcell.orchestration.acceptance import (
    ACCEPTANCE_COMMAND_SCHEMA,
    ACCEPTANCE_RESULT_SCHEMA,
    ACCEPTANCE_STREAM_SCHEMA,
    MAX_ACCEPTANCE_STREAM_BYTES,
    AcceptanceCommand,
    AcceptanceResult,
    AcceptanceStream,
)
from blackcell.orchestration.changes import (
    CHANGE_CONTEXT_SCHEMA,
    CHANGE_PROVIDER_RESULT_SCHEMA,
    MAX_CHANGE_CONTEXT_BYTES,
    MAX_CHANGE_PROPOSAL_BYTES,
    MAX_TEXT_CHANGE_RESULT_BYTES,
    ChangeContext,
    ChangeProposal,
    ChangeProviderResult,
    ExecutionEvidenceFile,
    TextOperation,
    change_proposal_from_mapping,
)
from blackcell.orchestration.execution_artifacts import (
    ACCEPTANCE_COMMAND_MEDIA_TYPE,
    ACCEPTANCE_RESULT_MEDIA_TYPE,
    CONTEXT_MEDIA_TYPE,
    EFFECT_MEDIA_TYPE,
    OUTCOME_MEDIA_TYPE,
    PROPOSAL_MEDIA_TYPE,
    PROVIDER_MEDIA_TYPE,
    CheckArtifacts,
    ExecutionArtifactContractError,
    ExecutionArtifactContractFailureCode,
    ExecutionArtifactLink,
    NodeOutcomeManifest,
    node_outcome_from_mapping,
)
from blackcell.orchestration.review import (
    MAX_REVIEW_EVIDENCE_ITEMS,
    ReviewAcceptance,
    ReviewCheck,
    ReviewContext,
    ReviewContractError,
    ReviewEvidence,
    ReviewEvidenceKind,
    ReviewPlanNode,
)

_TEXT_CHANGE_RESULT_SCHEMA = "execution-text-change-result/v1"
_BINARY_MEDIA_TYPE = "application/octet-stream"
_JSON_ENCODING = "utf-8"
_MAX_OUTCOME_BYTES = 4 * 1024 * 1024
_MAX_JSON_ARTIFACT_BYTES = 4 * 1024 * 1024
# Plan admission reserves at most 128 review relationships. One maximum-size stream per
# relationship is a conservative closed bound for every admitted context/proposal/check graph.
_MAX_TOTAL_BYTES = MAX_REVIEW_EVIDENCE_ITEMS * MAX_ACCEPTANCE_STREAM_BYTES
_MAX_ARTIFACT_RELATIONSHIPS = 16_384
_MAX_REVIEW_EXCERPT_BYTES = 32 * 1024
_MAX_REVIEW_EVIDENCE_BYTES = 512 * 1024
_REVIEW_TRUNCATION_MARKER = "\n...[truncated; complete artifact retained by digest]\n"

type ReplayNodeStatus = Literal[
    "pending",
    "claimed",
    "running",
    "succeeded",
    "failed",
    "canceled",
    "reconciliation-required",
]


class ExecutionArtifactReplayStatus(StrEnum):
    NOT_APPLICABLE = "not-applicable"
    VERIFIED = "verified"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"


class ReplayArtifactRole(StrEnum):
    OUTCOME = "outcome"
    CONTEXT = "context"
    PROPOSAL = "proposal"
    PROVIDER = "provider"
    EFFECT = "effect"
    CHECK_COMMAND = "check-command"
    CHECK_RESULT = "check-result"
    CHECK_STDOUT = "check-stdout"
    CHECK_STDERR = "check-stderr"


class ReplayFindingCode(StrEnum):
    ARTIFACT_STORE_UNAVAILABLE = "replay-artifact-store-unavailable"
    OUTCOME_REFERENCE_ABSENT = "replay-outcome-reference-absent"
    ARTIFACT_MISSING = "replay-artifact-missing"
    ARTIFACT_INTEGRITY_FAILED = "replay-artifact-integrity-failed"
    ARTIFACT_METADATA_MISMATCH = "replay-artifact-metadata-mismatch"
    ARTIFACT_READ_UNAVAILABLE = "replay-artifact-read-unavailable"
    ARTIFACT_BUDGET_EXCEEDED = "replay-artifact-budget-exceeded"
    ARTIFACT_JSON_INVALID = "replay-artifact-json-invalid"
    ARTIFACT_NONCANONICAL = "replay-artifact-noncanonical"
    OUTCOME_SCHEMA_UNSUPPORTED = "replay-outcome-schema-unsupported"
    OUTCOME_INVALID = "replay-outcome-invalid"
    ARTIFACT_BINDING_MISMATCH = "replay-artifact-binding-mismatch"


class ReviewEvidenceFailureCode(StrEnum):
    ARTIFACTS_NOT_VERIFIED = "review-artifacts-not-verified"
    DEFINITION_MISMATCH = "review-definition-mismatch"
    EVIDENCE_BUDGET_EXCEEDED = "review-evidence-budget-exceeded"


class ReviewEvidenceError(RuntimeError):
    """Content-free failure to construct complete independent review evidence."""

    def __init__(self, code: ReviewEvidenceFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class ExecutionArtifactReaderPort(Protocol):
    database_path: Path

    def stat(self, digest: str | ArtifactRef) -> ArtifactRef: ...

    def get_bytes(self, digest: str | ArtifactRef, *, verify: bool = True) -> bytes: ...


@dataclass(frozen=True, slots=True)
class ReplayCheckExpectation:
    check_id: str
    argv: tuple[str, ...]
    expected_exit_code: int
    timeout_seconds: int | float


@dataclass(frozen=True, slots=True)
class ReplayNodeExpectation:
    node_id: str
    objective: str
    constraints: tuple[str, ...]
    depends_on: tuple[str, ...]
    repository_write: bool
    effects: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    max_changed_paths: int
    checks: tuple[ReplayCheckExpectation, ...]
    status: ReplayNodeStatus
    attempt: int
    fencing_token: int
    lease_digest: str | None
    worktree_spec_digest: str | None
    base_commit: str | None
    head_commit: str | None
    failure_code: str | None
    result_digest: str | None
    provider_context_digest: str | None


@dataclass(frozen=True, slots=True)
class ReplayArtifactEvidence:
    node_id: str
    role: ReplayArtifactRole
    check_id: str | None
    digest: str
    size_bytes: int
    media_type: str
    encoding: str | None
    verified: bool


@dataclass(frozen=True, slots=True)
class ReplayFinding:
    code: ReplayFindingCode
    node_id: str | None
    role: ReplayArtifactRole | None
    check_id: str | None
    artifact_digest: str | None


@dataclass(frozen=True, slots=True)
class ExecutionArtifactReplayReport:
    status: ExecutionArtifactReplayStatus
    artifacts: tuple[ReplayArtifactEvidence, ...]
    findings: tuple[ReplayFinding, ...]
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class _ReviewExcerptSource:
    prefix: bytes = field(repr=False)
    size_bytes: int
    binary: bool


@dataclass(frozen=True, slots=True)
class _VerifiedCheckMaterial:
    command: AcceptanceCommand
    result_digest: str
    passed: bool
    recorded: CheckArtifacts
    command_bytes: bytes
    result_bytes: bytes
    stdout: _ReviewExcerptSource
    stderr: _ReviewExcerptSource


@dataclass(frozen=True, slots=True)
class _VerifiedNodeMaterial:
    expectation: ReplayNodeExpectation
    manifest: NodeOutcomeManifest
    outcome_link: ExecutionArtifactLink
    outcome_bytes: bytes
    context: ChangeContext | None
    proposal: ChangeProposal | None
    effect: _ReviewExcerptSource | None
    checks: tuple[_VerifiedCheckMaterial, ...]


@dataclass(frozen=True, slots=True)
class _ReplayIssue(Exception):
    status: ExecutionArtifactReplayStatus
    code: ReplayFindingCode
    node_id: str | None
    role: ReplayArtifactRole | None
    check_id: str | None
    artifact_digest: str | None

    def finding(self) -> ReplayFinding:
        return ReplayFinding(
            code=self.code,
            node_id=self.node_id,
            role=self.role,
            check_id=self.check_id,
            artifact_digest=self.artifact_digest,
        )


@dataclass(slots=True)
class _ArtifactSession:
    reader: ExecutionArtifactReaderPort
    artifacts: list[ReplayArtifactEvidence] = field(default_factory=list)
    bytes_by_digest: dict[str, bytes] = field(default_factory=dict)
    counted_digests: set[str] = field(default_factory=set)
    references_by_digest: dict[str, ArtifactRef] = field(default_factory=dict)
    total_bytes: int = 0

    def read_digest(
        self,
        digest: str,
        *,
        node_id: str,
        role: ReplayArtifactRole,
        check_id: str | None,
        media_type: str,
        encoding: str | None,
        maximum_bytes: int,
    ) -> tuple[bytes, ExecutionArtifactLink]:
        reference = self._stat(
            digest,
            node_id=node_id,
            role=role,
            check_id=check_id,
        )
        try:
            link = ExecutionArtifactLink.from_reference(reference)
        except ExecutionArtifactContractError:
            raise _issue(
                ExecutionArtifactReplayStatus.FAILED,
                ReplayFindingCode.ARTIFACT_METADATA_MISMATCH,
                node_id,
                role,
                check_id,
                digest,
            ) from None
        if link.digest != digest:
            raise _issue(
                ExecutionArtifactReplayStatus.FAILED,
                ReplayFindingCode.ARTIFACT_METADATA_MISMATCH,
                node_id,
                role,
                check_id,
                digest,
            )
        return (
            self._read(
                link,
                node_id=node_id,
                role=role,
                check_id=check_id,
                media_type=media_type,
                encoding=encoding,
                maximum_bytes=maximum_bytes,
            ),
            link,
        )

    def read_link(
        self,
        link: ExecutionArtifactLink,
        *,
        node_id: str,
        role: ReplayArtifactRole,
        check_id: str | None,
        media_type: str,
        encoding: str | None,
        maximum_bytes: int,
        retain_bytes: bool = True,
    ) -> bytes:
        return self._read(
            link,
            node_id=node_id,
            role=role,
            check_id=check_id,
            media_type=media_type,
            encoding=encoding,
            maximum_bytes=maximum_bytes,
            retain_bytes=retain_bytes,
        )

    def _read(
        self,
        link: ExecutionArtifactLink,
        *,
        node_id: str,
        role: ReplayArtifactRole,
        check_id: str | None,
        media_type: str,
        encoding: str | None,
        maximum_bytes: int,
        retain_bytes: bool = True,
    ) -> bytes:
        reference = self._stat(
            link.digest,
            node_id=node_id,
            role=role,
            check_id=check_id,
        )
        if (
            reference.size_bytes != link.size_bytes
            or reference.media_type != link.media_type
            or reference.encoding != link.encoding
            or (
                role
                not in {
                    ReplayArtifactRole.CHECK_STDOUT,
                    ReplayArtifactRole.CHECK_STDERR,
                }
                and (link.media_type != media_type or link.encoding != encoding)
            )
        ):
            raise _issue(
                ExecutionArtifactReplayStatus.FAILED,
                ReplayFindingCode.ARTIFACT_METADATA_MISMATCH,
                node_id,
                role,
                check_id,
                link.digest,
            )
        if link.size_bytes > maximum_bytes:
            raise _issue(
                ExecutionArtifactReplayStatus.INCONCLUSIVE,
                ReplayFindingCode.ARTIFACT_BUDGET_EXCEEDED,
                node_id,
                role,
                check_id,
                link.digest,
            )
        data = self.bytes_by_digest.get(link.digest)
        if data is None:
            first_read = link.digest not in self.counted_digests
            if first_read and self.total_bytes + link.size_bytes > _MAX_TOTAL_BYTES:
                raise _issue(
                    ExecutionArtifactReplayStatus.INCONCLUSIVE,
                    ReplayFindingCode.ARTIFACT_BUDGET_EXCEEDED,
                    node_id,
                    role,
                    check_id,
                    link.digest,
                )
            try:
                data = self.reader.get_bytes(reference, verify=True)
            except ArtifactNotFoundError:
                raise _issue(
                    ExecutionArtifactReplayStatus.FAILED,
                    ReplayFindingCode.ARTIFACT_MISSING,
                    node_id,
                    role,
                    check_id,
                    link.digest,
                ) from None
            except ArtifactIntegrityError:
                raise _issue(
                    ExecutionArtifactReplayStatus.FAILED,
                    ReplayFindingCode.ARTIFACT_INTEGRITY_FAILED,
                    node_id,
                    role,
                    check_id,
                    link.digest,
                ) from None
            except Exception:
                raise _issue(
                    ExecutionArtifactReplayStatus.INCONCLUSIVE,
                    ReplayFindingCode.ARTIFACT_READ_UNAVAILABLE,
                    node_id,
                    role,
                    check_id,
                    link.digest,
                ) from None
            if (
                not isinstance(data, bytes)
                or len(data) != link.size_bytes
                or bytes_digest(data) != link.digest
            ):
                raise _issue(
                    ExecutionArtifactReplayStatus.FAILED,
                    ReplayFindingCode.ARTIFACT_INTEGRITY_FAILED,
                    node_id,
                    role,
                    check_id,
                    link.digest,
                )
            if retain_bytes:
                self.bytes_by_digest[link.digest] = data
            if first_read:
                self.counted_digests.add(link.digest)
                self.total_bytes += len(data)
        if len(self.artifacts) >= _MAX_ARTIFACT_RELATIONSHIPS:
            raise _issue(
                ExecutionArtifactReplayStatus.INCONCLUSIVE,
                ReplayFindingCode.ARTIFACT_BUDGET_EXCEEDED,
                node_id,
                role,
                check_id,
                link.digest,
            )
        self.artifacts.append(
            ReplayArtifactEvidence(
                node_id=node_id,
                role=role,
                check_id=check_id,
                digest=link.digest,
                size_bytes=link.size_bytes,
                media_type=link.media_type,
                encoding=link.encoding,
                verified=True,
            )
        )
        return data

    def _stat(
        self,
        digest: str,
        *,
        node_id: str,
        role: ReplayArtifactRole,
        check_id: str | None,
    ) -> ArtifactRef:
        cached = self.references_by_digest.get(digest)
        if cached is not None:
            return cached
        try:
            reference = self.reader.stat(digest)
        except ArtifactNotFoundError:
            raise _issue(
                ExecutionArtifactReplayStatus.FAILED,
                ReplayFindingCode.ARTIFACT_MISSING,
                node_id,
                role,
                check_id,
                digest,
            ) from None
        except ArtifactIntegrityError:
            raise _issue(
                ExecutionArtifactReplayStatus.FAILED,
                ReplayFindingCode.ARTIFACT_INTEGRITY_FAILED,
                node_id,
                role,
                check_id,
                digest,
            ) from None
        except Exception:
            raise _issue(
                ExecutionArtifactReplayStatus.INCONCLUSIVE,
                ReplayFindingCode.ARTIFACT_READ_UNAVAILABLE,
                node_id,
                role,
                check_id,
                digest,
            ) from None
        self.references_by_digest[digest] = reference
        return reference


def verify_run_artifacts(
    reader: ExecutionArtifactReaderPort | None,
    *,
    run_id: str,
    nodes: tuple[ReplayNodeExpectation, ...],
) -> ExecutionArtifactReplayReport:
    """Verify all recorded execution node outcomes without invoking any live boundary."""

    report, _ = _verify_run_materials(reader, run_id=run_id, nodes=nodes)
    return report


def _verify_run_materials(
    reader: ExecutionArtifactReaderPort | None,
    *,
    run_id: str,
    nodes: tuple[ReplayNodeExpectation, ...],
) -> tuple[ExecutionArtifactReplayReport, tuple[_VerifiedNodeMaterial, ...]]:

    required = tuple(
        node
        for node in nodes
        if node.result_digest is not None or node.status in {"succeeded", "failed", "canceled"}
    )
    if not required:
        return _report(ExecutionArtifactReplayStatus.NOT_APPLICABLE, (), ()), ()
    if reader is None:
        finding = ReplayFinding(
            ReplayFindingCode.ARTIFACT_STORE_UNAVAILABLE,
            None,
            None,
            None,
            None,
        )
        return _report(ExecutionArtifactReplayStatus.INCONCLUSIVE, (), (finding,)), ()

    session = _ArtifactSession(reader)
    materials: list[_VerifiedNodeMaterial] = []
    try:
        for node in required:
            if node.result_digest is None:
                raise _issue(
                    ExecutionArtifactReplayStatus.INCONCLUSIVE,
                    ReplayFindingCode.OUTCOME_REFERENCE_ABSENT,
                    node.node_id,
                    ReplayArtifactRole.OUTCOME,
                    None,
                    None,
                )
            materials.append(_verify_node(session, run_id=run_id, expectation=node))
    except _ReplayIssue as issue:
        return _report(issue.status, tuple(session.artifacts), (issue.finding(),)), ()
    return (
        _report(ExecutionArtifactReplayStatus.VERIFIED, tuple(session.artifacts), ()),
        tuple(materials),
    )


def build_review_context_from_artifacts(
    reader: ExecutionArtifactReaderPort | None,
    *,
    run_id: str,
    project_id: str,
    intent_id: str,
    plan_id: str,
    objective: str,
    constraints: tuple[str, ...],
    base_commit: str,
    state_digest: str,
    nodes: tuple[ReplayNodeExpectation, ...],
    require_exact_constraints: bool = True,
) -> ReviewContext:
    """Construct complete review input only from a replay-verified artifact graph."""

    report, materials = _verify_run_materials(reader, run_id=run_id, nodes=nodes)
    if report.status is not ExecutionArtifactReplayStatus.VERIFIED:
        raise ReviewEvidenceError(ReviewEvidenceFailureCode.ARTIFACTS_NOT_VERIFIED)
    if (
        len(materials) != len(nodes)
        or any(node.status != "succeeded" for node in nodes)
        or (require_exact_constraints and any(node.constraints != constraints for node in nodes))
        or any(node.repository_write != ("repository-write" in node.effects) for node in nodes)
        or any(not node.depends_on and node.base_commit != base_commit for node in nodes)
        or any(not material.checks for material in materials)
        or any(not check.passed for material in materials for check in material.checks)
    ):
        raise ReviewEvidenceError(ReviewEvidenceFailureCode.DEFINITION_MISMATCH)

    plan_nodes: list[ReviewPlanNode] = []
    evidence: list[ReviewEvidence] = []
    try:
        evidence_item_count = sum(_review_evidence_item_count(material) for material in materials)
        if not 1 <= evidence_item_count <= MAX_REVIEW_EVIDENCE_ITEMS:
            raise ReviewEvidenceError(ReviewEvidenceFailureCode.EVIDENCE_BUDGET_EXCEEDED)
        maximum_excerpt_bytes = min(
            _MAX_REVIEW_EXCERPT_BYTES,
            _MAX_REVIEW_EVIDENCE_BYTES // evidence_item_count,
        )
        if maximum_excerpt_bytes < 1:
            raise ReviewEvidenceError(ReviewEvidenceFailureCode.EVIDENCE_BUDGET_EXCEEDED)
        for material in materials:
            expectation = material.expectation
            plan_nodes.append(
                ReviewPlanNode(
                    node_id=expectation.node_id,
                    objective=expectation.objective,
                    depends_on=expectation.depends_on,
                    effects=expectation.effects,
                    allowed_paths=expectation.allowed_paths,
                    max_changed_files=expectation.max_changed_paths,
                    checks=tuple(
                        ReviewCheck(
                            check_id=check.command.check_id,
                            argv=check.command.argv,
                            expected_exit_code=check.command.expected_exit_code,
                            command_digest=check.command.digest,
                            result_digest=check.result_digest,
                            passed=check.passed,
                        )
                        for check in material.checks
                    ),
                )
            )
            evidence.extend(
                _review_evidence_for_node(
                    material,
                    maximum_excerpt_bytes=maximum_excerpt_bytes,
                )
            )
        if (
            len(evidence) != evidence_item_count
            or sum(len(item.excerpt.encode("utf-8")) for item in evidence)
            > _MAX_REVIEW_EVIDENCE_BYTES
        ):
            raise ReviewEvidenceError(ReviewEvidenceFailureCode.EVIDENCE_BUDGET_EXCEEDED)
        acceptance = ReviewAcceptance(
            run_id=run_id,
            project_id=project_id,
            intent_id=intent_id,
            plan_id=plan_id,
            objective=objective,
            constraints=constraints,
            base_commit=base_commit,
            nodes=tuple(plan_nodes),
        )
        return ReviewContext(
            acceptance=acceptance,
            state_digest=state_digest,
            artifact_evidence_digest=report.evidence_digest,
            evidence=tuple(evidence),
        )
    except ReviewEvidenceError:
        raise
    except ReviewContractError as error:
        raise ReviewEvidenceError(ReviewEvidenceFailureCode.DEFINITION_MISMATCH) from error


def _review_evidence_item_count(material: _VerifiedNodeMaterial) -> int:
    count = 1 + (4 * len(material.checks))
    if material.expectation.repository_write:
        if material.proposal is None:
            raise ReviewEvidenceError(ReviewEvidenceFailureCode.DEFINITION_MISMATCH)
        count += sum(
            3 if operation.operation is TextOperation.REPLACE else 2
            for operation in material.proposal.operations
        )
    return count


def _review_evidence_for_node(
    material: _VerifiedNodeMaterial,
    *,
    maximum_excerpt_bytes: int,
) -> tuple[ReviewEvidence, ...]:
    expectation = material.expectation
    result: list[ReviewEvidence] = []
    _append_review_evidence(
        result,
        kind=ReviewEvidenceKind.OUTCOME,
        node_id=expectation.node_id,
        artifact_digest=material.outcome_link.digest,
        data=material.outcome_bytes,
        maximum_excerpt_bytes=maximum_excerpt_bytes,
    )

    context = material.context
    proposal = material.proposal
    if expectation.repository_write:
        if (
            context is None
            or proposal is None
            or material.manifest.context_artifact is None
            or material.manifest.proposal_artifact is None
            or material.manifest.effect_artifact is None
            or material.effect is None
        ):
            raise ReviewEvidenceError(ReviewEvidenceFailureCode.DEFINITION_MISMATCH)
        before = {item.path: item.content for item in context.files}
        for operation in proposal.operations:
            if operation.operation in {TextOperation.REPLACE, TextOperation.DELETE}:
                if operation.path not in before:
                    raise ReviewEvidenceError(ReviewEvidenceFailureCode.DEFINITION_MISMATCH)
                _append_review_evidence(
                    result,
                    kind=ReviewEvidenceKind.SOURCE_BEFORE,
                    node_id=expectation.node_id,
                    artifact_digest=material.manifest.context_artifact.digest,
                    data=before[operation.path].encode("utf-8"),
                    path=operation.path,
                    operation=operation.operation,
                    maximum_excerpt_bytes=maximum_excerpt_bytes,
                )
            if operation.operation in {TextOperation.CREATE, TextOperation.REPLACE}:
                if operation.content is None:
                    raise ReviewEvidenceError(ReviewEvidenceFailureCode.DEFINITION_MISMATCH)
                _append_review_evidence(
                    result,
                    kind=ReviewEvidenceKind.SOURCE_AFTER,
                    node_id=expectation.node_id,
                    artifact_digest=material.manifest.proposal_artifact.digest,
                    data=operation.content.encode("utf-8"),
                    path=operation.path,
                    operation=operation.operation,
                    maximum_excerpt_bytes=maximum_excerpt_bytes,
                )
            _append_review_evidence(
                result,
                kind=ReviewEvidenceKind.EFFECT,
                node_id=expectation.node_id,
                artifact_digest=material.manifest.effect_artifact.digest,
                data=material.effect,
                path=operation.path,
                operation=operation.operation,
                maximum_excerpt_bytes=maximum_excerpt_bytes,
            )

    for check in material.checks:
        check_id = check.command.check_id
        _append_review_evidence(
            result,
            kind=ReviewEvidenceKind.CHECK_COMMAND,
            node_id=expectation.node_id,
            artifact_digest=check.recorded.command.digest,
            data=check.command_bytes,
            check_id=check_id,
            maximum_excerpt_bytes=maximum_excerpt_bytes,
        )
        _append_review_evidence(
            result,
            kind=ReviewEvidenceKind.CHECK_RESULT,
            node_id=expectation.node_id,
            artifact_digest=check.recorded.result.digest,
            data=check.result_bytes,
            check_id=check_id,
            maximum_excerpt_bytes=maximum_excerpt_bytes,
        )
        _append_review_evidence(
            result,
            kind=ReviewEvidenceKind.CHECK_STDOUT,
            node_id=expectation.node_id,
            artifact_digest=check.recorded.stdout.digest,
            data=check.stdout,
            check_id=check_id,
            binary_allowed=True,
            maximum_excerpt_bytes=maximum_excerpt_bytes,
        )
        _append_review_evidence(
            result,
            kind=ReviewEvidenceKind.CHECK_STDERR,
            node_id=expectation.node_id,
            artifact_digest=check.recorded.stderr.digest,
            data=check.stderr,
            check_id=check_id,
            binary_allowed=True,
            maximum_excerpt_bytes=maximum_excerpt_bytes,
        )
    return tuple(result)


def _append_review_evidence(
    target: list[ReviewEvidence],
    *,
    kind: ReviewEvidenceKind,
    node_id: str,
    artifact_digest: str,
    data: bytes | _ReviewExcerptSource,
    path: str | None = None,
    check_id: str | None = None,
    operation: TextOperation | None = None,
    binary_allowed: bool = False,
    maximum_excerpt_bytes: int,
) -> None:
    source = (
        data
        if isinstance(data, _ReviewExcerptSource)
        else _review_excerpt_source(data, binary_allowed=binary_allowed)
    )
    excerpt = _bounded_review_excerpt(source, maximum_bytes=maximum_excerpt_bytes)
    target.append(
        ReviewEvidence(
            kind=kind,
            node_id=node_id,
            artifact_digest=artifact_digest,
            excerpt=excerpt,
            start_line=1,
            path=path,
            check_id=check_id,
            operation=operation,
        )
    )


def _review_excerpt_source(
    data: bytes,
    *,
    binary_allowed: bool,
) -> _ReviewExcerptSource:
    binary = False
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        if not binary_allowed:
            raise ReviewEvidenceError(ReviewEvidenceFailureCode.DEFINITION_MISMATCH) from None
        binary = True
    if not binary and "\x00" in decoded:
        if not binary_allowed:
            raise ReviewEvidenceError(ReviewEvidenceFailureCode.DEFINITION_MISMATCH)
        binary = True
    return _ReviewExcerptSource(
        prefix=data[:_MAX_REVIEW_EXCERPT_BYTES],
        size_bytes=len(data),
        binary=binary,
    )


def _bounded_review_excerpt(source: _ReviewExcerptSource, *, maximum_bytes: int) -> str:
    if source.binary:
        return _bounded_base64_excerpt(source, maximum_bytes=maximum_bytes)
    if source.size_bytes <= maximum_bytes:
        return source.prefix.decode("utf-8")
    marker = _REVIEW_TRUNCATION_MARKER.encode("utf-8")
    if maximum_bytes <= len(marker):
        raise ReviewEvidenceError(ReviewEvidenceFailureCode.EVIDENCE_BUDGET_EXCEEDED)
    prefix = source.prefix[: maximum_bytes - len(marker)].decode("utf-8", errors="ignore")
    return prefix + _REVIEW_TRUNCATION_MARKER


def _bounded_base64_excerpt(source: _ReviewExcerptSource, *, maximum_bytes: int) -> str:
    if source.size_bytes == len(source.prefix):
        complete = "base64:" + base64.b64encode(source.prefix).decode("ascii")
        if len(complete.encode("utf-8")) <= maximum_bytes:
            return complete
    label = "base64-prefix:"
    reserved = len(label.encode("utf-8")) + len(_REVIEW_TRUNCATION_MARKER.encode("utf-8"))
    available = maximum_bytes - reserved
    raw_prefix_bytes = min(len(source.prefix), (available // 4) * 3)
    if raw_prefix_bytes < 1:
        raise ReviewEvidenceError(ReviewEvidenceFailureCode.EVIDENCE_BUDGET_EXCEEDED)
    excerpt = (
        label
        + base64.b64encode(source.prefix[:raw_prefix_bytes]).decode("ascii")
        + _REVIEW_TRUNCATION_MARKER
    )
    if len(excerpt.encode("utf-8")) > maximum_bytes:  # pragma: no cover - arithmetic invariant
        raise ReviewEvidenceError(ReviewEvidenceFailureCode.EVIDENCE_BUDGET_EXCEEDED)
    return excerpt


def _verify_node(
    session: _ArtifactSession,
    *,
    run_id: str,
    expectation: ReplayNodeExpectation,
) -> _VerifiedNodeMaterial:
    outcome_digest = cast("str", expectation.result_digest)
    outcome_bytes, outcome_link = session.read_digest(
        outcome_digest,
        node_id=expectation.node_id,
        role=ReplayArtifactRole.OUTCOME,
        check_id=None,
        media_type=OUTCOME_MEDIA_TYPE,
        encoding=_JSON_ENCODING,
        maximum_bytes=_MAX_OUTCOME_BYTES,
    )
    raw_outcome = _canonical_mapping(
        outcome_bytes,
        node_id=expectation.node_id,
        role=ReplayArtifactRole.OUTCOME,
        check_id=None,
        digest=outcome_digest,
    )
    try:
        manifest = node_outcome_from_mapping(raw_outcome)
    except ExecutionArtifactContractError as error:
        code = (
            ReplayFindingCode.OUTCOME_SCHEMA_UNSUPPORTED
            if error.code is ExecutionArtifactContractFailureCode.UNSUPPORTED_OUTCOME_SCHEMA
            else ReplayFindingCode.OUTCOME_INVALID
        )
        status = (
            ExecutionArtifactReplayStatus.INCONCLUSIVE
            if code is ReplayFindingCode.OUTCOME_SCHEMA_UNSUPPORTED
            else ExecutionArtifactReplayStatus.FAILED
        )
        raise _issue(
            status,
            code,
            expectation.node_id,
            ReplayArtifactRole.OUTCOME,
            None,
            outcome_digest,
        ) from None
    if manifest.digest != outcome_digest or not _manifest_matches(
        manifest,
        expectation=expectation,
        run_id=run_id,
    ):
        raise _binding_issue(expectation.node_id, ReplayArtifactRole.OUTCOME, outcome_digest)

    context: ChangeContext | None = None
    proposal: ChangeProposal | None = None
    effect: _ReviewExcerptSource | None = None
    if manifest.context_artifact is not None:
        context = _verify_context(session, expectation, manifest.context_artifact)
    if manifest.proposal_artifact is not None:
        if context is None:
            raise _binding_issue(
                expectation.node_id,
                ReplayArtifactRole.PROPOSAL,
                manifest.proposal_artifact.digest,
            )
        proposal = _verify_proposal(
            session,
            expectation,
            manifest.proposal_artifact,
            context,
        )
    if manifest.provider_artifact is not None:
        if proposal is None:
            raise _binding_issue(
                expectation.node_id,
                ReplayArtifactRole.PROVIDER,
                manifest.provider_artifact.digest,
            )
        _verify_provider(session, expectation, manifest.provider_artifact, proposal)
    if manifest.effect_artifact is not None:
        if context is None or proposal is None:
            raise _binding_issue(
                expectation.node_id,
                ReplayArtifactRole.EFFECT,
                manifest.effect_artifact.digest,
            )
        effect = _verify_effect(
            session,
            expectation,
            manifest.effect_artifact,
            context=context,
            proposal=proposal,
        )
    checks = tuple(
        _verify_check(session, expectation, expected, recorded)
        for expected, recorded in zip(expectation.checks, manifest.checks, strict=False)
    )
    return _VerifiedNodeMaterial(
        expectation=expectation,
        manifest=manifest,
        outcome_link=outcome_link,
        outcome_bytes=outcome_bytes,
        context=context,
        proposal=proposal,
        effect=effect,
        checks=checks,
    )


def _manifest_matches(
    manifest: NodeOutcomeManifest,
    *,
    expectation: ReplayNodeExpectation,
    run_id: str,
) -> bool:
    expected_status = expectation.status
    check_ids = tuple(check.check_id for check in manifest.checks)
    declared_check_ids = tuple(check.check_id for check in expectation.checks)
    if (
        expected_status not in {"succeeded", "failed", "canceled"}
        or manifest.run_id != run_id
        or manifest.node_id != expectation.node_id
        or manifest.attempt != expectation.attempt
        or manifest.fencing_token != expectation.fencing_token
        or manifest.lease_digest != expectation.lease_digest
        or manifest.worktree_spec_digest != expectation.worktree_spec_digest
        or manifest.base_commit != expectation.base_commit
        or manifest.repository_write != expectation.repository_write
        or manifest.status != expected_status
        or manifest.failure_code != expectation.failure_code
        or check_ids != declared_check_ids[: len(check_ids)]
    ):
        return False
    if expected_status == "succeeded":
        if manifest.head_commit != expectation.head_commit:
            return False
    elif manifest.head_commit is not None and manifest.head_commit != expectation.head_commit:
        return False
    if expected_status == "succeeded" and check_ids != declared_check_ids:
        return False
    if expectation.repository_write and expected_status == "succeeded":
        if (
            expectation.provider_context_digest is None
            or manifest.context_artifact is None
            or manifest.context_artifact.digest != expectation.provider_context_digest
        ):
            return False
    elif (
        expectation.provider_context_digest is not None
        and manifest.context_artifact is not None
        and manifest.context_artifact.digest != expectation.provider_context_digest
    ):
        return False
    return True


def _verify_context(
    session: _ArtifactSession,
    expectation: ReplayNodeExpectation,
    link: ExecutionArtifactLink,
) -> ChangeContext:
    data = session.read_link(
        link,
        node_id=expectation.node_id,
        role=ReplayArtifactRole.CONTEXT,
        check_id=None,
        media_type=CONTEXT_MEDIA_TYPE,
        encoding=_JSON_ENCODING,
        maximum_bytes=MAX_CHANGE_CONTEXT_BYTES,
        retain_bytes=False,
    )
    raw = _canonical_mapping(
        data,
        node_id=expectation.node_id,
        role=ReplayArtifactRole.CONTEXT,
        check_id=None,
        digest=link.digest,
    )
    try:
        context = _context_from_mapping(raw)
    except Exception:
        raise _binding_issue(
            expectation.node_id,
            ReplayArtifactRole.CONTEXT,
            link.digest,
        ) from None
    if (
        context.digest != link.digest
        or context.objective != expectation.objective
        or context.constraints != expectation.constraints
        or context.base_commit != expectation.base_commit
        or context.allowed_paths != expectation.allowed_paths
        or context.max_changed_paths != expectation.max_changed_paths
    ):
        raise _binding_issue(
            expectation.node_id,
            ReplayArtifactRole.CONTEXT,
            link.digest,
        )
    return context


def _verify_proposal(
    session: _ArtifactSession,
    expectation: ReplayNodeExpectation,
    link: ExecutionArtifactLink,
    context: ChangeContext,
) -> ChangeProposal:
    data = session.read_link(
        link,
        node_id=expectation.node_id,
        role=ReplayArtifactRole.PROPOSAL,
        check_id=None,
        media_type=PROPOSAL_MEDIA_TYPE,
        encoding=_JSON_ENCODING,
        maximum_bytes=MAX_CHANGE_PROPOSAL_BYTES,
        retain_bytes=False,
    )
    raw = _canonical_mapping(
        data,
        node_id=expectation.node_id,
        role=ReplayArtifactRole.PROPOSAL,
        check_id=None,
        digest=link.digest,
    )
    try:
        proposal = change_proposal_from_mapping(raw)
    except Exception:
        raise _binding_issue(
            expectation.node_id,
            ReplayArtifactRole.PROPOSAL,
            link.digest,
        ) from None
    if proposal.digest != link.digest or proposal.evidence_digest != context.digest:
        raise _binding_issue(
            expectation.node_id,
            ReplayArtifactRole.PROPOSAL,
            link.digest,
        )
    return proposal


def _verify_provider(
    session: _ArtifactSession,
    expectation: ReplayNodeExpectation,
    link: ExecutionArtifactLink,
    proposal: ChangeProposal,
) -> None:
    data = session.read_link(
        link,
        node_id=expectation.node_id,
        role=ReplayArtifactRole.PROVIDER,
        check_id=None,
        media_type=PROVIDER_MEDIA_TYPE,
        encoding=_JSON_ENCODING,
        maximum_bytes=_MAX_JSON_ARTIFACT_BYTES,
        retain_bytes=False,
    )
    raw = _canonical_mapping(
        data,
        node_id=expectation.node_id,
        role=ReplayArtifactRole.PROVIDER,
        check_id=None,
        digest=link.digest,
    )
    try:
        result = _provider_from_mapping(raw, proposal)
    except Exception:
        raise _binding_issue(
            expectation.node_id,
            ReplayArtifactRole.PROVIDER,
            link.digest,
        ) from None
    if json_digest(raw) != link.digest or result.proposal.digest != proposal.digest:
        raise _binding_issue(
            expectation.node_id,
            ReplayArtifactRole.PROVIDER,
            link.digest,
        )


def _verify_effect(
    session: _ArtifactSession,
    expectation: ReplayNodeExpectation,
    link: ExecutionArtifactLink,
    *,
    context: ChangeContext,
    proposal: ChangeProposal,
) -> _ReviewExcerptSource:
    data = session.read_link(
        link,
        node_id=expectation.node_id,
        role=ReplayArtifactRole.EFFECT,
        check_id=None,
        media_type=EFFECT_MEDIA_TYPE,
        encoding=_JSON_ENCODING,
        maximum_bytes=MAX_TEXT_CHANGE_RESULT_BYTES,
        retain_bytes=False,
    )
    raw = _canonical_mapping(
        data,
        node_id=expectation.node_id,
        role=ReplayArtifactRole.EFFECT,
        check_id=None,
        digest=link.digest,
    )
    if not _effect_matches(
        raw,
        expectation=expectation,
        context=context,
        proposal=proposal,
    ):
        raise _binding_issue(
            expectation.node_id,
            ReplayArtifactRole.EFFECT,
            link.digest,
        )
    return _review_excerpt_source(data, binary_allowed=False)


def _verify_check(
    session: _ArtifactSession,
    node: ReplayNodeExpectation,
    expected: ReplayCheckExpectation,
    recorded: CheckArtifacts,
) -> _VerifiedCheckMaterial:
    command_bytes = session.read_link(
        recorded.command,
        node_id=node.node_id,
        role=ReplayArtifactRole.CHECK_COMMAND,
        check_id=expected.check_id,
        media_type=ACCEPTANCE_COMMAND_MEDIA_TYPE,
        encoding=_JSON_ENCODING,
        maximum_bytes=_MAX_JSON_ARTIFACT_BYTES,
    )
    raw_command = _canonical_mapping(
        command_bytes,
        node_id=node.node_id,
        role=ReplayArtifactRole.CHECK_COMMAND,
        check_id=expected.check_id,
        digest=recorded.command.digest,
    )
    try:
        command = _command_from_mapping(raw_command)
    except Exception:
        raise _binding_issue(
            node.node_id,
            ReplayArtifactRole.CHECK_COMMAND,
            recorded.command.digest,
            expected.check_id,
        ) from None
    if (
        command.digest != recorded.command_digest
        or command.check_id != expected.check_id
        or command.argv != expected.argv
        or command.expected_exit_code != expected.expected_exit_code
        or command.timeout_seconds != expected.timeout_seconds
    ):
        raise _binding_issue(
            node.node_id,
            ReplayArtifactRole.CHECK_COMMAND,
            recorded.command.digest,
            expected.check_id,
        )
    result_bytes = session.read_link(
        recorded.result,
        node_id=node.node_id,
        role=ReplayArtifactRole.CHECK_RESULT,
        check_id=expected.check_id,
        media_type=ACCEPTANCE_RESULT_MEDIA_TYPE,
        encoding=_JSON_ENCODING,
        maximum_bytes=_MAX_JSON_ARTIFACT_BYTES,
    )
    raw_result = _canonical_mapping(
        result_bytes,
        node_id=node.node_id,
        role=ReplayArtifactRole.CHECK_RESULT,
        check_id=expected.check_id,
        digest=recorded.result.digest,
    )
    stdout = session.read_link(
        recorded.stdout,
        node_id=node.node_id,
        role=ReplayArtifactRole.CHECK_STDOUT,
        check_id=expected.check_id,
        media_type=_BINARY_MEDIA_TYPE,
        encoding=None,
        maximum_bytes=MAX_ACCEPTANCE_STREAM_BYTES,
        retain_bytes=False,
    )
    stderr = session.read_link(
        recorded.stderr,
        node_id=node.node_id,
        role=ReplayArtifactRole.CHECK_STDERR,
        check_id=expected.check_id,
        media_type=_BINARY_MEDIA_TYPE,
        encoding=None,
        maximum_bytes=MAX_ACCEPTANCE_STREAM_BYTES,
        retain_bytes=False,
    )
    try:
        result = _result_from_mapping(raw_result, stdout=stdout, stderr=stderr)
    except Exception:
        raise _binding_issue(
            node.node_id,
            ReplayArtifactRole.CHECK_RESULT,
            recorded.result.digest,
            expected.check_id,
        ) from None
    if (
        result.digest != recorded.result_digest
        or result.check_id != expected.check_id
        or result.command_digest != command.digest
        or result.worktree_spec_digest != node.worktree_spec_digest
        or result.expected_exit_code != expected.expected_exit_code
        or result.passed != recorded.passed
        or len(stdout) > command.stdout_limit_bytes
        or len(stderr) > command.stderr_limit_bytes
    ):
        raise _binding_issue(
            node.node_id,
            ReplayArtifactRole.CHECK_RESULT,
            recorded.result.digest,
            expected.check_id,
        )
    return _VerifiedCheckMaterial(
        command=command,
        result_digest=result.digest,
        passed=result.passed,
        recorded=recorded,
        command_bytes=command_bytes,
        result_bytes=result_bytes,
        stdout=_review_excerpt_source(stdout, binary_allowed=True),
        stderr=_review_excerpt_source(stderr, binary_allowed=True),
    )


def _context_from_mapping(value: Mapping[str, object]) -> ChangeContext:
    if set(value) != {
        "schema_version",
        "objective",
        "constraints",
        "base_commit",
        "allowed_paths",
        "max_changed_paths",
        "files",
    }:
        raise ValueError
    if value.get("schema_version") != CHANGE_CONTEXT_SCHEMA:
        raise ValueError
    constraints = _string_tuple(value.get("constraints"))
    allowed_paths = _string_tuple(value.get("allowed_paths"))
    raw_files = _sequence(value.get("files"))
    files: list[ExecutionEvidenceFile] = []
    for item in raw_files:
        raw = _mapping(item)
        if set(raw) != {"path", "content", "content_digest"}:
            raise ValueError
        files.append(
            ExecutionEvidenceFile(
                path=_string(raw.get("path")),
                content=_string(raw.get("content"), allow_empty=True),
                content_digest=_string(raw.get("content_digest")),
            )
        )
    return ChangeContext(
        objective=_string(value.get("objective")),
        constraints=constraints,
        base_commit=_string(value.get("base_commit")),
        allowed_paths=allowed_paths,
        max_changed_paths=_integer(value.get("max_changed_paths")),
        files=tuple(files),
        schema_version=cast("str", value.get("schema_version")),
    )


def _provider_from_mapping(
    value: Mapping[str, object],
    proposal: ChangeProposal,
) -> ChangeProviderResult:
    if (
        set(value)
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
        or value.get("schema_version") != CHANGE_PROVIDER_RESULT_SCHEMA
    ):
        raise ValueError
    if value.get("proposal_digest") != proposal.digest:
        raise ValueError
    return ChangeProviderResult(
        proposal=proposal,
        provider_output_digest=_string(value.get("provider_output_digest")),
        profile_id=_string(value.get("profile_id")),
        adapter_id=_string(value.get("adapter_id")),
        model_id=_string(value.get("model_id")),
        input_tokens=_optional_integer(value.get("input_tokens")),
        output_tokens=_optional_integer(value.get("output_tokens")),
        latency_ms=_integer(value.get("latency_ms")),
        cost_microusd=_optional_integer(value.get("cost_microusd")),
        completed_at=datetime.fromisoformat(_string(value.get("completed_at"))),
        schema_version=cast("str", value.get("schema_version")),
    )


def _effect_matches(
    value: Mapping[str, object],
    *,
    expectation: ReplayNodeExpectation,
    context: ChangeContext,
    proposal: ChangeProposal,
) -> bool:
    if set(value) != {
        "schema_version",
        "status",
        "worktree_spec_digest",
        "lease_digest",
        "evidence_digest",
        "proposal_digest",
        "head_commit",
        "effects",
        "changed_paths",
    }:
        return False
    if (
        value.get("schema_version") != _TEXT_CHANGE_RESULT_SCHEMA
        or value.get("status") != "applied"
        or value.get("worktree_spec_digest") != expectation.worktree_spec_digest
        or value.get("lease_digest") != expectation.lease_digest
        or value.get("evidence_digest") != context.digest
        or value.get("proposal_digest") != proposal.digest
        or value.get("head_commit") != expectation.base_commit
    ):
        return False
    try:
        changed_paths = _string_tuple(value.get("changed_paths"))
        raw_effects = _sequence(value.get("effects"))
    except ValueError:
        return False
    expected_paths = tuple(change.path for change in proposal.operations)
    if changed_paths != expected_paths or len(raw_effects) != len(proposal.operations):
        return False
    for raw_value, change in zip(raw_effects, proposal.operations, strict=True):
        try:
            raw = _mapping(raw_value)
        except ValueError:
            return False
        if set(raw) != {"operation", "path", "before_digest", "after_digest"}:
            return False
        before_digest = change.expected_digest
        after_digest = change.content_digest
        if change.operation is TextOperation.CREATE:
            before_digest = None
        elif change.operation is TextOperation.DELETE:
            after_digest = None
        if (
            raw.get("operation") != change.operation.value
            or raw.get("path") != change.path
            or raw.get("before_digest") != before_digest
            or raw.get("after_digest") != after_digest
        ):
            return False
    return True


def _command_from_mapping(value: Mapping[str, object]) -> AcceptanceCommand:
    if (
        set(value)
        != {
            "schema_version",
            "check_id",
            "argv",
            "expected_exit_code",
            "timeout_seconds",
            "stdout_limit_bytes",
            "stderr_limit_bytes",
        }
        or value.get("schema_version") != ACCEPTANCE_COMMAND_SCHEMA
    ):
        raise ValueError
    return AcceptanceCommand(
        check_id=_string(value.get("check_id")),
        argv=_string_tuple(value.get("argv")),
        expected_exit_code=_integer(value.get("expected_exit_code")),
        timeout_seconds=_number(value.get("timeout_seconds")),
        stdout_limit_bytes=_integer(value.get("stdout_limit_bytes")),
        stderr_limit_bytes=_integer(value.get("stderr_limit_bytes")),
        schema_version=cast(
            "Literal['blackcell.acceptance-command/v1']",
            value.get("schema_version"),
        ),
    )


def _result_from_mapping(
    value: Mapping[str, object],
    *,
    stdout: bytes,
    stderr: bytes,
) -> AcceptanceResult:
    if (
        set(value)
        != {
            "schema_version",
            "check_id",
            "command_digest",
            "worktree_spec_digest",
            "isolation_policy_digest",
            "inspection_before_digest",
            "inspection_after_digest",
            "return_code",
            "expected_exit_code",
            "passed",
            "stdout",
            "stderr",
        }
        or value.get("schema_version") != ACCEPTANCE_RESULT_SCHEMA
    ):
        raise ValueError
    stdout_value = _mapping(value.get("stdout"))
    stderr_value = _mapping(value.get("stderr"))
    for raw, data in ((stdout_value, stdout), (stderr_value, stderr)):
        if (
            set(raw) != {"schema_version", "size_bytes", "digest"}
            or raw.get("schema_version") != ACCEPTANCE_STREAM_SCHEMA
            or raw.get("size_bytes") != len(data)
            or raw.get("digest") != bytes_digest(data)
        ):
            raise ValueError
    passed = value.get("passed")
    if not isinstance(passed, bool):
        raise ValueError
    return AcceptanceResult(
        check_id=_string(value.get("check_id")),
        command_digest=_string(value.get("command_digest")),
        worktree_spec_digest=_string(value.get("worktree_spec_digest")),
        isolation_policy_digest=_string(value.get("isolation_policy_digest")),
        inspection_before_digest=_string(value.get("inspection_before_digest")),
        inspection_after_digest=_string(value.get("inspection_after_digest")),
        return_code=_integer(value.get("return_code")),
        expected_exit_code=_integer(value.get("expected_exit_code")),
        passed=passed,
        stdout=AcceptanceStream(stdout),
        stderr=AcceptanceStream(stderr),
        schema_version=cast(
            "Literal['blackcell.acceptance-result/v1']",
            value.get("schema_version"),
        ),
    )


def _canonical_mapping(
    data: bytes,
    *,
    node_id: str,
    role: ReplayArtifactRole,
    check_id: str | None,
    digest: str,
) -> Mapping[str, object]:
    try:
        value = json.loads(data)
    except UnicodeDecodeError, json.JSONDecodeError, RecursionError:
        raise _issue(
            ExecutionArtifactReplayStatus.FAILED,
            ReplayFindingCode.ARTIFACT_JSON_INVALID,
            node_id,
            role,
            check_id,
            digest,
        ) from None
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise _issue(
            ExecutionArtifactReplayStatus.FAILED,
            ReplayFindingCode.ARTIFACT_JSON_INVALID,
            node_id,
            role,
            check_id,
            digest,
        )
    try:
        canonical = canonical_json_bytes(value)
    except TypeError, ValueError, RecursionError:
        raise _issue(
            ExecutionArtifactReplayStatus.FAILED,
            ReplayFindingCode.ARTIFACT_JSON_INVALID,
            node_id,
            role,
            check_id,
            digest,
        ) from None
    if canonical != data:
        raise _issue(
            ExecutionArtifactReplayStatus.FAILED,
            ReplayFindingCode.ARTIFACT_NONCANONICAL,
            node_id,
            role,
            check_id,
            digest,
        )
    return cast("Mapping[str, object]", value)


def _report(
    status: ExecutionArtifactReplayStatus,
    artifacts: tuple[ReplayArtifactEvidence, ...],
    findings: tuple[ReplayFinding, ...],
) -> ExecutionArtifactReplayReport:
    payload: dict[str, JsonInput] = {
        "status": status.value,
        "artifacts": [
            {
                "node_id": artifact.node_id,
                "role": artifact.role.value,
                "check_id": artifact.check_id,
                "digest": artifact.digest,
                "size_bytes": artifact.size_bytes,
                "media_type": artifact.media_type,
                "encoding": artifact.encoding,
                "verified": artifact.verified,
            }
            for artifact in artifacts
        ],
        "findings": [
            {
                "code": finding.code.value,
                "node_id": finding.node_id,
                "role": None if finding.role is None else finding.role.value,
                "check_id": finding.check_id,
                "artifact_digest": finding.artifact_digest,
            }
            for finding in findings
        ],
    }
    return ExecutionArtifactReplayReport(status, artifacts, findings, json_digest(payload))


def _binding_issue(
    node_id: str,
    role: ReplayArtifactRole,
    digest: str,
    check_id: str | None = None,
) -> _ReplayIssue:
    return _issue(
        ExecutionArtifactReplayStatus.FAILED,
        ReplayFindingCode.ARTIFACT_BINDING_MISMATCH,
        node_id,
        role,
        check_id,
        digest,
    )


def _issue(
    status: ExecutionArtifactReplayStatus,
    code: ReplayFindingCode,
    node_id: str | None,
    role: ReplayArtifactRole | None,
    check_id: str | None,
    digest: str | None,
) -> _ReplayIssue:
    return _ReplayIssue(status, code, node_id, role, check_id, digest)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError
    return cast("Mapping[str, object]", value)


def _sequence(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise ValueError
    return cast("Sequence[object]", value)


def _string_tuple(value: object) -> tuple[str, ...]:
    items = _sequence(value)
    if not all(isinstance(item, str) for item in items):
        raise ValueError
    return cast("tuple[str, ...]", tuple(items))


def _string(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError
    return value


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    return _integer(value)


def _number(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError
    return value


__all__ = [
    "ExecutionArtifactReaderPort",
    "ExecutionArtifactReplayReport",
    "ExecutionArtifactReplayStatus",
    "ReplayArtifactEvidence",
    "ReplayArtifactRole",
    "ReplayCheckExpectation",
    "ReplayFinding",
    "ReplayFindingCode",
    "ReplayNodeExpectation",
    "ReplayNodeStatus",
    "ReviewEvidenceError",
    "ReviewEvidenceFailureCode",
    "build_review_context_from_artifacts",
    "verify_run_artifacts",
]
