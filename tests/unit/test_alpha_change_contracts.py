from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest

from blackcell.gateway import DataClassification, GatewayBudget, LocalityPolicy
from blackcell.kernel._json import bytes_digest
from blackcell.orchestration.alpha_changes import (
    ALPHA_CHANGE_CONTEXT_SCHEMA,
    ALPHA_CHANGE_PROPOSAL_SCHEMA_VERSION,
    AlphaChangeContext,
    AlphaChangeContractError,
    AlphaChangeContractFailureCode,
    AlphaChangeProposal,
    AlphaChangeProviderCall,
    AlphaChangeProviderResult,
    AlphaEvidenceFile,
    AlphaFileChange,
    AlphaTextOperation,
    alpha_change_context_payload,
    alpha_change_proposal_from_mapping,
    alpha_change_proposal_payload,
    alpha_change_provider_result_payload,
)


def test_evidence_context_is_bounded_content_addressed_and_authority_free() -> None:
    first = _evidence_file("src/a.py", "VALUE = 1\n")
    second = _evidence_file("README.md", "fixture\n")
    context = AlphaChangeContext(
        objective="Add a bounded value.",
        constraints=("Do not alter public behavior.",),
        base_commit="a" * 40,
        allowed_paths=("src",),
        max_changed_paths=2,
        files=(first, second),
    )

    payload = alpha_change_context_payload(context)

    assert context.schema_version == ALPHA_CHANGE_CONTEXT_SCHEMA
    assert tuple(item.path for item in context.files) == ("README.md", "src/a.py")
    assert context.digest.startswith("sha256:") and len(context.digest) == 71
    assert payload["files"] == [
        {
            "path": "README.md",
            "content": "fixture\n",
            "content_digest": bytes_digest(b"fixture\n"),
        },
        {
            "path": "src/a.py",
            "content": "VALUE = 1\n",
            "content_digest": bytes_digest(b"VALUE = 1\n"),
        },
    ]
    serialized = repr(payload)
    assert "repository_root" not in serialized
    assert "worktree" not in serialized
    assert "executable" not in serialized
    assert "credential" not in serialized
    assert "network" not in serialized
    assert "executor" not in serialized
    assert "VALUE = 1" not in repr(first)

    with pytest.raises(AlphaChangeContractError) as wrong_digest:
        AlphaEvidenceFile("src/a.py", "VALUE = 1\n", "sha256:" + "0" * 64)
    assert wrong_digest.value.code is AlphaChangeContractFailureCode.INVALID_EVIDENCE
    for path in (".git/config", "src/.git/config"):
        with pytest.raises(AlphaChangeContractError) as metadata:
            _evidence_file(path, "forbidden\n")
        assert metadata.value.code is AlphaChangeContractFailureCode.INVALID_EVIDENCE
    with pytest.raises(AlphaChangeContractError) as oversized:
        _evidence_file("src/large.txt", "x" * (256 * 1024 + 1))
    assert oversized.value.code is AlphaChangeContractFailureCode.INVALID_EVIDENCE
    with pytest.raises(AlphaChangeContractError) as wrong_schema:
        replace(context, schema_version="alpha-change-context/v2")
    assert wrong_schema.value.code is AlphaChangeContractFailureCode.INVALID_EVIDENCE


def test_change_proposal_parser_is_closed_and_binds_operation_semantics() -> None:
    old_digest = bytes_digest(b"old\n")
    mapping = {
        "schema_version": ALPHA_CHANGE_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": "proposal-1",
        "evidence_digest": "sha256:" + "1" * 64,
        "operations": [
            {
                "operation": "replace",
                "path": "src/z.py",
                "expected_digest": old_digest,
                "content": "new\n",
            },
            {
                "operation": "create",
                "path": "src/a.py",
                "expected_digest": None,
                "content": "created\n",
            },
        ],
        "summary": "Create and replace two bounded files.",
    }

    proposal = alpha_change_proposal_from_mapping(mapping)

    assert proposal.schema_version == ALPHA_CHANGE_PROPOSAL_SCHEMA_VERSION
    assert tuple(item.path for item in proposal.operations) == ("src/a.py", "src/z.py")
    assert proposal.operations[0].operation is AlphaTextOperation.CREATE
    assert proposal.operations[0].content_digest == bytes_digest(b"created\n")
    assert proposal.operations[1].expected_digest == old_digest
    assert proposal.digest.startswith("sha256:")
    assert "created" not in repr(proposal.operations[0])

    invalid_values = (
        {**mapping, "unexpected": True},
        {**mapping, "operations": [{**mapping["operations"][0], "unexpected": True}]},
        {
            **mapping,
            "operations": [
                {
                    "operation": "create",
                    "path": ".git/config",
                    "expected_digest": None,
                    "content": "forbidden",
                }
            ],
        },
        {
            **mapping,
            "operations": [
                {
                    "operation": "create",
                    "path": "src/.git/config",
                    "expected_digest": None,
                    "content": "forbidden",
                }
            ],
        },
        {
            **mapping,
            "operations": [
                {
                    "operation": "replace",
                    "path": "src/a.py",
                    "expected_digest": None,
                    "content": "missing digest",
                }
            ],
        },
        {
            **mapping,
            "operations": [
                {
                    "operation": "delete",
                    "path": "src/a.py",
                    "expected_digest": old_digest,
                    "content": "not allowed",
                }
            ],
        },
        {
            **mapping,
            "operations": [
                {
                    "operation": "replace",
                    "path": "src/a.py",
                    "expected_digest": old_digest,
                    "content": "old\n",
                }
            ],
        },
        {
            **mapping,
            "operations": [
                mapping["operations"][0],
                mapping["operations"][0],
            ],
        },
    )
    for value in invalid_values:
        with pytest.raises(AlphaChangeContractError) as caught:
            alpha_change_proposal_from_mapping(value)
        assert caught.value.code is AlphaChangeContractFailureCode.INVALID_PROPOSAL

    with pytest.raises(AlphaChangeContractError) as raw_operation:
        AlphaFileChange(
            "execute",  # ty: ignore[invalid-argument-type]
            "src/a.py",
            None,
            "content",
        )
    assert raw_operation.value.code is AlphaChangeContractFailureCode.INVALID_PROPOSAL


def test_change_context_rejects_ambiguous_scope_and_unbounded_evidence() -> None:
    first = _evidence_file("src/a.py", "VALUE = 1\n")
    context = AlphaChangeContext(
        objective="Apply one bounded change.",
        constraints=("Preserve behavior.",),
        base_commit="a" * 40,
        allowed_paths=("src",),
        max_changed_paths=2,
        files=(first,),
    )
    invalid_replacements = (
        {"objective": " \t"},
        {"constraints": cast("tuple[str, ...]", ["not-a-tuple"])},
        {"constraints": ("duplicate", "duplicate")},
        {"base_commit": "not-a-commit"},
        {"allowed_paths": cast("tuple[str, ...]", ["src"])},
        {"allowed_paths": ("src", "src")},
        {"max_changed_paths": True},
        {"max_changed_paths": -1},
        {"files": cast("tuple[AlphaEvidenceFile, ...]", [first])},
        {"files": cast("tuple[AlphaEvidenceFile, ...]", ("not-evidence",))},
        {"files": (first, first)},
    )
    for replacement in invalid_replacements:
        with pytest.raises(AlphaChangeContractError) as caught:
            replace(context, **replacement)
        assert caught.value.code is AlphaChangeContractFailureCode.INVALID_EVIDENCE

    large_files = tuple(
        _evidence_file(f"evidence/{index}.txt", "x" * (256 * 1024)) for index in range(5)
    )
    with pytest.raises(AlphaChangeContractError) as aggregate_limit:
        replace(context, files=large_files)
    assert aggregate_limit.value.code is AlphaChangeContractFailureCode.INVALID_EVIDENCE

    assert replace(context, allowed_paths=(".",)).allowed_paths == (".",)
    invalid_paths = ("", "/absolute", "src/../escape", "src\\value.py", "src/\x00value")
    for path in invalid_paths:
        with pytest.raises(AlphaChangeContractError) as invalid_path:
            replace(context, allowed_paths=(path,))
        assert invalid_path.value.code is AlphaChangeContractFailureCode.INVALID_EVIDENCE


def test_change_proposal_objects_enforce_bounded_inert_operations() -> None:
    digest = bytes_digest(b"old\n")
    valid = AlphaFileChange(AlphaTextOperation.REPLACE, "src/value.py", digest, "new\n")
    deleted = AlphaFileChange(AlphaTextOperation.DELETE, "src/old.py", digest, None)
    assert deleted.content_digest is None

    invalid_operations = (
        (AlphaTextOperation.CREATE, "src/value.py", digest, "new\n"),
        (AlphaTextOperation.CREATE, "src/value.py", None, None),
        (AlphaTextOperation.REPLACE, "src/value.py", None, "new\n"),
        (AlphaTextOperation.DELETE, "src/value.py", None, None),
        (AlphaTextOperation.DELETE, "src/value.py", digest, "content"),
        (AlphaTextOperation.CREATE, "src/value.py", None, "x" * (1024 * 1024 + 1)),
    )
    for operation, path, expected_digest, content in invalid_operations:
        with pytest.raises(AlphaChangeContractError) as caught:
            AlphaFileChange(operation, path, expected_digest, content)
        assert caught.value.code is AlphaChangeContractFailureCode.INVALID_PROPOSAL

    proposal = AlphaChangeProposal(
        proposal_id="proposal-1",
        evidence_digest="sha256:" + "a" * 64,
        operations=(valid,),
        summary="Replace one bounded file.",
    )
    invalid_proposals = (
        {"schema_version": "alpha-change-proposal/v2"},
        {"proposal_id": "bad id"},
        {"evidence_digest": "not-a-digest"},
        {"operations": cast("tuple[AlphaFileChange, ...]", [])},
        {"operations": ()},
        {"operations": cast("tuple[AlphaFileChange, ...]", ("not-an-operation",))},
        {"operations": (valid, valid)},
        {"summary": ""},
        {"summary": "x" * 4097},
    )
    for replacement in invalid_proposals:
        with pytest.raises(AlphaChangeContractError) as caught:
            replace(proposal, **replacement)
        assert caught.value.code is AlphaChangeContractFailureCode.INVALID_PROPOSAL

    large_operations = tuple(
        AlphaFileChange(
            AlphaTextOperation.CREATE,
            f"src/generated-{index}.txt",
            None,
            "x" * (1024 * 1024),
        )
        for index in range(5)
    )
    with pytest.raises(AlphaChangeContractError) as aggregate_limit:
        replace(proposal, operations=large_operations)
    assert aggregate_limit.value.code is AlphaChangeContractFailureCode.INVALID_PROPOSAL


def test_change_provider_call_and_result_are_strict_content_free_boundaries() -> None:
    context = AlphaChangeContext(
        objective="Apply one bounded change.",
        constraints=(),
        base_commit="a" * 40,
        allowed_paths=("src",),
        max_changed_paths=1,
        files=(_evidence_file("src/value.py", "VALUE = 1\n"),),
    )
    call = AlphaChangeProviderCall(
        request_id="request-1",
        correlation_id="correlation-1",
        run_id="run-1",
        node_id="node-1",
        context=context,
        classification=DataClassification.PRIVATE,
        locality=LocalityPolicy.REMOTE_ALLOWED,
        budget=GatewayBudget(100, 50, 1_000, 0),
        estimated_input_tokens=10,
        causation_id="event-1",
    )
    assert call.context is context
    invalid_calls = (
        {"request_id": ""},
        {"correlation_id": "x" * 257},
        {"causation_id": " "},
        {"context": cast("AlphaChangeContext", object())},
        {"classification": cast("DataClassification", "private")},
        {"locality": cast("LocalityPolicy", "remote-allowed")},
        {"budget": cast("GatewayBudget", object())},
        {"estimated_input_tokens": True},
        {"estimated_input_tokens": -1},
    )
    for replacement in invalid_calls:
        with pytest.raises(AlphaChangeContractError) as caught:
            replace(call, **replacement)
        assert caught.value.code is AlphaChangeContractFailureCode.INVALID_EVIDENCE

    proposal = AlphaChangeProposal(
        proposal_id="proposal-1",
        evidence_digest=context.digest,
        operations=(AlphaFileChange(AlphaTextOperation.CREATE, "src/new.py", None, "NEW = 1\n"),),
        summary="Create one bounded file.",
    )
    result = AlphaChangeProviderResult(
        proposal=proposal,
        provider_output_digest="sha256:" + "b" * 64,
        profile_id="alpha-code",
        adapter_id="codex-cli",
        model_id="gpt-alpha",
        input_tokens=10,
        output_tokens=5,
        latency_ms=100,
        cost_microusd=0,
        completed_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    assert alpha_change_provider_result_payload(result)["proposal_digest"] == proposal.digest
    invalid_results = (
        {"schema_version": "alpha-change-provider-result/v2"},
        {"proposal": cast("AlphaChangeProposal", object())},
        {"provider_output_digest": "not-a-digest"},
        {"profile_id": ""},
        {"adapter_id": "x" * 257},
        {"input_tokens": True},
        {"output_tokens": -1},
        {"completed_at": datetime(2026, 7, 22)},
    )
    for replacement in invalid_results:
        with pytest.raises(AlphaChangeContractError) as caught:
            replace(result, **replacement)
        assert caught.value.code is AlphaChangeContractFailureCode.INVALID_PROPOSAL

    for serializer, value, code in (
        (alpha_change_context_payload, object(), AlphaChangeContractFailureCode.INVALID_EVIDENCE),
        (alpha_change_proposal_payload, object(), AlphaChangeContractFailureCode.INVALID_PROPOSAL),
        (
            alpha_change_provider_result_payload,
            object(),
            AlphaChangeContractFailureCode.INVALID_PROPOSAL,
        ),
    ):
        with pytest.raises(AlphaChangeContractError) as caught:
            serializer(value)  # ty: ignore[invalid-argument-type]
        assert caught.value.code is code


def test_change_proposal_parser_rejects_wrong_container_and_scalar_types() -> None:
    valid: dict[str, object] = {
        "schema_version": ALPHA_CHANGE_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": "proposal-1",
        "evidence_digest": "sha256:" + "a" * 64,
        "operations": [
            {
                "operation": "create",
                "path": "src/new.py",
                "expected_digest": None,
                "content": "NEW = 1\n",
            }
        ],
        "summary": "Create one bounded file.",
    }
    variants: list[object] = [
        [],
        {**valid, "operations": "not-an-array"},
        {**valid, "operations": []},
        {**valid, "operations": ["not-an-object"]},
        {**valid, "operations": [{"operation": "create"}]},
    ]
    operation = cast("list[dict[str, object]]", valid["operations"])[0]
    variants.extend(
        (
            {**valid, "operations": [{**operation, "operation": "execute"}]},
            {**valid, "operations": [{**operation, "path": 1}]},
            {**valid, "operations": [{**operation, "expected_digest": 1}]},
            {**valid, "operations": [{**operation, "content": 1}]},
            {**valid, "summary": 1},
        )
    )
    for variant in variants:
        with pytest.raises(AlphaChangeContractError) as caught:
            alpha_change_proposal_from_mapping(cast("dict[str, object]", variant))
        assert caught.value.code is AlphaChangeContractFailureCode.INVALID_PROPOSAL


def _evidence_file(path: str, content: str) -> AlphaEvidenceFile:
    return AlphaEvidenceFile(path, content, bytes_digest(content.encode("utf-8")))
