from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import cast

import pytest

from blackcell.kernel import JsonInput
from blackcell.kernel._json import json_digest
from blackcell.orchestration.alpha_review import (
    AlphaAdmittedReview,
    AlphaProposedReviewFinding,
    AlphaReviewAcceptance,
    AlphaReviewCheck,
    AlphaReviewCitation,
    AlphaReviewContext,
    AlphaReviewEvidence,
    AlphaReviewEvidenceKind,
    AlphaReviewFindingCategory,
    AlphaReviewPlanNode,
    AlphaReviewProposal,
    AlphaReviewSeverity,
    admit_alpha_review,
)
from blackcell.orchestration.alpha_verify import (
    AlphaVerificationCriterionKind,
    AlphaVerificationError,
    AlphaVerificationFailureCode,
    AlphaVerificationReasonCode,
    AlphaVerificationStatus,
    alpha_verification_matrix_payload,
    alpha_verification_report_from_mapping,
    alpha_verification_report_payload,
    verify_alpha_review,
)


def test_verifier_passes_complete_execution_and_clear_review_with_exact_matrix() -> None:
    context = _context()
    admitted = _admitted(context)

    report = verify_alpha_review(context, admitted)
    repeated = verify_alpha_review(context, admitted)
    payload = alpha_verification_report_payload(report)

    assert report == repeated
    assert report.digest == repeated.digest
    assert report.status is AlphaVerificationStatus.PASS
    assert report.run_id == context.acceptance.run_id
    assert report.context_digest == context.digest
    assert report.acceptance_digest == context.acceptance.digest
    assert report.state_digest == context.state_digest
    assert report.artifact_evidence_digest == context.artifact_evidence_digest
    assert report.admitted_review_digest == admitted.digest
    assert tuple(row.kind for row in report.matrix) == (
        AlphaVerificationCriterionKind.OBJECTIVE,
        AlphaVerificationCriterionKind.CONSTRAINT,
        AlphaVerificationCriterionKind.NODE,
        AlphaVerificationCriterionKind.WRITE_SCOPE,
        AlphaVerificationCriterionKind.CHECK,
        AlphaVerificationCriterionKind.REVIEW_POLICY,
    )
    assert all(row.status is AlphaVerificationStatus.PASS for row in report.matrix)
    check_row = next(
        row for row in report.matrix if row.kind is AlphaVerificationCriterionKind.CHECK
    )
    assert len(check_row.evidence_ids) == 4
    assert check_row.node_id == "node-1"
    assert check_row.check_id == "unit-check"
    assert payload["status"] == "pass"
    serialized = repr(payload)
    assert "VALUE = 1" not in serialized
    assert "VALUE = 2" not in serialized
    assert "approved" not in serialized


def test_verifier_fails_unresolved_reward_hacking_findings_without_treating_them_as_truth() -> None:
    context = _context()
    evidence = next(
        item for item in context.evidence if item.kind is AlphaReviewEvidenceKind.SOURCE_AFTER
    )
    citation = AlphaReviewCitation(
        evidence.evidence_id,
        evidence.start_line,
        evidence.end_line,
    )
    findings = tuple(
        AlphaProposedReviewFinding(
            finding_id=f"finding-{index}",
            category=category,
            severity=AlphaReviewSeverity.LOW,
            claim=f"Unresolved {category.value} claim.",
            impact="The bounded result may violate its acceptance contract.",
            recommendation="Resolve the cited finding before verification.",
            citations=(citation,),
        )
        for index, category in enumerate(AlphaReviewFindingCategory, start=1)
    )
    admitted = _admitted(context, findings=findings)

    report = verify_alpha_review(context, admitted)

    assert report.status is AlphaVerificationStatus.FAIL
    assert report.acceptance_digest == context.acceptance.digest
    assert all(
        finding.finding_id
        in next(row for row in report.matrix if row.criterion_id == "review").finding_ids
        for finding in findings
    )
    assert any(
        AlphaVerificationReasonCode.UNRESOLVED_REVIEW_FINDING in row.reason_codes
        for row in report.matrix
        if row.status is AlphaVerificationStatus.FAIL
    )
    payload = alpha_verification_report_payload(report)
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
    failed = verify_alpha_review(failed_context, _admitted(failed_context))
    failed_row = next(
        row for row in failed.matrix if row.kind is AlphaVerificationCriterionKind.CHECK
    )
    assert failed.status is AlphaVerificationStatus.FAIL
    assert AlphaVerificationReasonCode.CHECK_FAILED in failed_row.reason_codes

    missing_context = replace(
        context,
        evidence=tuple(
            item for item in context.evidence if item.kind is not AlphaReviewEvidenceKind.EFFECT
        ),
    )
    missing = verify_alpha_review(missing_context, _admitted(missing_context))
    scope = next(
        row for row in missing.matrix if row.kind is AlphaVerificationCriterionKind.WRITE_SCOPE
    )
    assert missing.status is AlphaVerificationStatus.INCONCLUSIVE
    assert AlphaVerificationReasonCode.CHANGE_EVIDENCE_MISSING in scope.reason_codes

    duplicate_outcome = AlphaReviewEvidence(
        kind=AlphaReviewEvidenceKind.OUTCOME,
        node_id="node-1",
        artifact_digest=_digest("second-outcome"),
        excerpt='{"status":"succeeded","copy":2}',
        start_line=1,
    )
    ambiguous_context = replace(context, evidence=(*context.evidence, duplicate_outcome))
    ambiguous = verify_alpha_review(ambiguous_context, _admitted(ambiguous_context))
    node_row = next(
        row for row in ambiguous.matrix if row.kind is AlphaVerificationCriterionKind.NODE
    )
    assert ambiguous.status is AlphaVerificationStatus.INCONCLUSIVE
    assert AlphaVerificationReasonCode.EVIDENCE_AMBIGUOUS in node_row.reason_codes

    command = next(
        item for item in context.evidence if item.kind is AlphaReviewEvidenceKind.CHECK_COMMAND
    )
    mismatched_context = replace(
        context,
        evidence=tuple(
            replace(item, artifact_digest=_digest("wrong-command")) if item is command else item
            for item in context.evidence
        ),
    )
    mismatch = verify_alpha_review(mismatched_context, _admitted(mismatched_context))
    mismatch_row = next(
        row for row in mismatch.matrix if row.kind is AlphaVerificationCriterionKind.CHECK
    )
    assert mismatch.status is AlphaVerificationStatus.FAIL
    assert AlphaVerificationReasonCode.EVIDENCE_IDENTITY_MISMATCH in mismatch_row.reason_codes


def test_verifier_rejects_binding_and_citation_drift_content_free() -> None:
    context = _context()
    admitted = _admitted(context)
    with pytest.raises(AlphaVerificationError) as binding:
        verify_alpha_review(
            context,
            replace(admitted, context_digest=_digest("other-context")),
        )
    assert binding.value.code is AlphaVerificationFailureCode.BINDING_MISMATCH
    assert str(binding.value) == "alpha-verification-binding-mismatch"

    evidence = context.evidence[0]
    invalid_finding = AlphaProposedReviewFinding(
        finding_id="finding-invalid-range",
        category=AlphaReviewFindingCategory.HIDDEN_SHORTCUT,
        severity=AlphaReviewSeverity.HIGH,
        claim="The citation range exceeds host evidence.",
        impact="The claim is not source-bound.",
        recommendation="Reject the unbound claim.",
        citations=(
            AlphaReviewCitation(
                evidence.evidence_id,
                evidence.start_line,
                evidence.end_line + 1,
            ),
        ),
    )
    invalid = AlphaAdmittedReview(
        context_digest=context.digest,
        acceptance_digest=context.acceptance.digest,
        findings=(invalid_finding,),
        summary="Structurally shaped but not context-admitted.",
    )
    with pytest.raises(AlphaVerificationError) as citation:
        verify_alpha_review(context, invalid)
    assert citation.value.code is AlphaVerificationFailureCode.CITATION_MISMATCH
    assert "citation range" not in str(citation.value)


def test_verification_report_parser_and_matrix_digest_are_closed_and_stable() -> None:
    context = _context()
    report = verify_alpha_review(context, _admitted(context))
    payload = alpha_verification_report_payload(report)

    assert alpha_verification_report_from_mapping(payload) == report
    assert report.matrix_digest == json_digest(alpha_verification_matrix_payload(report))

    unknown = deepcopy(payload)
    unknown["approved"] = True
    invalid_row = deepcopy(payload)
    matrix = cast("list[dict[str, JsonInput]]", invalid_row["matrix"])
    matrix[0]["reason_codes"] = []
    for malformed in (unknown, invalid_row):
        with pytest.raises(AlphaVerificationError) as caught:
            alpha_verification_report_from_mapping(malformed)
        assert caught.value.code is AlphaVerificationFailureCode.INVALID_INPUT


def _context() -> AlphaReviewContext:
    command_digest = _digest("command")
    result_digest = _digest("result")
    check = AlphaReviewCheck(
        check_id="unit-check",
        argv=("python", "-m", "pytest", "tests/unit/test_value.py::test_value"),
        expected_exit_code=0,
        command_digest=command_digest,
        result_digest=result_digest,
        passed=True,
    )
    node = AlphaReviewPlanNode(
        node_id="node-1",
        objective="Update the bounded value and run its exact check.",
        depends_on=(),
        effects=("repository-read", "repository-write", "process"),
        allowed_paths=("src",),
        max_changed_files=1,
        checks=(check,),
    )
    acceptance = AlphaReviewAcceptance(
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
        AlphaReviewEvidence(
            AlphaReviewEvidenceKind.SOURCE_BEFORE,
            node.node_id,
            _digest("source-before"),
            "VALUE = 1\n",
            1,
            path="src/value.py",
        ),
        AlphaReviewEvidence(
            AlphaReviewEvidenceKind.SOURCE_AFTER,
            node.node_id,
            _digest("source-after"),
            "VALUE = 2\n",
            1,
            path="src/value.py",
        ),
        AlphaReviewEvidence(
            AlphaReviewEvidenceKind.EFFECT,
            node.node_id,
            _digest("effect"),
            '{"changed_paths":["src/value.py"]}',
            1,
            path="src/value.py",
        ),
        AlphaReviewEvidence(
            AlphaReviewEvidenceKind.OUTCOME,
            node.node_id,
            _digest("outcome"),
            '{"status":"succeeded"}',
            1,
        ),
        AlphaReviewEvidence(
            AlphaReviewEvidenceKind.CHECK_COMMAND,
            node.node_id,
            command_digest,
            '{"argv":["python"]}',
            1,
            check_id=check.check_id,
        ),
        AlphaReviewEvidence(
            AlphaReviewEvidenceKind.CHECK_RESULT,
            node.node_id,
            result_digest,
            '{"passed":true}',
            1,
            check_id=check.check_id,
        ),
        AlphaReviewEvidence(
            AlphaReviewEvidenceKind.CHECK_STDOUT,
            node.node_id,
            _digest("stdout"),
            "1 passed\n",
            1,
            check_id=check.check_id,
        ),
        AlphaReviewEvidence(
            AlphaReviewEvidenceKind.CHECK_STDERR,
            node.node_id,
            _digest("stderr"),
            "",
            1,
            check_id=check.check_id,
        ),
    )
    return AlphaReviewContext(
        acceptance=acceptance,
        state_digest=_digest("state"),
        artifact_evidence_digest=_digest("artifact-evidence"),
        evidence=evidence,
    )


def _admitted(
    context: AlphaReviewContext,
    *,
    findings: tuple[AlphaProposedReviewFinding, ...] = (),
) -> AlphaAdmittedReview:
    return admit_alpha_review(
        context,
        AlphaReviewProposal(
            context_digest=context.digest,
            findings=findings,
            summary="All admitted findings are preserved for deterministic policy.",
        ),
    )


def _digest(label: str) -> str:
    return json_digest({"label": label})
