"""Deterministic intent-to-evidence verification for alpha review artifacts."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from blackcell.kernel import JsonInput
from blackcell.kernel._json import canonical_json_bytes, json_digest
from blackcell.orchestration.alpha_review import (
    AlphaAdmittedReview,
    AlphaProposedReviewFinding,
    AlphaReviewCheck,
    AlphaReviewContext,
    AlphaReviewEvidence,
    AlphaReviewEvidenceKind,
    AlphaReviewFindingCategory,
    AlphaReviewPlanNode,
)

ALPHA_VERIFICATION_REPORT_SCHEMA = "alpha-verification-report/v1"
ALPHA_VERIFICATION_MATRIX_SCHEMA = "alpha-verification-matrix/v1"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_MATRIX_ROWS = 4_290
_MAX_REPORT_BYTES = 4 * 1024 * 1024


class AlphaVerificationStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class AlphaVerificationCriterionKind(StrEnum):
    OBJECTIVE = "objective"
    CONSTRAINT = "constraint"
    NODE = "node"
    WRITE_SCOPE = "write-scope"
    CHECK = "check"
    REVIEW_POLICY = "review-policy"


class AlphaVerificationReasonCode(StrEnum):
    EVIDENCE_COMPLETE = "evidence-complete"
    REVIEW_CLEAR = "review-clear"
    UNRESOLVED_REVIEW_FINDING = "unresolved-review-finding"
    NODE_OUTCOME_MISSING = "node-outcome-missing"
    EVIDENCE_AMBIGUOUS = "evidence-ambiguous"
    CHECK_EVIDENCE_MISSING = "check-evidence-missing"
    EVIDENCE_IDENTITY_MISMATCH = "evidence-identity-mismatch"
    CHECK_FAILED = "check-failed"
    CHANGE_EVIDENCE_MISSING = "change-evidence-missing"


class AlphaVerificationFailureCode(StrEnum):
    INVALID_INPUT = "invalid-alpha-verification-input"
    BINDING_MISMATCH = "alpha-verification-binding-mismatch"
    CITATION_MISMATCH = "alpha-verification-citation-mismatch"


class AlphaVerificationError(ValueError):
    """Content-free rejection at the deterministic verifier boundary."""

    def __init__(self, code: AlphaVerificationFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class AlphaVerificationMatrixRow:
    criterion_id: str
    kind: AlphaVerificationCriterionKind
    claim_digest: str
    status: AlphaVerificationStatus
    reason_codes: tuple[AlphaVerificationReasonCode, ...]
    evidence_ids: tuple[str, ...]
    finding_ids: tuple[str, ...]
    node_id: str | None = None
    check_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.criterion_id, str)
            or _IDENTIFIER.fullmatch(self.criterion_id) is None
            or not isinstance(self.kind, AlphaVerificationCriterionKind)
            or not isinstance(self.claim_digest, str)
            or _DIGEST.fullmatch(self.claim_digest) is None
            or not isinstance(self.status, AlphaVerificationStatus)
            or not isinstance(self.reason_codes, tuple)
            or not self.reason_codes
            or not all(isinstance(item, AlphaVerificationReasonCode) for item in self.reason_codes)
            or not isinstance(self.evidence_ids, tuple)
            or not isinstance(self.finding_ids, tuple)
            or any(_DIGEST.fullmatch(item) is None for item in self.evidence_ids)
            or any(_IDENTIFIER.fullmatch(item) is None for item in self.finding_ids)
            or (
                self.node_id is not None
                and (
                    not isinstance(self.node_id, str) or _IDENTIFIER.fullmatch(self.node_id) is None
                )
            )
            or (
                self.check_id is not None
                and (
                    not isinstance(self.check_id, str)
                    or _IDENTIFIER.fullmatch(self.check_id) is None
                )
            )
        ):
            raise AlphaVerificationError(AlphaVerificationFailureCode.INVALID_INPUT)
        node_kind = self.kind in {
            AlphaVerificationCriterionKind.NODE,
            AlphaVerificationCriterionKind.WRITE_SCOPE,
            AlphaVerificationCriterionKind.CHECK,
        }
        if node_kind != (self.node_id is not None) or (
            self.kind is AlphaVerificationCriterionKind.CHECK
        ) != (self.check_id is not None):
            raise AlphaVerificationError(AlphaVerificationFailureCode.INVALID_INPUT)
        reasons = tuple(sorted(set(self.reason_codes), key=lambda item: item.value))
        evidence = tuple(sorted(set(self.evidence_ids)))
        findings = tuple(sorted(set(self.finding_ids)))
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "evidence_ids", evidence)
        object.__setattr__(self, "finding_ids", findings)
        unresolved = AlphaVerificationReasonCode.UNRESOLVED_REVIEW_FINDING in reasons
        if (
            unresolved != bool(findings)
            or (AlphaVerificationReasonCode.EVIDENCE_COMPLETE in reasons and len(reasons) != 1)
            or (
                AlphaVerificationReasonCode.REVIEW_CLEAR in reasons
                and (
                    len(reasons) != 1
                    or self.kind is not AlphaVerificationCriterionKind.REVIEW_POLICY
                    or findings
                )
            )
            or (
                self.kind is AlphaVerificationCriterionKind.REVIEW_POLICY
                and not unresolved
                and AlphaVerificationReasonCode.REVIEW_CLEAR not in reasons
            )
        ):
            raise AlphaVerificationError(AlphaVerificationFailureCode.INVALID_INPUT)
        expected = _status_for_reasons(reasons)
        if self.status is not expected:
            raise AlphaVerificationError(AlphaVerificationFailureCode.INVALID_INPUT)


@dataclass(frozen=True, slots=True)
class AlphaVerificationReport:
    run_id: str
    context_digest: str
    acceptance_digest: str
    state_digest: str
    artifact_evidence_digest: str
    admitted_review_digest: str
    status: AlphaVerificationStatus
    matrix: tuple[AlphaVerificationMatrixRow, ...]
    schema_version: str = ALPHA_VERIFICATION_REPORT_SCHEMA

    def __post_init__(self) -> None:
        digests = (
            self.context_digest,
            self.acceptance_digest,
            self.state_digest,
            self.artifact_evidence_digest,
            self.admitted_review_digest,
        )
        if (
            self.schema_version != ALPHA_VERIFICATION_REPORT_SCHEMA
            or not isinstance(self.run_id, str)
            or _IDENTIFIER.fullmatch(self.run_id) is None
            or any(not isinstance(item, str) or _DIGEST.fullmatch(item) is None for item in digests)
            or not isinstance(self.status, AlphaVerificationStatus)
            or not isinstance(self.matrix, tuple)
            or not 1 <= len(self.matrix) <= _MAX_MATRIX_ROWS
            or not all(isinstance(item, AlphaVerificationMatrixRow) for item in self.matrix)
            or len({item.criterion_id for item in self.matrix}) != len(self.matrix)
            or self.status is not _aggregate_status(self.matrix)
        ):
            raise AlphaVerificationError(AlphaVerificationFailureCode.INVALID_INPUT)
        if len(canonical_json_bytes(alpha_verification_report_payload(self))) > _MAX_REPORT_BYTES:
            raise AlphaVerificationError(AlphaVerificationFailureCode.INVALID_INPUT)

    @property
    def digest(self) -> str:
        return json_digest(alpha_verification_report_payload(self))

    @property
    def matrix_digest(self) -> str:
        return json_digest(alpha_verification_matrix_payload(self))


def verify_alpha_review(
    context: AlphaReviewContext,
    admitted: AlphaAdmittedReview,
) -> AlphaVerificationReport:
    """Build a host-owned verdict without reinterpreting acceptance or reviewer prose."""

    if not isinstance(context, AlphaReviewContext) or not isinstance(admitted, AlphaAdmittedReview):
        raise AlphaVerificationError(AlphaVerificationFailureCode.INVALID_INPUT)
    if (
        admitted.context_digest != context.digest
        or admitted.acceptance_digest != context.acceptance.digest
    ):
        raise AlphaVerificationError(AlphaVerificationFailureCode.BINDING_MISMATCH)
    evidence_by_id = {item.evidence_id: item for item in context.evidence}
    _validate_citations(admitted.findings, evidence_by_id)
    index = _EvidenceIndex(context.evidence)
    finding_index = _FindingIndex(admitted.findings, evidence_by_id)

    node_rows: list[AlphaVerificationMatrixRow] = []
    supporting_rows: list[AlphaVerificationMatrixRow] = []
    for node_number, node in enumerate(context.acceptance.nodes, start=1):
        scope_row = (
            _scope_row(node_number, node, index, finding_index)
            if "repository-write" in node.effects
            else None
        )
        check_rows = tuple(
            _check_row(node_number, check_number, node, check, index, finding_index)
            for check_number, check in enumerate(node.checks, start=1)
        )
        node_row = _node_row(
            node_number,
            node,
            index,
            finding_index,
            scope_row=scope_row,
            check_rows=check_rows,
        )
        node_rows.append(node_row)
        if scope_row is not None:
            supporting_rows.append(scope_row)
        supporting_rows.extend(check_rows)

    objective = _intent_row(
        "objective",
        AlphaVerificationCriterionKind.OBJECTIVE,
        {
            "kind": "objective",
            "acceptance_digest": context.acceptance.digest,
            "objective": context.acceptance.objective,
        },
        tuple(node_rows),
        admitted.findings,
    )
    constraints = tuple(
        _intent_row(
            f"constraint-{index_number:03d}",
            AlphaVerificationCriterionKind.CONSTRAINT,
            {
                "kind": "constraint",
                "acceptance_digest": context.acceptance.digest,
                "index": index_number,
                "constraint": constraint,
            },
            tuple(node_rows),
            admitted.findings,
        )
        for index_number, constraint in enumerate(context.acceptance.constraints, start=1)
    )
    review_row = _review_row(admitted, finding_index)
    matrix = (
        objective,
        *constraints,
        *node_rows,
        *supporting_rows,
        review_row,
    )
    return AlphaVerificationReport(
        run_id=context.acceptance.run_id,
        context_digest=context.digest,
        acceptance_digest=context.acceptance.digest,
        state_digest=context.state_digest,
        artifact_evidence_digest=context.artifact_evidence_digest,
        admitted_review_digest=admitted.digest,
        status=_aggregate_status(matrix),
        matrix=matrix,
    )


def alpha_verification_report_payload(value: AlphaVerificationReport) -> dict[str, JsonInput]:
    if not isinstance(value, AlphaVerificationReport):
        raise AlphaVerificationError(AlphaVerificationFailureCode.INVALID_INPUT)
    return {
        "schema_version": value.schema_version,
        "run_id": value.run_id,
        "context_digest": value.context_digest,
        "acceptance_digest": value.acceptance_digest,
        "state_digest": value.state_digest,
        "artifact_evidence_digest": value.artifact_evidence_digest,
        "admitted_review_digest": value.admitted_review_digest,
        "status": value.status.value,
        "matrix": [_matrix_row_payload(row) for row in value.matrix],
    }


def alpha_verification_matrix_payload(value: AlphaVerificationReport) -> dict[str, JsonInput]:
    if not isinstance(value, AlphaVerificationReport):
        raise AlphaVerificationError(AlphaVerificationFailureCode.INVALID_INPUT)
    return {
        "schema_version": ALPHA_VERIFICATION_MATRIX_SCHEMA,
        "rows": [_matrix_row_payload(row) for row in value.matrix],
    }


def alpha_verification_report_from_mapping(
    value: Mapping[str, object],
) -> AlphaVerificationReport:
    raw = _mapping(value)
    if (
        set(raw)
        != {
            "schema_version",
            "run_id",
            "context_digest",
            "acceptance_digest",
            "state_digest",
            "artifact_evidence_digest",
            "admitted_review_digest",
            "status",
            "matrix",
        }
        or raw.get("schema_version") != ALPHA_VERIFICATION_REPORT_SCHEMA
    ):
        raise AlphaVerificationError(AlphaVerificationFailureCode.INVALID_INPUT)
    try:
        status = AlphaVerificationStatus(raw.get("status"))
    except TypeError, ValueError:
        raise AlphaVerificationError(AlphaVerificationFailureCode.INVALID_INPUT) from None
    matrix = tuple(
        _matrix_row_from_mapping(item)
        for item in _sequence(raw.get("matrix"), minimum=1, maximum=_MAX_MATRIX_ROWS)
    )
    return AlphaVerificationReport(
        run_id=_text(raw.get("run_id")),
        context_digest=_text(raw.get("context_digest")),
        acceptance_digest=_text(raw.get("acceptance_digest")),
        state_digest=_text(raw.get("state_digest")),
        artifact_evidence_digest=_text(raw.get("artifact_evidence_digest")),
        admitted_review_digest=_text(raw.get("admitted_review_digest")),
        status=status,
        matrix=matrix,
    )


def _matrix_row_payload(row: AlphaVerificationMatrixRow) -> dict[str, JsonInput]:
    return {
        "criterion_id": row.criterion_id,
        "kind": row.kind.value,
        "claim_digest": row.claim_digest,
        "status": row.status.value,
        "reason_codes": [item.value for item in row.reason_codes],
        "evidence_ids": list(row.evidence_ids),
        "finding_ids": list(row.finding_ids),
        "node_id": row.node_id,
        "check_id": row.check_id,
    }


def _matrix_row_from_mapping(value: object) -> AlphaVerificationMatrixRow:
    raw = _mapping(value)
    if set(raw) != {
        "criterion_id",
        "kind",
        "claim_digest",
        "status",
        "reason_codes",
        "evidence_ids",
        "finding_ids",
        "node_id",
        "check_id",
    }:
        raise AlphaVerificationError(AlphaVerificationFailureCode.INVALID_INPUT)
    try:
        kind = AlphaVerificationCriterionKind(raw.get("kind"))
        status = AlphaVerificationStatus(raw.get("status"))
        reasons = tuple(
            AlphaVerificationReasonCode(item)
            for item in _sequence(
                raw.get("reason_codes"),
                minimum=1,
                maximum=len(AlphaVerificationReasonCode),
            )
        )
    except TypeError, ValueError:
        raise AlphaVerificationError(AlphaVerificationFailureCode.INVALID_INPUT) from None
    return AlphaVerificationMatrixRow(
        criterion_id=_text(raw.get("criterion_id")),
        kind=kind,
        claim_digest=_text(raw.get("claim_digest")),
        status=status,
        reason_codes=reasons,
        evidence_ids=tuple(_text(item) for item in _sequence(raw.get("evidence_ids"), maximum=128)),
        finding_ids=tuple(_text(item) for item in _sequence(raw.get("finding_ids"), maximum=64)),
        node_id=_optional_text(raw.get("node_id")),
        check_id=_optional_text(raw.get("check_id")),
    )


@dataclass(frozen=True, slots=True)
class _EvidenceIndex:
    evidence: tuple[AlphaReviewEvidence, ...]

    def select(
        self,
        kind: AlphaReviewEvidenceKind,
        *,
        node_id: str,
        check_id: str | None = None,
    ) -> tuple[AlphaReviewEvidence, ...]:
        return tuple(
            item
            for item in self.evidence
            if item.kind is kind and item.node_id == node_id and item.check_id == check_id
        )


@dataclass(frozen=True, slots=True)
class _FindingIndex:
    findings: tuple[AlphaProposedReviewFinding, ...]
    evidence_by_id: Mapping[str, AlphaReviewEvidence]

    def for_node(self, node_id: str) -> tuple[AlphaProposedReviewFinding, ...]:
        return tuple(
            finding
            for finding in self.findings
            if any(
                self.evidence_by_id[citation.evidence_id].node_id == node_id
                for citation in finding.citations
            )
        )

    def for_check(self, node_id: str, check_id: str) -> tuple[AlphaProposedReviewFinding, ...]:
        return tuple(
            finding
            for finding in self.findings
            if any(
                (evidence := self.evidence_by_id[citation.evidence_id]).node_id == node_id
                and evidence.check_id == check_id
                for citation in finding.citations
            )
        )

    def scope_for_node(self, node_id: str) -> tuple[AlphaProposedReviewFinding, ...]:
        return tuple(
            finding
            for finding in self.for_node(node_id)
            if finding.category is AlphaReviewFindingCategory.SCOPE_DRIFT
        )


def _check_row(
    node_number: int,
    check_number: int,
    node: AlphaReviewPlanNode,
    check: AlphaReviewCheck,
    evidence: _EvidenceIndex,
    findings: _FindingIndex,
) -> AlphaVerificationMatrixRow:
    required = (
        AlphaReviewEvidenceKind.CHECK_COMMAND,
        AlphaReviewEvidenceKind.CHECK_RESULT,
        AlphaReviewEvidenceKind.CHECK_STDOUT,
        AlphaReviewEvidenceKind.CHECK_STDERR,
    )
    selected = tuple(
        item
        for kind in required
        for item in evidence.select(kind, node_id=node.node_id, check_id=check.check_id)
    )
    counts = tuple(
        len(evidence.select(kind, node_id=node.node_id, check_id=check.check_id))
        for kind in required
    )
    matched_findings = findings.for_check(node.node_id, check.check_id)
    reasons: list[AlphaVerificationReasonCode] = []
    if any(count == 0 for count in counts):
        reasons.append(AlphaVerificationReasonCode.CHECK_EVIDENCE_MISSING)
    if any(count > 1 for count in counts):
        reasons.append(AlphaVerificationReasonCode.EVIDENCE_AMBIGUOUS)
    commands = evidence.select(
        AlphaReviewEvidenceKind.CHECK_COMMAND,
        node_id=node.node_id,
        check_id=check.check_id,
    )
    results = evidence.select(
        AlphaReviewEvidenceKind.CHECK_RESULT,
        node_id=node.node_id,
        check_id=check.check_id,
    )
    if (len(commands) == 1 and commands[0].artifact_digest != check.command_digest) or (
        len(results) == 1 and results[0].artifact_digest != check.result_digest
    ):
        reasons.append(AlphaVerificationReasonCode.EVIDENCE_IDENTITY_MISMATCH)
    if not check.passed:
        reasons.append(AlphaVerificationReasonCode.CHECK_FAILED)
    if matched_findings:
        reasons.append(AlphaVerificationReasonCode.UNRESOLVED_REVIEW_FINDING)
    if not reasons:
        reasons.append(AlphaVerificationReasonCode.EVIDENCE_COMPLETE)
    return _row(
        criterion_id=f"check-{node_number:03d}-{check_number:03d}",
        kind=AlphaVerificationCriterionKind.CHECK,
        claim={
            "kind": "check",
            "node_id": node.node_id,
            "check_id": check.check_id,
            "argv": list(check.argv),
            "expected_exit_code": check.expected_exit_code,
            "command_digest": check.command_digest,
            "result_digest": check.result_digest,
        },
        reasons=reasons,
        evidence=selected,
        findings=matched_findings,
        node_id=node.node_id,
        check_id=check.check_id,
    )


def _scope_row(
    node_number: int,
    node: AlphaReviewPlanNode,
    evidence: _EvidenceIndex,
    findings: _FindingIndex,
) -> AlphaVerificationMatrixRow:
    required = (
        AlphaReviewEvidenceKind.SOURCE_BEFORE,
        AlphaReviewEvidenceKind.SOURCE_AFTER,
        AlphaReviewEvidenceKind.EFFECT,
    )
    selected = tuple(
        item for kind in required for item in evidence.select(kind, node_id=node.node_id)
    )
    matched_findings = findings.scope_for_node(node.node_id)
    reasons: list[AlphaVerificationReasonCode] = []
    if any(not evidence.select(kind, node_id=node.node_id) for kind in required):
        reasons.append(AlphaVerificationReasonCode.CHANGE_EVIDENCE_MISSING)
    if matched_findings:
        reasons.append(AlphaVerificationReasonCode.UNRESOLVED_REVIEW_FINDING)
    if not reasons:
        reasons.append(AlphaVerificationReasonCode.EVIDENCE_COMPLETE)
    return _row(
        criterion_id=f"scope-{node_number:03d}",
        kind=AlphaVerificationCriterionKind.WRITE_SCOPE,
        claim={
            "kind": "write-scope",
            "node_id": node.node_id,
            "effects": list(node.effects),
            "allowed_paths": list(node.allowed_paths),
            "max_changed_files": node.max_changed_files,
        },
        reasons=reasons,
        evidence=selected,
        findings=matched_findings,
        node_id=node.node_id,
    )


def _node_row(
    node_number: int,
    node: AlphaReviewPlanNode,
    evidence: _EvidenceIndex,
    findings: _FindingIndex,
    *,
    scope_row: AlphaVerificationMatrixRow | None,
    check_rows: tuple[AlphaVerificationMatrixRow, ...],
) -> AlphaVerificationMatrixRow:
    outcomes = evidence.select(AlphaReviewEvidenceKind.OUTCOME, node_id=node.node_id)
    matched_findings = findings.for_node(node.node_id)
    reasons: list[AlphaVerificationReasonCode] = []
    if not outcomes:
        reasons.append(AlphaVerificationReasonCode.NODE_OUTCOME_MISSING)
    elif len(outcomes) > 1:
        reasons.append(AlphaVerificationReasonCode.EVIDENCE_AMBIGUOUS)
    child_rows = (*(() if scope_row is None else (scope_row,)), *check_rows)
    for row in child_rows:
        reasons.extend(
            reason
            for reason in row.reason_codes
            if reason is not AlphaVerificationReasonCode.EVIDENCE_COMPLETE
        )
    if matched_findings:
        reasons.append(AlphaVerificationReasonCode.UNRESOLVED_REVIEW_FINDING)
    if not reasons:
        reasons.append(AlphaVerificationReasonCode.EVIDENCE_COMPLETE)
    child_evidence = tuple(
        item
        for row in child_rows
        for item in evidence.evidence
        if item.evidence_id in row.evidence_ids
    )
    return _row(
        criterion_id=f"node-{node_number:03d}",
        kind=AlphaVerificationCriterionKind.NODE,
        claim={
            "kind": "node",
            "node_id": node.node_id,
            "objective": node.objective,
            "depends_on": list(node.depends_on),
        },
        reasons=reasons,
        evidence=(*outcomes, *child_evidence),
        findings=matched_findings,
        node_id=node.node_id,
    )


def _intent_row(
    criterion_id: str,
    kind: AlphaVerificationCriterionKind,
    claim: Mapping[str, JsonInput],
    node_rows: tuple[AlphaVerificationMatrixRow, ...],
    findings: tuple[AlphaProposedReviewFinding, ...],
) -> AlphaVerificationMatrixRow:
    reasons = [
        reason
        for row in node_rows
        for reason in row.reason_codes
        if reason is not AlphaVerificationReasonCode.EVIDENCE_COMPLETE
    ]
    if findings:
        reasons.append(AlphaVerificationReasonCode.UNRESOLVED_REVIEW_FINDING)
    if not reasons:
        reasons.append(AlphaVerificationReasonCode.EVIDENCE_COMPLETE)
    return AlphaVerificationMatrixRow(
        criterion_id=criterion_id,
        kind=kind,
        claim_digest=json_digest(claim),
        status=_status_for_reasons(tuple(reasons)),
        reason_codes=tuple(reasons),
        evidence_ids=tuple(item for row in node_rows for item in row.evidence_ids),
        finding_ids=tuple(finding.finding_id for finding in findings),
    )


def _review_row(
    admitted: AlphaAdmittedReview,
    findings: _FindingIndex,
) -> AlphaVerificationMatrixRow:
    reasons = (
        (AlphaVerificationReasonCode.UNRESOLVED_REVIEW_FINDING,)
        if admitted.findings
        else (AlphaVerificationReasonCode.REVIEW_CLEAR,)
    )
    cited_ids = tuple(
        citation.evidence_id for finding in admitted.findings for citation in finding.citations
    )
    return AlphaVerificationMatrixRow(
        criterion_id="review",
        kind=AlphaVerificationCriterionKind.REVIEW_POLICY,
        claim_digest=json_digest(
            {
                "kind": "review-policy",
                "admitted_review_digest": admitted.digest,
                "policy": "unresolved-findings-block",
            }
        ),
        status=_status_for_reasons(reasons),
        reason_codes=reasons,
        evidence_ids=cited_ids,
        finding_ids=tuple(finding.finding_id for finding in findings.findings),
    )


def _row(
    *,
    criterion_id: str,
    kind: AlphaVerificationCriterionKind,
    claim: Mapping[str, JsonInput],
    reasons: list[AlphaVerificationReasonCode],
    evidence: tuple[AlphaReviewEvidence, ...],
    findings: tuple[AlphaProposedReviewFinding, ...],
    node_id: str | None = None,
    check_id: str | None = None,
) -> AlphaVerificationMatrixRow:
    return AlphaVerificationMatrixRow(
        criterion_id=criterion_id,
        kind=kind,
        claim_digest=json_digest(claim),
        status=_status_for_reasons(tuple(reasons)),
        reason_codes=tuple(reasons),
        evidence_ids=tuple(item.evidence_id for item in evidence),
        finding_ids=tuple(item.finding_id for item in findings),
        node_id=node_id,
        check_id=check_id,
    )


def _validate_citations(
    findings: tuple[AlphaProposedReviewFinding, ...],
    evidence_by_id: Mapping[str, AlphaReviewEvidence],
) -> None:
    for finding in findings:
        for citation in finding.citations:
            evidence = evidence_by_id.get(citation.evidence_id)
            if (
                evidence is None
                or citation.start_line < evidence.start_line
                or citation.end_line > evidence.end_line
            ):
                raise AlphaVerificationError(AlphaVerificationFailureCode.CITATION_MISMATCH)


def _status_for_reasons(
    reasons: tuple[AlphaVerificationReasonCode, ...],
) -> AlphaVerificationStatus:
    if any(
        reason
        in {
            AlphaVerificationReasonCode.UNRESOLVED_REVIEW_FINDING,
            AlphaVerificationReasonCode.EVIDENCE_IDENTITY_MISMATCH,
            AlphaVerificationReasonCode.CHECK_FAILED,
        }
        for reason in reasons
    ):
        return AlphaVerificationStatus.FAIL
    if any(
        reason
        in {
            AlphaVerificationReasonCode.NODE_OUTCOME_MISSING,
            AlphaVerificationReasonCode.EVIDENCE_AMBIGUOUS,
            AlphaVerificationReasonCode.CHECK_EVIDENCE_MISSING,
            AlphaVerificationReasonCode.CHANGE_EVIDENCE_MISSING,
        }
        for reason in reasons
    ):
        return AlphaVerificationStatus.INCONCLUSIVE
    return AlphaVerificationStatus.PASS


def _aggregate_status(
    matrix: tuple[AlphaVerificationMatrixRow, ...],
) -> AlphaVerificationStatus:
    if any(row.status is AlphaVerificationStatus.FAIL for row in matrix):
        return AlphaVerificationStatus.FAIL
    if any(row.status is AlphaVerificationStatus.INCONCLUSIVE for row in matrix):
        return AlphaVerificationStatus.INCONCLUSIVE
    return AlphaVerificationStatus.PASS


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise AlphaVerificationError(AlphaVerificationFailureCode.INVALID_INPUT)
    return cast("Mapping[str, object]", value)


def _sequence(value: object, *, minimum: int = 0, maximum: int) -> Sequence[object]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, str | bytes | bytearray)
        or not minimum <= len(value) <= maximum
    ):
        raise AlphaVerificationError(AlphaVerificationFailureCode.INVALID_INPUT)
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AlphaVerificationError(AlphaVerificationFailureCode.INVALID_INPUT)
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


__all__ = [
    "ALPHA_VERIFICATION_MATRIX_SCHEMA",
    "ALPHA_VERIFICATION_REPORT_SCHEMA",
    "AlphaVerificationCriterionKind",
    "AlphaVerificationError",
    "AlphaVerificationFailureCode",
    "AlphaVerificationMatrixRow",
    "AlphaVerificationReasonCode",
    "AlphaVerificationReport",
    "AlphaVerificationStatus",
    "alpha_verification_matrix_payload",
    "alpha_verification_report_from_mapping",
    "alpha_verification_report_payload",
    "verify_alpha_review",
]
