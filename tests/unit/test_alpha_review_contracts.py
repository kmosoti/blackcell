from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest

from blackcell.gateway import DataClassification, GatewayBudget, LocalityPolicy
from blackcell.kernel import JsonInput
from blackcell.kernel._json import json_digest
from blackcell.orchestration.alpha_review import (
    ALPHA_ADMITTED_REVIEW_SCHEMA,
    ALPHA_REVIEW_PROPOSAL_OUTPUT_SCHEMA,
    AlphaProposedReviewFinding,
    AlphaReviewAcceptance,
    AlphaReviewCheck,
    AlphaReviewCitation,
    AlphaReviewContext,
    AlphaReviewContractError,
    AlphaReviewContractFailureCode,
    AlphaReviewEvidence,
    AlphaReviewEvidenceKind,
    AlphaReviewFindingCategory,
    AlphaReviewPlanNode,
    AlphaReviewProposal,
    AlphaReviewProviderCall,
    AlphaReviewProviderResult,
    AlphaReviewSeverity,
    admit_alpha_review,
    alpha_admitted_review_from_mapping,
    alpha_admitted_review_payload,
    alpha_review_acceptance_payload,
    alpha_review_context_payload,
    alpha_review_proposal_from_mapping,
    alpha_review_proposal_payload,
    alpha_review_provider_result_from_mapping,
    alpha_review_provider_result_payload,
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
    payload = alpha_review_context_payload(context)

    assert payload["acceptance_digest"] == acceptance.digest
    assert payload["state_digest"] == DIGEST_D
    assert payload["artifact_evidence_digest"] == DIGEST_E
    assert payload["review_categories"] == [
        category.value for category in AlphaReviewFindingCategory
    ]
    assert context.evidence[0].evidence_id == replace(context.evidence[0]).evidence_id
    assert context.evidence[0].evidence_id == json_digest(
        {
            "kind": context.evidence[0].kind.value,
            "node_id": context.evidence[0].node_id,
            "artifact_digest": context.evidence[0].artifact_digest,
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
    with pytest.raises(AlphaReviewContractError) as cyclic:
        replace(acceptance, nodes=(first, second))
    assert cyclic.value.code is AlphaReviewContractFailureCode.INVALID_CONTEXT


def test_review_admission_accepts_every_fixed_finding_category_with_exact_citations() -> None:
    context = review_context()
    evidence = context.evidence[0]
    citation = AlphaReviewCitation(evidence.evidence_id, evidence.start_line, evidence.end_line)
    findings = tuple(
        AlphaProposedReviewFinding(
            finding_id=f"finding-{index}",
            category=category,
            severity=AlphaReviewSeverity.MEDIUM,
            claim=f"Cited {category.value} claim.",
            impact="The bounded change may violate its acceptance contract.",
            recommendation="Inspect and remediate the cited lines.",
            citations=(citation,),
        )
        for index, category in enumerate(AlphaReviewFindingCategory, start=1)
    )
    proposal = AlphaReviewProposal(context.digest, findings, "Six structurally cited claims.")

    admitted = admit_alpha_review(context, proposal)
    payload = alpha_admitted_review_payload(admitted)

    assert admitted.schema_version == ALPHA_ADMITTED_REVIEW_SCHEMA
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
        AlphaReviewCitation("sha256:" + "f" * 64, evidence.start_line, evidence.end_line),
        AlphaReviewCitation(evidence.evidence_id, evidence.start_line, evidence.end_line + 1),
    ):
        proposal = AlphaReviewProposal(
            context.digest,
            (_finding("finding-1", citation),),
            "One proposed finding.",
        )
        with pytest.raises(AlphaReviewContractError) as rejected:
            admit_alpha_review(context, proposal)
        assert rejected.value.code is AlphaReviewContractFailureCode.ADMISSION_REJECTED

    valid_citation = AlphaReviewCitation(
        evidence.evidence_id,
        evidence.start_line,
        evidence.end_line,
    )
    duplicate = _finding("duplicate", valid_citation)
    with pytest.raises(AlphaReviewContractError) as duplicate_findings:
        AlphaReviewProposal(
            context.digest,
            (duplicate, duplicate),
            "Duplicated identifiers.",
        )
    assert duplicate_findings.value.code is AlphaReviewContractFailureCode.INVALID_PROPOSAL

    with pytest.raises(AlphaReviewContractError) as duplicate_citations:
        replace(duplicate, citations=(valid_citation, valid_citation))
    assert duplicate_citations.value.code is AlphaReviewContractFailureCode.INVALID_PROPOSAL


def test_review_proposal_parser_is_closed_bounded_and_cannot_self_admit() -> None:
    context = review_context()
    valid = review_output(context)

    parsed = alpha_review_proposal_from_mapping(valid)
    assert parsed.context_digest == context.digest
    assert alpha_review_proposal_payload(parsed) == valid

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
        with pytest.raises(AlphaReviewContractError) as invalid:
            alpha_review_proposal_from_mapping(variant)
        assert invalid.value.code is AlphaReviewContractFailureCode.INVALID_PROPOSAL

    raw_properties = ALPHA_REVIEW_PROPOSAL_OUTPUT_SCHEMA["properties"]
    assert isinstance(raw_properties, dict)
    assert "admitted" not in raw_properties
    assert "acceptance_digest" not in raw_properties
    assert "expected_exit_code" not in raw_properties


def test_review_persisted_artifact_parsers_are_closed_and_round_trip() -> None:
    context = review_context()
    proposal = alpha_review_proposal_from_mapping(review_output(context))
    admitted = admit_alpha_review(context, proposal)
    provider = AlphaReviewProviderResult(
        proposal=proposal,
        provider_output_digest=DIGEST_D,
        profile_id="alpha-review",
        adapter_id="codex-cli",
        model_id="gpt-review",
        input_tokens=100,
        output_tokens=25,
        latency_ms=500,
        cost_microusd=0,
        completed_at=datetime(2026, 7, 22, 20, tzinfo=UTC),
    )

    assert alpha_admitted_review_from_mapping(alpha_admitted_review_payload(admitted)) == admitted
    assert (
        alpha_review_provider_result_from_mapping(
            alpha_review_provider_result_payload(provider),
            proposal=proposal,
        )
        == provider
    )

    admitted_unknown = alpha_admitted_review_payload(admitted)
    admitted_unknown["approved"] = True
    with pytest.raises(AlphaReviewContractError):
        alpha_admitted_review_from_mapping(admitted_unknown)

    provider_mismatch = alpha_review_provider_result_payload(provider)
    provider_mismatch["proposal_digest"] = DIGEST_E
    with pytest.raises(AlphaReviewContractError):
        alpha_review_provider_result_from_mapping(provider_mismatch, proposal=proposal)


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
        with pytest.raises(AlphaReviewContractError) as caught:
            replace(check, **replacement)
        assert caught.value.code is AlphaReviewContractFailureCode.INVALID_CONTEXT

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
        {"checks": cast("tuple[AlphaReviewCheck, ...]", [check])},
        {"checks": ()},
        {"checks": cast("tuple[AlphaReviewCheck, ...]", ("not-a-check",))},
        {"checks": (check, check)},
        {"allowed_paths": ("src", "src")},
        {"effects": ("repository-read", "process")},
        {"allowed_paths": (), "max_changed_files": 0},
    )
    for replacement in invalid_nodes:
        with pytest.raises(AlphaReviewContractError) as caught:
            replace(node, **replacement)
        assert caught.value.code is AlphaReviewContractFailureCode.INVALID_CONTEXT

    unknown_dependency = replace(node, node_id="node-2", depends_on=("missing",))
    invalid_acceptance = (
        {"schema_version": "alpha-review-acceptance/v2"},
        {"run_id": "bad id"},
        {"objective": ""},
        {"constraints": cast("tuple[str, ...]", ["constraint"])},
        {"constraints": ("duplicate", "duplicate")},
        {"constraints": ("",)},
        {"base_commit": "not-a-commit"},
        {"nodes": cast("tuple[AlphaReviewPlanNode, ...]", [node])},
        {"nodes": ()},
        {"nodes": cast("tuple[AlphaReviewPlanNode, ...]", ("not-a-node",))},
        {"nodes": (node, node)},
        {"nodes": (unknown_dependency,)},
    )
    for replacement in invalid_acceptance:
        with pytest.raises(AlphaReviewContractError) as caught:
            replace(acceptance, **replacement)
        assert caught.value.code is AlphaReviewContractFailureCode.INVALID_CONTEXT


def test_review_evidence_and_context_bind_exact_host_evidence() -> None:
    context = review_context()
    evidence = context.evidence[0]
    invalid_evidence = (
        {"kind": cast("AlphaReviewEvidenceKind", "source-after")},
        {"node_id": "bad id"},
        {"artifact_digest": "not-a-digest"},
        {"excerpt": "contains\x00nul"},
        {"excerpt": "x" * (32 * 1024 + 1)},
        {"start_line": True},
        {"start_line": 0},
        {"check_id": "bad id"},
        {"path": None},
        {"kind": AlphaReviewEvidenceKind.CHECK_RESULT, "path": None, "check_id": None},
        {"kind": AlphaReviewEvidenceKind.OUTCOME, "path": "src/value.py"},
        {"start_line": 10_000_000, "excerpt": "first\nsecond\n"},
        {"path": "."},
        {"path": "/absolute"},
    )
    for replacement in invalid_evidence:
        with pytest.raises(AlphaReviewContractError) as caught:
            replace(evidence, **replacement)
        assert caught.value.code is AlphaReviewContractFailureCode.INVALID_CONTEXT

    unknown_node = replace(evidence, node_id="node-unknown")
    outside_scope = replace(evidence, path="tests/value.py")
    check_evidence = AlphaReviewEvidence(
        kind=AlphaReviewEvidenceKind.CHECK_RESULT,
        node_id="node-1",
        artifact_digest=DIGEST_C,
        excerpt="exit_code=0\n",
        start_line=1,
        check_id="unknown-check",
    )
    invalid_contexts = (
        {"schema_version": "alpha-review-context/v2"},
        {"acceptance": cast("AlphaReviewAcceptance", object())},
        {"state_digest": "not-a-digest"},
        {"artifact_evidence_digest": "not-a-digest"},
        {"evidence": cast("tuple[AlphaReviewEvidence, ...]", [evidence])},
        {"evidence": ()},
        {"evidence": cast("tuple[AlphaReviewEvidence, ...]", ("not-evidence",))},
        {"evidence": (evidence, evidence)},
        {"evidence": (unknown_node,)},
        {"evidence": (outside_scope,)},
        {"evidence": (check_evidence,)},
    )
    for replacement in invalid_contexts:
        with pytest.raises(AlphaReviewContractError) as caught:
            replace(context, **replacement)
        assert caught.value.code is AlphaReviewContractFailureCode.INVALID_CONTEXT


def test_review_proposal_provider_and_admission_boundaries_reject_wrong_types() -> None:
    context = review_context()
    evidence = context.evidence[0]
    citation = AlphaReviewCitation(evidence.evidence_id, evidence.start_line, evidence.end_line)
    finding = _finding("finding-1", citation)
    proposal = AlphaReviewProposal(context.digest, (finding,), "One bounded finding.")

    for replacement in (
        {"evidence_id": "not-a-digest"},
        {"start_line": True},
        {"start_line": 0},
        {"end_line": cast("int", True)},
        {"end_line": citation.start_line - 1},
    ):
        with pytest.raises(AlphaReviewContractError) as caught:
            replace(citation, **replacement)
        assert caught.value.code is AlphaReviewContractFailureCode.INVALID_PROPOSAL

    invalid_findings = (
        {"finding_id": "bad id"},
        {"category": cast("AlphaReviewFindingCategory", "correctness")},
        {"severity": cast("AlphaReviewSeverity", "high")},
        {"claim": ""},
        {"citations": cast("tuple[AlphaReviewCitation, ...]", [citation])},
        {"citations": ()},
        {"citations": cast("tuple[AlphaReviewCitation, ...]", ("not-a-citation",))},
    )
    for replacement in invalid_findings:
        with pytest.raises(AlphaReviewContractError) as caught:
            replace(finding, **replacement)
        assert caught.value.code is AlphaReviewContractFailureCode.INVALID_PROPOSAL

    invalid_proposals = (
        {"schema_version": "alpha-review-proposal/v2"},
        {"context_digest": "not-a-digest"},
        {"findings": cast("tuple[AlphaProposedReviewFinding, ...]", [finding])},
        {"findings": cast("tuple[AlphaProposedReviewFinding, ...]", ("not-a-finding",))},
        {"summary": ""},
    )
    for replacement in invalid_proposals:
        with pytest.raises(AlphaReviewContractError) as caught:
            replace(proposal, **replacement)
        assert caught.value.code is AlphaReviewContractFailureCode.INVALID_PROPOSAL

    admitted = admit_alpha_review(context, proposal)
    for replacement in (
        {"schema_version": "alpha-admitted-review/v2"},
        {"context_digest": "not-a-digest"},
        {"acceptance_digest": "not-a-digest"},
        {"findings": cast("tuple[AlphaProposedReviewFinding, ...]", [finding])},
        {"summary": ""},
    ):
        with pytest.raises(AlphaReviewContractError) as caught:
            replace(admitted, **replacement)
        assert caught.value.code is AlphaReviewContractFailureCode.ADMISSION_REJECTED

    call = AlphaReviewProviderCall(
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
        {"context": cast("AlphaReviewContext", object())},
        {"classification": cast("DataClassification", "private")},
        {"locality": cast("LocalityPolicy", "remote-allowed")},
        {"budget": cast("GatewayBudget", object())},
        {"estimated_input_tokens": True},
        {"estimated_input_tokens": -1},
        {"causation_id": "bad id"},
    )
    for replacement in invalid_calls:
        with pytest.raises(AlphaReviewContractError) as caught:
            replace(call, **replacement)
        assert caught.value.code is AlphaReviewContractFailureCode.INVALID_CONTEXT

    provider = AlphaReviewProviderResult(
        proposal=proposal,
        provider_output_digest=DIGEST_D,
        profile_id="alpha-review",
        adapter_id="codex-cli",
        model_id="gpt-review",
        input_tokens=10,
        output_tokens=5,
        latency_ms=100,
        cost_microusd=0,
        completed_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    invalid_results = (
        {"schema_version": "alpha-review-provider-result/v2"},
        {"proposal": cast("AlphaReviewProposal", object())},
        {"provider_output_digest": "not-a-digest"},
        {"profile_id": ""},
        {"input_tokens": True},
        {"output_tokens": -1},
        {"completed_at": cast("datetime", object())},
        {"completed_at": datetime(2026, 7, 22)},
    )
    for replacement in invalid_results:
        with pytest.raises(AlphaReviewContractError) as caught:
            replace(provider, **replacement)
        assert caught.value.code is AlphaReviewContractFailureCode.INVALID_PROPOSAL

    with pytest.raises(AlphaReviewContractError) as wrong_admission:
        admit_alpha_review(cast("AlphaReviewContext", object()), proposal)
    assert wrong_admission.value.code is AlphaReviewContractFailureCode.ADMISSION_REJECTED


def test_review_serializers_and_parsers_reject_malformed_persisted_shapes() -> None:
    context = review_context()
    proposal = alpha_review_proposal_from_mapping(review_output(context))
    admitted = admit_alpha_review(context, proposal)
    provider = AlphaReviewProviderResult(
        proposal=proposal,
        provider_output_digest=DIGEST_D,
        profile_id="alpha-review",
        adapter_id="codex-cli",
        model_id="gpt-review",
        input_tokens=10,
        output_tokens=5,
        latency_ms=100,
        cost_microusd=0,
        completed_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    serializers = (
        (alpha_review_acceptance_payload, AlphaReviewContractFailureCode.INVALID_CONTEXT),
        (alpha_review_context_payload, AlphaReviewContractFailureCode.INVALID_CONTEXT),
        (alpha_review_proposal_payload, AlphaReviewContractFailureCode.INVALID_PROPOSAL),
        (alpha_review_provider_result_payload, AlphaReviewContractFailureCode.INVALID_PROPOSAL),
        (alpha_admitted_review_payload, AlphaReviewContractFailureCode.ADMISSION_REJECTED),
    )
    for serializer, code in serializers:
        with pytest.raises(AlphaReviewContractError) as caught:
            serializer(object())  # ty: ignore[invalid-argument-type]
        assert caught.value.code is code

    admitted_payload = alpha_admitted_review_payload(admitted)
    admitted_variants = (
        [],
        {**admitted_payload, "schema_version": "alpha-admitted-review/v2"},
        {**admitted_payload, "summary": 1},
        {**admitted_payload, "findings": "not-an-array"},
    )
    for value in admitted_variants:
        with pytest.raises(AlphaReviewContractError):
            alpha_admitted_review_from_mapping(cast("dict[str, object]", value))

    provider_payload = alpha_review_provider_result_payload(provider)
    provider_variants = (
        {**provider_payload, "schema_version": "alpha-review-provider-result/v2"},
        {**provider_payload, "completed_at": "not-a-time"},
        {**provider_payload, "profile_id": 1},
        {**provider_payload, "input_tokens": True},
    )
    for value in provider_variants:
        with pytest.raises(AlphaReviewContractError):
            alpha_review_provider_result_from_mapping(value, proposal=proposal)

    output = review_output(context)
    finding = cast("list[dict[str, JsonInput]]", output["findings"])[0]
    citation = cast("list[dict[str, JsonInput]]", finding["citations"])[0]
    proposal_variants: list[object] = [
        [],
        {**output, "schema_version": "alpha-review-proposal/v2"},
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
        with pytest.raises(AlphaReviewContractError):
            alpha_review_proposal_from_mapping(cast("dict[str, object]", value))


def review_context() -> AlphaReviewContext:
    check = AlphaReviewCheck(
        check_id="unit-check",
        argv=("python", "-m", "pytest", "tests/unit/test_value.py::test_value"),
        expected_exit_code=0,
        command_digest=DIGEST_A,
        result_digest=DIGEST_B,
        passed=True,
    )
    node = AlphaReviewPlanNode(
        node_id="node-1",
        objective="Update the bounded value and prove its exact acceptance check.",
        depends_on=(),
        effects=("repository-read", "repository-write", "process"),
        allowed_paths=("src",),
        max_changed_files=2,
        checks=(check,),
    )
    acceptance = AlphaReviewAcceptance(
        run_id="run-1",
        project_id="project-1",
        intent_id="intent-1",
        plan_id="plan-1",
        objective="Produce a reviewable bounded change.",
        constraints=("Do not weaken acceptance.", "Stay inside the declared scope."),
        base_commit="a" * 40,
        nodes=(node,),
    )
    evidence = AlphaReviewEvidence(
        kind=AlphaReviewEvidenceKind.SOURCE_AFTER,
        node_id=node.node_id,
        artifact_digest=DIGEST_C,
        path="src/value.py",
        start_line=10,
        excerpt="VALUE = 2\nassert VALUE == 2\n",
    )
    return AlphaReviewContext(
        acceptance=acceptance,
        state_digest=DIGEST_D,
        artifact_evidence_digest=DIGEST_E,
        evidence=(evidence,),
    )


def review_output(context: AlphaReviewContext) -> dict[str, JsonInput]:
    evidence = context.evidence[0]
    proposal = AlphaReviewProposal(
        context_digest=context.digest,
        findings=(
            _finding(
                "finding-1",
                AlphaReviewCitation(
                    evidence.evidence_id,
                    evidence.start_line,
                    evidence.end_line,
                ),
            ),
        ),
        summary="One cited proposal for host admission.",
    )
    return alpha_review_proposal_payload(proposal)


def _finding(finding_id: str, citation: AlphaReviewCitation) -> AlphaProposedReviewFinding:
    return AlphaProposedReviewFinding(
        finding_id=finding_id,
        category=AlphaReviewFindingCategory.CORRECTNESS,
        severity=AlphaReviewSeverity.HIGH,
        claim="The cited result contradicts the bounded objective.",
        impact="The node may not satisfy its immutable acceptance contract.",
        recommendation="Correct the implementation without changing acceptance.",
        citations=(citation,),
    )
