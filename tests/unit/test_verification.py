from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import cast

import pytest

from blackcell.kernel import JsonInput
from blackcell.kernel._json import json_digest
from blackcell.orchestration.changes import TextOperation
from blackcell.orchestration.review import (
    AdmittedReview,
    EpistemicAssessment,
    EpistemicDimension,
    EpistemicDisposition,
    ProposedReviewFinding,
    ReviewAcceptance,
    ReviewCheck,
    ReviewCitation,
    ReviewContext,
    ReviewEvidence,
    ReviewEvidenceKind,
    ReviewFindingCategory,
    ReviewPlanNode,
    ReviewProposal,
    ReviewSeverity,
    admit_review,
)
from blackcell.orchestration.verification import (
    VerificationCriterionKind,
    VerificationError,
    VerificationFailureCode,
    VerificationReasonCode,
    VerificationStatus,
    verification_matrix_payload,
    verification_report_from_mapping,
    verification_report_payload,
    verify_review,
)


def test_verifier_passes_complete_execution_and_clear_review_with_exact_matrix() -> None:
    context = _context()
    admitted = _admitted(context)

    report = verify_review(context, admitted)
    repeated = verify_review(context, admitted)
    payload = verification_report_payload(report)

    assert report == repeated
    assert report.digest == repeated.digest
    assert report.status is VerificationStatus.PASS
    assert report.run_id == context.acceptance.run_id
    assert report.context_digest == context.digest
    assert report.acceptance_digest == context.acceptance.digest
    assert report.state_digest == context.state_digest
    assert report.artifact_evidence_digest == context.artifact_evidence_digest
    assert report.admitted_review_digest == admitted.digest
    assert tuple(row.kind for row in report.matrix) == (
        VerificationCriterionKind.OBJECTIVE,
        VerificationCriterionKind.CONSTRAINT,
        VerificationCriterionKind.NODE,
        VerificationCriterionKind.WRITE_SCOPE,
        VerificationCriterionKind.CHECK,
        *(VerificationCriterionKind.EPISTEMIC_POLICY for _ in EpistemicDimension),
        VerificationCriterionKind.REVIEW_POLICY,
    )
    assert all(row.status is VerificationStatus.PASS for row in report.matrix)
    check_row = next(row for row in report.matrix if row.kind is VerificationCriterionKind.CHECK)
    assert len(check_row.evidence_ids) == 4
    assert check_row.node_id == "node-1"
    assert check_row.check_id == "unit-check"
    assert payload["status"] == "pass"
    serialized = repr(payload)
    assert "VALUE = 1" not in serialized
    assert "VALUE = 2" not in serialized
    assert "approved" not in serialized


def test_epistemic_policy_rows_fail_concerns_and_preserve_unknowns_as_inconclusive() -> None:
    context = _context()
    admitted = _admitted(context)
    first = admitted.epistemic_assessments[0]
    unknown = replace(first, disposition=EpistemicDisposition.UNKNOWN)
    unknown_review = replace(
        admitted,
        epistemic_assessments=(unknown, *admitted.epistemic_assessments[1:]),
    )

    report = verify_review(context, unknown_review)
    row = next(
        item for item in report.matrix if item.criterion_id == f"epistemic-{first.dimension.value}"
    )

    assert report.status is VerificationStatus.INCONCLUSIVE
    assert row.status is VerificationStatus.INCONCLUSIVE
    assert row.reason_codes == (VerificationReasonCode.EPISTEMIC_UNKNOWN,)
    assert row.evidence_ids == (first.citations[0].evidence_id,)


def test_verifier_fails_unresolved_reward_hacking_findings_without_treating_them_as_truth() -> None:
    context = _context()
    evidence = next(
        item for item in context.evidence if item.kind is ReviewEvidenceKind.SOURCE_AFTER
    )
    citation = ReviewCitation(
        evidence.evidence_id,
        evidence.start_line,
        evidence.end_line,
    )
    findings = tuple(
        ProposedReviewFinding(
            finding_id=f"finding-{index}",
            category=category,
            severity=ReviewSeverity.LOW,
            claim=f"Unresolved {category.value} claim.",
            impact="The bounded result may violate its acceptance contract.",
            recommendation="Resolve the cited finding before verification.",
            citations=(citation,),
        )
        for index, category in enumerate(ReviewFindingCategory, start=1)
    )
    admitted = _admitted(context, findings=findings)

    report = verify_review(context, admitted)

    assert report.status is VerificationStatus.FAIL
    assert report.acceptance_digest == context.acceptance.digest
    assert all(
        finding.finding_id
        in next(row for row in report.matrix if row.criterion_id == "review").finding_ids
        for finding in findings
    )
    assert any(
        VerificationReasonCode.UNRESOLVED_REVIEW_FINDING in row.reason_codes
        for row in report.matrix
        if row.status is VerificationStatus.FAIL
    )
    epistemic = next(
        row for row in report.matrix if VerificationReasonCode.EPISTEMIC_CONCERN in row.reason_codes
    )
    assert epistemic.status is VerificationStatus.FAIL
    assert set(epistemic.finding_ids) == {item.finding_id for item in findings}
    payload = verification_report_payload(report)
    serialized = repr(payload)
    assert "Unresolved correctness claim" not in serialized
    assert "Resolve the cited finding" not in serialized


def test_verifier_distinguishes_failed_checks_from_missing_or_ambiguous_evidence() -> None:
    context = _context()
    node = context.acceptance.nodes[0]
    failed_check = replace(node.checks[0], passed=False)
    failed_node = replace(node, checks=(failed_check,))
    failed_context = replace(
        context,
        acceptance=replace(context.acceptance, nodes=(failed_node,)),
    )
    failed = verify_review(failed_context, _admitted(failed_context))
    failed_row = next(row for row in failed.matrix if row.kind is VerificationCriterionKind.CHECK)
    assert failed.status is VerificationStatus.FAIL
    assert VerificationReasonCode.CHECK_FAILED in failed_row.reason_codes

    missing_context = replace(
        context,
        evidence=tuple(
            item for item in context.evidence if item.kind is not ReviewEvidenceKind.EFFECT
        ),
    )
    missing = verify_review(missing_context, _admitted(missing_context))
    scope = next(row for row in missing.matrix if row.kind is VerificationCriterionKind.WRITE_SCOPE)
    assert missing.status is VerificationStatus.INCONCLUSIVE
    assert VerificationReasonCode.CHANGE_EVIDENCE_MISSING in scope.reason_codes

    duplicate_outcome = ReviewEvidence(
        kind=ReviewEvidenceKind.OUTCOME,
        node_id="node-1",
        artifact_digest=_digest("second-outcome"),
        excerpt='{"status":"succeeded","copy":2}',
        start_line=1,
    )
    ambiguous_context = replace(context, evidence=(*context.evidence, duplicate_outcome))
    ambiguous = verify_review(ambiguous_context, _admitted(ambiguous_context))
    node_row = next(row for row in ambiguous.matrix if row.kind is VerificationCriterionKind.NODE)
    assert ambiguous.status is VerificationStatus.INCONCLUSIVE
    assert VerificationReasonCode.EVIDENCE_AMBIGUOUS in node_row.reason_codes

    command = next(
        item for item in context.evidence if item.kind is ReviewEvidenceKind.CHECK_COMMAND
    )
    mismatched_context = replace(
        context,
        evidence=tuple(
            replace(item, artifact_digest=_digest("wrong-command")) if item is command else item
            for item in context.evidence
        ),
    )
    mismatch = verify_review(mismatched_context, _admitted(mismatched_context))
    mismatch_row = next(
        row for row in mismatch.matrix if row.kind is VerificationCriterionKind.CHECK
    )
    assert mismatch.status is VerificationStatus.FAIL
    assert VerificationReasonCode.EVIDENCE_IDENTITY_MISMATCH in mismatch_row.reason_codes


@pytest.mark.parametrize("operation", (TextOperation.CREATE, TextOperation.DELETE))
def test_verifier_accepts_operation_specific_create_and_delete_evidence(
    operation: TextOperation,
) -> None:
    context = _context_for_operation(operation)

    report = verify_review(context, _admitted(context))
    scope = next(row for row in report.matrix if row.kind is VerificationCriterionKind.WRITE_SCOPE)

    assert report.status is VerificationStatus.PASS
    assert scope.reason_codes == (VerificationReasonCode.EVIDENCE_COMPLETE,)
    source_kinds = {
        item.kind
        for item in context.evidence
        if item.kind in {ReviewEvidenceKind.SOURCE_BEFORE, ReviewEvidenceKind.SOURCE_AFTER}
    }
    assert source_kinds == {
        ReviewEvidenceKind.SOURCE_AFTER
        if operation is TextOperation.CREATE
        else ReviewEvidenceKind.SOURCE_BEFORE
    }


@pytest.mark.parametrize(
    "missing_kind",
    (ReviewEvidenceKind.SOURCE_BEFORE, ReviewEvidenceKind.SOURCE_AFTER),
)
def test_verifier_requires_both_source_sides_for_replace(
    missing_kind: ReviewEvidenceKind,
) -> None:
    context = _context()
    context = replace(
        context,
        evidence=tuple(item for item in context.evidence if item.kind is not missing_kind),
    )

    report = verify_review(context, _admitted(context))
    scope = next(row for row in report.matrix if row.kind is VerificationCriterionKind.WRITE_SCOPE)

    assert report.status is VerificationStatus.INCONCLUSIVE
    assert VerificationReasonCode.CHANGE_EVIDENCE_MISSING in scope.reason_codes


@pytest.mark.parametrize(
    ("operation", "missing_kind"),
    (
        (TextOperation.CREATE, ReviewEvidenceKind.SOURCE_AFTER),
        (TextOperation.DELETE, ReviewEvidenceKind.SOURCE_BEFORE),
    ),
)
def test_verifier_rejects_missing_operation_specific_source_evidence(
    operation: TextOperation,
    missing_kind: ReviewEvidenceKind,
) -> None:
    context = _context_for_operation(operation)
    context = replace(
        context,
        evidence=tuple(item for item in context.evidence if item.kind is not missing_kind),
    )

    report = verify_review(context, _admitted(context))
    scope = next(row for row in report.matrix if row.kind is VerificationCriterionKind.WRITE_SCOPE)

    assert report.status is VerificationStatus.INCONCLUSIVE
    assert VerificationReasonCode.CHANGE_EVIDENCE_MISSING in scope.reason_codes


def test_verifier_keeps_duplicate_and_conflicting_change_evidence_ambiguous() -> None:
    context = _context_for_operation(TextOperation.CREATE)
    source_after = next(
        item for item in context.evidence if item.kind is ReviewEvidenceKind.SOURCE_AFTER
    )
    effect = next(item for item in context.evidence if item.kind is ReviewEvidenceKind.EFFECT)
    variants = (
        replace(
            context,
            evidence=(
                *context.evidence,
                replace(
                    source_after,
                    artifact_digest=_digest("duplicate-source-after"),
                ),
            ),
        ),
        replace(
            context,
            evidence=tuple(
                replace(item, operation=TextOperation.REPLACE) if item is effect else item
                for item in context.evidence
            ),
        ),
    )

    for variant in variants:
        report = verify_review(variant, _admitted(variant))
        scope = next(
            row for row in report.matrix if row.kind is VerificationCriterionKind.WRITE_SCOPE
        )
        assert report.status is VerificationStatus.INCONCLUSIVE
        assert VerificationReasonCode.EVIDENCE_AMBIGUOUS in scope.reason_codes


def test_verifier_rejects_binding_and_citation_drift_content_free() -> None:
    context = _context()
    admitted = _admitted(context)
    with pytest.raises(VerificationError) as binding:
        verify_review(
            context,
            replace(admitted, context_digest=_digest("other-context")),
        )
    assert binding.value.code is VerificationFailureCode.BINDING_MISMATCH
    assert str(binding.value) == "verification-binding-mismatch"

    evidence = context.evidence[0]
    invalid_finding = ProposedReviewFinding(
        finding_id="finding-invalid-range",
        category=ReviewFindingCategory.HIDDEN_SHORTCUT,
        severity=ReviewSeverity.HIGH,
        claim="The citation range exceeds host evidence.",
        impact="The claim is not source-bound.",
        recommendation="Reject the unbound claim.",
        citations=(
            ReviewCitation(
                evidence.evidence_id,
                evidence.start_line,
                evidence.end_line + 1,
            ),
        ),
    )
    invalid = AdmittedReview(
        context_digest=context.digest,
        acceptance_digest=context.acceptance.digest,
        findings=(invalid_finding,),
        summary="Structurally shaped but not context-admitted.",
        epistemic_assessments=_assessments(context, (invalid_finding,)),
    )
    with pytest.raises(VerificationError) as citation:
        verify_review(context, invalid)
    assert citation.value.code is VerificationFailureCode.CITATION_MISMATCH
    assert "citation range" not in str(citation.value)


def test_verification_report_parser_and_matrix_digest_are_closed_and_stable() -> None:
    context = _context()
    report = verify_review(context, _admitted(context))
    payload = verification_report_payload(report)

    assert verification_report_from_mapping(payload) == report
    assert report.matrix_digest == json_digest(verification_matrix_payload(report))

    unknown = deepcopy(payload)
    unknown["approved"] = True
    invalid_row = deepcopy(payload)
    matrix = cast("list[dict[str, JsonInput]]", invalid_row["matrix"])
    matrix[0]["reason_codes"] = []
    for malformed in (unknown, invalid_row):
        with pytest.raises(VerificationError) as caught:
            verification_report_from_mapping(malformed)
        assert caught.value.code is VerificationFailureCode.INVALID_INPUT


def _context() -> ReviewContext:
    command_digest = _digest("command")
    result_digest = _digest("result")
    check = ReviewCheck(
        check_id="unit-check",
        argv=("python", "-m", "pytest", "tests/unit/test_value.py::test_value"),
        expected_exit_code=0,
        command_digest=command_digest,
        result_digest=result_digest,
        passed=True,
    )
    node = ReviewPlanNode(
        node_id="node-1",
        objective="Update the bounded value and run its exact check.",
        depends_on=(),
        effects=("repository-read", "repository-write", "process"),
        allowed_paths=("src",),
        max_changed_files=1,
        checks=(check,),
    )
    acceptance = ReviewAcceptance(
        run_id="run-1",
        project_id="project-1",
        intent_id="intent-1",
        plan_id="plan-1",
        objective="Produce one verified bounded change.",
        constraints=("Do not weaken the exact acceptance check.",),
        base_commit="a" * 40,
        nodes=(node,),
    )
    evidence = (
        ReviewEvidence(
            ReviewEvidenceKind.SOURCE_BEFORE,
            node.node_id,
            _digest("source-before"),
            "VALUE = 1\n",
            1,
            path="src/value.py",
            operation=TextOperation.REPLACE,
        ),
        ReviewEvidence(
            ReviewEvidenceKind.SOURCE_AFTER,
            node.node_id,
            _digest("source-after"),
            "VALUE = 2\n",
            1,
            path="src/value.py",
            operation=TextOperation.REPLACE,
        ),
        ReviewEvidence(
            ReviewEvidenceKind.EFFECT,
            node.node_id,
            _digest("effect"),
            '{"changed_paths":["src/value.py"]}',
            1,
            path="src/value.py",
            operation=TextOperation.REPLACE,
        ),
        ReviewEvidence(
            ReviewEvidenceKind.OUTCOME,
            node.node_id,
            _digest("outcome"),
            '{"status":"succeeded"}',
            1,
        ),
        ReviewEvidence(
            ReviewEvidenceKind.CHECK_COMMAND,
            node.node_id,
            command_digest,
            '{"argv":["python"]}',
            1,
            check_id=check.check_id,
        ),
        ReviewEvidence(
            ReviewEvidenceKind.CHECK_RESULT,
            node.node_id,
            result_digest,
            '{"passed":true}',
            1,
            check_id=check.check_id,
        ),
        ReviewEvidence(
            ReviewEvidenceKind.CHECK_STDOUT,
            node.node_id,
            _digest("stdout"),
            "1 passed\n",
            1,
            check_id=check.check_id,
        ),
        ReviewEvidence(
            ReviewEvidenceKind.CHECK_STDERR,
            node.node_id,
            _digest("stderr"),
            "",
            1,
            check_id=check.check_id,
        ),
    )
    return ReviewContext(
        acceptance=acceptance,
        state_digest=_digest("state"),
        artifact_evidence_digest=_digest("artifact-evidence"),
        evidence=evidence,
    )


def _context_for_operation(operation: TextOperation) -> ReviewContext:
    context = _context()
    excluded_kind = (
        ReviewEvidenceKind.SOURCE_BEFORE
        if operation is TextOperation.CREATE
        else ReviewEvidenceKind.SOURCE_AFTER
    )
    change_kinds = {
        ReviewEvidenceKind.SOURCE_BEFORE,
        ReviewEvidenceKind.SOURCE_AFTER,
        ReviewEvidenceKind.EFFECT,
    }
    return replace(
        context,
        evidence=tuple(
            replace(item, operation=operation) if item.kind in change_kinds else item
            for item in context.evidence
            if item.kind is not excluded_kind
        ),
    )


def _admitted(
    context: ReviewContext,
    *,
    findings: tuple[ProposedReviewFinding, ...] = (),
) -> AdmittedReview:
    return admit_review(
        context,
        ReviewProposal(
            context_digest=context.digest,
            findings=findings,
            summary="All admitted findings are preserved for deterministic policy.",
            epistemic_assessments=_assessments(context, findings),
        ),
    )


def _assessments(
    context: ReviewContext,
    findings: tuple[ProposedReviewFinding, ...],
) -> tuple[EpistemicAssessment, ...]:
    evidence = context.evidence[0]
    citation = ReviewCitation(evidence.evidence_id, evidence.start_line, evidence.end_line)
    finding_ids = tuple(item.finding_id for item in findings)
    return tuple(
        EpistemicAssessment(
            dimension=dimension,
            disposition=(
                EpistemicDisposition.CONCERN
                if index == 0 and finding_ids
                else EpistemicDisposition.SUPPORTED
            ),
            claim=f"The review covered {dimension.value} against cited evidence.",
            falsification_question=f"What evidence would refute {dimension.value}?",
            citations=(citation,),
            finding_ids=finding_ids if index == 0 else (),
        )
        for index, dimension in enumerate(EpistemicDimension)
    )


def _digest(label: str) -> str:
    return json_digest({"label": label})
