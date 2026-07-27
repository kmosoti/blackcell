from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from blackcell.gateway import DataClassification, GatewayBudget, LocalityPolicy
from blackcell.kernel import JsonInput
from blackcell.kernel._json import json_digest
from blackcell.orchestration.changes import TextOperation
from blackcell.orchestration.review import (
    ADMITTED_REVIEW_SCHEMA,
    REVIEW_PROPOSAL_OUTPUT_SCHEMA,
    EpistemicAssessment,
    EpistemicDimension,
    EpistemicDisposition,
    ProposedReviewFinding,
    ReviewAcceptance,
    ReviewCheck,
    ReviewCitation,
    ReviewContext,
    ReviewContractError,
    ReviewContractFailureCode,
    ReviewEvidence,
    ReviewEvidenceKind,
    ReviewFindingCategory,
    ReviewPlanNode,
    ReviewProposal,
    ReviewProviderCall,
    ReviewProviderResult,
    ReviewSeverity,
    admit_review,
    admitted_review_from_mapping,
    admitted_review_payload,
    review_acceptance_payload,
    review_context_payload,
    review_proposal_from_mapping,
    review_proposal_payload,
    review_provider_result_from_mapping,
    review_provider_result_payload,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64
DIGEST_E = "sha256:" + "e" * 64


def test_review_context_binds_immutable_acceptance_and_host_derived_evidence() -> None:
    context = review_context()
    acceptance = context.acceptance
    node = acceptance.nodes[0]
    check = node.checks[0]
    operation = context.evidence[0].operation
    assert operation is not None
    payload = review_context_payload(context)

    assert payload["acceptance_digest"] == acceptance.digest
    assert payload["state_digest"] == DIGEST_D
    assert payload["artifact_evidence_digest"] == DIGEST_E
    assert payload["review_categories"] == [category.value for category in ReviewFindingCategory]
    assert payload["epistemic_dimensions"] == [item.value for item in EpistemicDimension]
    assert payload["epistemic_dispositions"] == [item.value for item in EpistemicDisposition]
    assert context.evidence[0].evidence_id == replace(context.evidence[0]).evidence_id
    assert context.evidence[0].evidence_id == json_digest(
        {
            "kind": context.evidence[0].kind.value,
            "node_id": context.evidence[0].node_id,
            "artifact_digest": context.evidence[0].artifact_digest,
            "operation": operation.value,
            "path": context.evidence[0].path,
            "check_id": context.evidence[0].check_id,
            "start_line": context.evidence[0].start_line,
            "end_line": context.evidence[0].end_line,
            "excerpt": context.evidence[0].excerpt,
        }
    )

    changed_expected = replace(check, expected_exit_code=1)
    changed_argv = replace(check, argv=("python", "-m", "pytest", "different::test"))
    changed_scope = replace(node, allowed_paths=("tests",))
    assert (
        replace(
            acceptance,
            nodes=(replace(node, checks=(changed_expected,)),),
        ).digest
        != acceptance.digest
    )
    assert (
        replace(
            acceptance,
            nodes=(replace(node, checks=(changed_argv,)),),
        ).digest
        != acceptance.digest
    )
    assert replace(acceptance, nodes=(changed_scope,)).digest != acceptance.digest

    serialized = repr(payload)
    for forbidden in ("repository_root", "worktree", "credential", "secret"):
        assert forbidden not in serialized

    root_scoped = replace(node, allowed_paths=(".",))
    root_acceptance = replace(acceptance, nodes=(root_scoped,))
    replace(context, acceptance=root_acceptance)

    second = replace(node, node_id="node-2", depends_on=("node-1",))
    first = replace(node, depends_on=("node-2",))
    with pytest.raises(ReviewContractError) as cyclic:
        replace(acceptance, nodes=(first, second))
    assert cyclic.value.code is ReviewContractFailureCode.INVALID_CONTEXT


def test_review_admission_accepts_every_fixed_finding_category_with_exact_citations() -> None:
    context = review_context()
    evidence = context.evidence[0]
    citation = ReviewCitation(evidence.evidence_id, evidence.start_line, evidence.end_line)
    findings = tuple(
        ProposedReviewFinding(
            finding_id=f"finding-{index}",
            category=category,
            severity=ReviewSeverity.MEDIUM,
            claim=f"Cited {category.value} claim.",
            impact="The bounded change may violate its acceptance contract.",
            recommendation="Inspect and remediate the cited lines.",
            citations=(citation,),
        )
        for index, category in enumerate(ReviewFindingCategory, start=1)
    )
    proposal = ReviewProposal(
        context.digest,
        findings,
        "Six structurally cited claims.",
        _assessments(context, tuple(item.finding_id for item in findings)),
    )

    admitted = admit_review(context, proposal)
    payload = admitted_review_payload(admitted)

    assert admitted.schema_version == ADMITTED_REVIEW_SCHEMA
    assert admitted.context_digest == context.digest
    assert admitted.acceptance_digest == context.acceptance.digest
    assert admitted.findings == findings
    assert payload["acceptance_digest"] == context.acceptance.digest
    assert "passed" not in payload
    assert "approved" not in payload


def test_review_admission_rejects_invented_or_out_of_range_evidence_and_duplicates() -> None:
    context = review_context()
    evidence = context.evidence[0]

    for citation in (
        ReviewCitation("sha256:" + "f" * 64, evidence.start_line, evidence.end_line),
        ReviewCitation(evidence.evidence_id, evidence.start_line, evidence.end_line + 1),
    ):
        proposal = ReviewProposal(
            context.digest,
            (_finding("finding-1", citation),),
            "One proposed finding.",
            _assessments(context, ("finding-1",)),
        )
        with pytest.raises(ReviewContractError) as rejected:
            admit_review(context, proposal)
        assert rejected.value.code is ReviewContractFailureCode.ADMISSION_REJECTED

    valid_citation = ReviewCitation(
        evidence.evidence_id,
        evidence.start_line,
        evidence.end_line,
    )
    duplicate = _finding("duplicate", valid_citation)
    with pytest.raises(ReviewContractError) as duplicate_findings:
        ReviewProposal(
            context.digest,
            (duplicate, duplicate),
            "Duplicated identifiers.",
            _assessments(context, ("duplicate",)),
        )
    assert duplicate_findings.value.code is ReviewContractFailureCode.INVALID_PROPOSAL

    with pytest.raises(ReviewContractError) as duplicate_citations:
        replace(duplicate, citations=(valid_citation, valid_citation))
    assert duplicate_citations.value.code is ReviewContractFailureCode.INVALID_PROPOSAL


def test_review_proposal_parser_is_closed_bounded_and_cannot_self_admit() -> None:
    context = review_context()
    valid = review_output(context)

    parsed = review_proposal_from_mapping(valid)
    assert parsed.context_digest == context.digest
    assert review_proposal_payload(parsed) == valid

    forbidden_variants: list[dict[str, JsonInput]] = []
    for key, value in (
        ("admitted", True),
        ("acceptance_digest", context.acceptance.digest),
        ("expected_exit_code", 0),
    ):
        variant = deepcopy(valid)
        variant[key] = value
        forbidden_variants.append(variant)

    unknown_category = deepcopy(valid)
    unknown_findings = cast("list[dict[str, JsonInput]]", unknown_category["findings"])
    unknown_findings[0]["category"] = "looks-good"
    forbidden_variants.append(unknown_category)

    missing_citation = deepcopy(valid)
    missing_findings = cast("list[dict[str, JsonInput]]", missing_citation["findings"])
    missing_findings[0]["citations"] = []
    forbidden_variants.append(missing_citation)

    oversized = deepcopy(valid)
    oversized["summary"] = "x" * 4_097
    forbidden_variants.append(oversized)

    for variant in forbidden_variants:
        with pytest.raises(ReviewContractError) as invalid:
            review_proposal_from_mapping(variant)
        assert invalid.value.code is ReviewContractFailureCode.INVALID_PROPOSAL

    raw_properties = REVIEW_PROPOSAL_OUTPUT_SCHEMA["properties"]
    assert isinstance(raw_properties, dict)
    assessments_schema = cast("dict[str, Any]", raw_properties["epistemic_assessments"])
    assert assessments_schema["minItems"] == assessments_schema["maxItems"] == 9
    assert set(assessments_schema["items"]["properties"]["dimension"]["enum"]) == {
        item.value for item in EpistemicDimension
    }
    assert "admitted" not in raw_properties
    assert "acceptance_digest" not in raw_properties
    assert "expected_exit_code" not in raw_properties


def test_epistemic_matrix_requires_complete_dimensions_and_exact_finding_links() -> None:
    context = review_context()
    evidence = context.evidence[0]
    citation = ReviewCitation(evidence.evidence_id, evidence.start_line, evidence.end_line)
    finding = _finding("finding-1", citation)
    clear = _assessments(context)
    linked = _assessments(context, (finding.finding_id,))

    invalid_matrices = (
        clear[:-1],
        (*clear[:-1], replace(clear[-1], dimension=clear[0].dimension)),
    )
    for assessments in invalid_matrices:
        with pytest.raises(ReviewContractError) as invalid:
            ReviewProposal(context.digest, (), "Invalid matrix.", assessments)
        assert invalid.value.code is ReviewContractFailureCode.INVALID_PROPOSAL

    with pytest.raises(ReviewContractError) as concern_without_link:
        replace(clear[0], disposition=EpistemicDisposition.CONCERN)
    assert concern_without_link.value.code is ReviewContractFailureCode.INVALID_PROPOSAL

    with pytest.raises(ReviewContractError) as link_without_concern:
        replace(clear[0], finding_ids=(finding.finding_id,))
    assert link_without_concern.value.code is ReviewContractFailureCode.INVALID_PROPOSAL

    with pytest.raises(ReviewContractError) as unlinked_finding:
        ReviewProposal(context.digest, (finding,), "Unlinked finding.", clear)
    assert unlinked_finding.value.code is ReviewContractFailureCode.INVALID_PROPOSAL

    with pytest.raises(ReviewContractError) as invented_link:
        ReviewProposal(context.digest, (), "Invented link.", linked)
    assert invented_link.value.code is ReviewContractFailureCode.INVALID_PROPOSAL


def test_epistemic_matrix_citations_are_admitted_against_host_evidence() -> None:
    context = review_context()
    evidence = context.evidence[0]
    assessments = _assessments(context)
    out_of_range = replace(
        assessments[0],
        citations=(
            ReviewCitation(
                evidence.evidence_id,
                evidence.start_line,
                evidence.end_line + 1,
            ),
        ),
    )
    proposal = ReviewProposal(
        context.digest,
        (),
        "Assessment cites outside the immutable excerpt.",
        (out_of_range, *assessments[1:]),
    )

    with pytest.raises(ReviewContractError) as rejected:
        admit_review(context, proposal)
    assert rejected.value.code is ReviewContractFailureCode.ADMISSION_REJECTED


def test_review_persisted_artifact_parsers_are_closed_and_round_trip() -> None:
    context = review_context()
    proposal = review_proposal_from_mapping(review_output(context))
    admitted = admit_review(context, proposal)
    provider = ReviewProviderResult(
        proposal=proposal,
        provider_output_digest=DIGEST_D,
        profile_id="review",
        adapter_id="codex-cli",
        model_id="gpt-review",
        input_tokens=100,
        output_tokens=25,
        latency_ms=500,
        cost_microusd=0,
        completed_at=datetime(2026, 7, 22, 20, tzinfo=UTC),
    )

    assert admitted_review_from_mapping(admitted_review_payload(admitted)) == admitted
    assert (
        review_provider_result_from_mapping(
            review_provider_result_payload(provider),
            proposal=proposal,
        )
        == provider
    )

    admitted_unknown = admitted_review_payload(admitted)
    admitted_unknown["approved"] = True
    with pytest.raises(ReviewContractError):
        admitted_review_from_mapping(admitted_unknown)

    provider_mismatch = review_provider_result_payload(provider)
    provider_mismatch["proposal_digest"] = DIGEST_E
    with pytest.raises(ReviewContractError):
        review_provider_result_from_mapping(provider_mismatch, proposal=proposal)


def test_review_acceptance_rejects_ambiguous_checks_scope_and_dependencies() -> None:
    context = review_context()
    acceptance = context.acceptance
    node = acceptance.nodes[0]
    check = node.checks[0]

    invalid_checks = (
        {"check_id": "bad id"},
        {"argv": cast("tuple[str, ...]", ["python"])},
        {"argv": ()},
        {"argv": ("python", "")},
        {"expected_exit_code": True},
        {"expected_exit_code": 256},
        {"command_digest": "not-a-digest"},
        {"result_digest": "not-a-digest"},
        {"passed": cast("bool", 1)},
    )
    for replacement in invalid_checks:
        with pytest.raises(ReviewContractError) as caught:
            replace(check, **replacement)
        assert caught.value.code is ReviewContractFailureCode.INVALID_CONTEXT

    invalid_nodes = (
        {"node_id": "bad id"},
        {"objective": ""},
        {"depends_on": cast("tuple[str, ...]", ["node-0"])},
        {"depends_on": ("node-1",)},
        {"effects": cast("tuple[str, ...]", ["repository-read"])},
        {"effects": ()},
        {"effects": ("repository-read", "repository-read")},
        {"effects": ("execute-anything",)},
        {"allowed_paths": cast("tuple[str, ...]", ["src"])},
        {"max_changed_files": True},
        {"checks": cast("tuple[ReviewCheck, ...]", [check])},
        {"checks": ()},
        {"checks": cast("tuple[ReviewCheck, ...]", ("not-a-check",))},
        {"checks": (check, check)},
        {"allowed_paths": ("src", "src")},
        {"effects": ("repository-read", "process")},
        {"allowed_paths": (), "max_changed_files": 0},
    )
    for replacement in invalid_nodes:
        with pytest.raises(ReviewContractError) as caught:
            replace(node, **replacement)
        assert caught.value.code is ReviewContractFailureCode.INVALID_CONTEXT

    unknown_dependency = replace(node, node_id="node-2", depends_on=("missing",))
    invalid_acceptance = (
        {"schema_version": "review-acceptance/v2"},
        {"run_id": "bad id"},
        {"objective": ""},
        {"constraints": cast("tuple[str, ...]", ["constraint"])},
        {"constraints": ("duplicate", "duplicate")},
        {"constraints": ("",)},
        {"base_commit": "not-a-commit"},
        {"nodes": cast("tuple[ReviewPlanNode, ...]", [node])},
        {"nodes": ()},
        {"nodes": cast("tuple[ReviewPlanNode, ...]", ("not-a-node",))},
        {"nodes": (node, node)},
        {"nodes": (unknown_dependency,)},
    )
    for replacement in invalid_acceptance:
        with pytest.raises(ReviewContractError) as caught:
            replace(acceptance, **replacement)
        assert caught.value.code is ReviewContractFailureCode.INVALID_CONTEXT


def test_review_evidence_and_context_bind_exact_host_evidence() -> None:
    context = review_context()
    evidence = context.evidence[0]
    invalid_evidence = (
        {"kind": cast("ReviewEvidenceKind", "source-after")},
        {"node_id": "bad id"},
        {"artifact_digest": "not-a-digest"},
        {"excerpt": "contains\x00nul"},
        {"excerpt": "x" * (32 * 1024 + 1)},
        {"start_line": True},
        {"start_line": 0},
        {"check_id": "bad id"},
        {"path": None},
        {"operation": None},
        {"operation": cast("TextOperation", "replace")},
        {"operation": TextOperation.DELETE},
        {"kind": ReviewEvidenceKind.CHECK_RESULT, "path": None, "check_id": None},
        {"kind": ReviewEvidenceKind.OUTCOME, "path": "src/value.py"},
        {"start_line": 10_000_000, "excerpt": "first\nsecond\n"},
        {"path": "."},
        {"path": "/absolute"},
    )
    for replacement in invalid_evidence:
        with pytest.raises(ReviewContractError) as caught:
            replace(evidence, **replacement)
        assert caught.value.code is ReviewContractFailureCode.INVALID_CONTEXT

    unknown_node = replace(evidence, node_id="node-unknown")
    outside_scope = replace(evidence, path="tests/value.py")
    check_evidence = ReviewEvidence(
        kind=ReviewEvidenceKind.CHECK_RESULT,
        node_id="node-1",
        artifact_digest=DIGEST_C,
        excerpt="exit_code=0\n",
        start_line=1,
        check_id="unknown-check",
    )
    invalid_contexts = (
        {"schema_version": "review-context/v2"},
        {"acceptance": cast("ReviewAcceptance", object())},
        {"state_digest": "not-a-digest"},
        {"artifact_evidence_digest": "not-a-digest"},
        {"evidence": cast("tuple[ReviewEvidence, ...]", [evidence])},
        {"evidence": ()},
        {"evidence": cast("tuple[ReviewEvidence, ...]", ("not-evidence",))},
        {"evidence": (evidence, evidence)},
        {"evidence": (unknown_node,)},
        {"evidence": (outside_scope,)},
        {"evidence": (check_evidence,)},
    )
    for replacement in invalid_contexts:
        with pytest.raises(ReviewContractError) as caught:
            replace(context, **replacement)
        assert caught.value.code is ReviewContractFailureCode.INVALID_CONTEXT


def test_review_proposal_provider_and_admission_boundaries_reject_wrong_types() -> None:
    context = review_context()
    evidence = context.evidence[0]
    citation = ReviewCitation(evidence.evidence_id, evidence.start_line, evidence.end_line)
    finding = _finding("finding-1", citation)
    proposal = ReviewProposal(
        context.digest,
        (finding,),
        "One bounded finding.",
        _assessments(context, ("finding-1",)),
    )

    for replacement in (
        {"evidence_id": "not-a-digest"},
        {"start_line": True},
        {"start_line": 0},
        {"end_line": cast("int", True)},
        {"end_line": citation.start_line - 1},
    ):
        with pytest.raises(ReviewContractError) as caught:
            replace(citation, **replacement)
        assert caught.value.code is ReviewContractFailureCode.INVALID_PROPOSAL

    invalid_findings = (
        {"finding_id": "bad id"},
        {"category": cast("ReviewFindingCategory", "correctness")},
        {"severity": cast("ReviewSeverity", "high")},
        {"claim": ""},
        {"citations": cast("tuple[ReviewCitation, ...]", [citation])},
        {"citations": ()},
        {"citations": cast("tuple[ReviewCitation, ...]", ("not-a-citation",))},
    )
    for replacement in invalid_findings:
        with pytest.raises(ReviewContractError) as caught:
            replace(finding, **replacement)
        assert caught.value.code is ReviewContractFailureCode.INVALID_PROPOSAL

    invalid_proposals = (
        {"schema_version": "review-proposal/v2"},
        {"context_digest": "not-a-digest"},
        {"findings": cast("tuple[ProposedReviewFinding, ...]", [finding])},
        {"findings": cast("tuple[ProposedReviewFinding, ...]", ("not-a-finding",))},
        {"summary": ""},
    )
    for replacement in invalid_proposals:
        with pytest.raises(ReviewContractError) as caught:
            replace(proposal, **replacement)
        assert caught.value.code is ReviewContractFailureCode.INVALID_PROPOSAL

    admitted = admit_review(context, proposal)
    for replacement in (
        {"schema_version": "execution-admitted-review/v2"},
        {"context_digest": "not-a-digest"},
        {"acceptance_digest": "not-a-digest"},
        {"findings": cast("tuple[ProposedReviewFinding, ...]", [finding])},
        {"summary": ""},
    ):
        with pytest.raises(ReviewContractError) as caught:
            replace(admitted, **replacement)
        assert caught.value.code is ReviewContractFailureCode.ADMISSION_REJECTED

    call = ReviewProviderCall(
        request_id="request-1",
        correlation_id="correlation-1",
        review_id="review-1",
        context=context,
        classification=DataClassification.PRIVATE,
        locality=LocalityPolicy.REMOTE_ALLOWED,
        budget=GatewayBudget(100, 50, 1_000, 0),
        estimated_input_tokens=10,
        causation_id="event-1",
    )
    invalid_calls = (
        {"request_id": "bad id"},
        {"context": cast("ReviewContext", object())},
        {"classification": cast("DataClassification", "private")},
        {"locality": cast("LocalityPolicy", "remote-allowed")},
        {"budget": cast("GatewayBudget", object())},
        {"estimated_input_tokens": True},
        {"estimated_input_tokens": -1},
        {"causation_id": "bad id"},
    )
    for replacement in invalid_calls:
        with pytest.raises(ReviewContractError) as caught:
            replace(call, **replacement)
        assert caught.value.code is ReviewContractFailureCode.INVALID_CONTEXT

    provider = ReviewProviderResult(
        proposal=proposal,
        provider_output_digest=DIGEST_D,
        profile_id="review",
        adapter_id="codex-cli",
        model_id="gpt-review",
        input_tokens=10,
        output_tokens=5,
        latency_ms=100,
        cost_microusd=0,
        completed_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    invalid_results = (
        {"schema_version": "review-provider-result/v2"},
        {"proposal": cast("ReviewProposal", object())},
        {"provider_output_digest": "not-a-digest"},
        {"profile_id": ""},
        {"input_tokens": True},
        {"output_tokens": -1},
        {"completed_at": cast("datetime", object())},
        {"completed_at": datetime(2026, 7, 22)},
    )
    for replacement in invalid_results:
        with pytest.raises(ReviewContractError) as caught:
            replace(provider, **replacement)
        assert caught.value.code is ReviewContractFailureCode.INVALID_PROPOSAL

    with pytest.raises(ReviewContractError) as wrong_admission:
        admit_review(cast("ReviewContext", object()), proposal)
    assert wrong_admission.value.code is ReviewContractFailureCode.ADMISSION_REJECTED


def test_review_serializers_and_parsers_reject_malformed_persisted_shapes() -> None:
    context = review_context()
    proposal = review_proposal_from_mapping(review_output(context))
    admitted = admit_review(context, proposal)
    provider = ReviewProviderResult(
        proposal=proposal,
        provider_output_digest=DIGEST_D,
        profile_id="review",
        adapter_id="codex-cli",
        model_id="gpt-review",
        input_tokens=10,
        output_tokens=5,
        latency_ms=100,
        cost_microusd=0,
        completed_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    serializers = (
        (review_acceptance_payload, ReviewContractFailureCode.INVALID_CONTEXT),
        (review_context_payload, ReviewContractFailureCode.INVALID_CONTEXT),
        (review_proposal_payload, ReviewContractFailureCode.INVALID_PROPOSAL),
        (review_provider_result_payload, ReviewContractFailureCode.INVALID_PROPOSAL),
        (admitted_review_payload, ReviewContractFailureCode.ADMISSION_REJECTED),
    )
    for serializer, code in serializers:
        with pytest.raises(ReviewContractError) as caught:
            serializer(object())  # ty: ignore[invalid-argument-type]
        assert caught.value.code is code

    admitted_payload = admitted_review_payload(admitted)
    admitted_variants = (
        [],
        {**admitted_payload, "schema_version": "execution-admitted-review/v2"},
        {**admitted_payload, "summary": 1},
        {**admitted_payload, "findings": "not-an-array"},
    )
    for value in admitted_variants:
        with pytest.raises(ReviewContractError):
            admitted_review_from_mapping(cast("dict[str, object]", value))

    provider_payload = review_provider_result_payload(provider)
    provider_variants = (
        {**provider_payload, "schema_version": "review-provider-result/v2"},
        {**provider_payload, "completed_at": "not-a-time"},
        {**provider_payload, "profile_id": 1},
        {**provider_payload, "input_tokens": True},
    )
    for value in provider_variants:
        with pytest.raises(ReviewContractError):
            review_provider_result_from_mapping(value, proposal=proposal)

    output = review_output(context)
    finding = cast("list[dict[str, JsonInput]]", output["findings"])[0]
    citation = cast("list[dict[str, JsonInput]]", finding["citations"])[0]
    proposal_variants: list[object] = [
        [],
        {**output, "schema_version": "review-proposal/v2"},
        {**output, "summary": 1},
        {**output, "findings": "not-an-array"},
        {**output, "findings": [{"finding_id": "incomplete"}]},
        {**output, "findings": [{**finding, "category": "unknown"}]},
        {**output, "findings": [{**finding, "claim": 1}]},
        {**output, "findings": [{**finding, "citations": [{"evidence_id": DIGEST_A}]}]},
        {
            **output,
            "findings": [{**finding, "citations": [{**citation, "start_line": True}]}],
        },
    ]
    for value in proposal_variants:
        with pytest.raises(ReviewContractError):
            review_proposal_from_mapping(cast("dict[str, object]", value))


def review_context() -> ReviewContext:
    check = ReviewCheck(
        check_id="unit-check",
        argv=("python", "-m", "pytest", "tests/unit/test_value.py::test_value"),
        expected_exit_code=0,
        command_digest=DIGEST_A,
        result_digest=DIGEST_B,
        passed=True,
    )
    node = ReviewPlanNode(
        node_id="node-1",
        objective="Update the bounded value and prove its exact acceptance check.",
        depends_on=(),
        effects=("repository-read", "repository-write", "process"),
        allowed_paths=("src",),
        max_changed_files=2,
        checks=(check,),
    )
    acceptance = ReviewAcceptance(
        run_id="run-1",
        project_id="project-1",
        intent_id="intent-1",
        plan_id="plan-1",
        objective="Produce a reviewable bounded change.",
        constraints=("Do not weaken acceptance.", "Stay inside the declared scope."),
        base_commit="a" * 40,
        nodes=(node,),
    )
    evidence = ReviewEvidence(
        kind=ReviewEvidenceKind.SOURCE_AFTER,
        node_id=node.node_id,
        artifact_digest=DIGEST_C,
        path="src/value.py",
        operation=TextOperation.REPLACE,
        start_line=10,
        excerpt="VALUE = 2\nassert VALUE == 2\n",
    )
    return ReviewContext(
        acceptance=acceptance,
        state_digest=DIGEST_D,
        artifact_evidence_digest=DIGEST_E,
        evidence=(evidence,),
    )


def review_output(context: ReviewContext) -> dict[str, JsonInput]:
    evidence = context.evidence[0]
    proposal = ReviewProposal(
        context_digest=context.digest,
        findings=(
            _finding(
                "finding-1",
                ReviewCitation(
                    evidence.evidence_id,
                    evidence.start_line,
                    evidence.end_line,
                ),
            ),
        ),
        summary="One cited proposal for host admission.",
        epistemic_assessments=_assessments(context, ("finding-1",)),
    )
    return review_proposal_payload(proposal)


def _finding(finding_id: str, citation: ReviewCitation) -> ProposedReviewFinding:
    return ProposedReviewFinding(
        finding_id=finding_id,
        category=ReviewFindingCategory.CORRECTNESS,
        severity=ReviewSeverity.HIGH,
        claim="The cited result contradicts the bounded objective.",
        impact="The node may not satisfy its immutable acceptance contract.",
        recommendation="Correct the implementation without changing acceptance.",
        citations=(citation,),
    )


def _assessments(
    context: ReviewContext,
    finding_ids: tuple[str, ...] = (),
) -> tuple[EpistemicAssessment, ...]:
    evidence = context.evidence[0]
    citation = ReviewCitation(evidence.evidence_id, evidence.start_line, evidence.end_line)
    return tuple(
        EpistemicAssessment(
            dimension=dimension,
            disposition=(
                EpistemicDisposition.CONCERN
                if index == 0 and finding_ids
                else EpistemicDisposition.SUPPORTED
            ),
            claim=f"The review explicitly covered {dimension.value}.",
            falsification_question=f"What cited evidence would refute {dimension.value}?",
            citations=(citation,),
            finding_ids=finding_ids if index == 0 else (),
        )
        for index, dimension in enumerate(EpistemicDimension)
    )
