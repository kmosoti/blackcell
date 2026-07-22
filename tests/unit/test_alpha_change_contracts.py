from __future__ import annotations

from dataclasses import replace

import pytest

from blackcell.kernel._json import bytes_digest
from blackcell.orchestration.alpha_changes import (
    ALPHA_CHANGE_CONTEXT_SCHEMA,
    ALPHA_CHANGE_PROPOSAL_SCHEMA_VERSION,
    AlphaChangeContext,
    AlphaChangeContractError,
    AlphaChangeContractFailureCode,
    AlphaEvidenceFile,
    AlphaFileChange,
    AlphaTextOperation,
    alpha_change_context_payload,
    alpha_change_proposal_from_mapping,
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


def _evidence_file(path: str, content: str) -> AlphaEvidenceFile:
    return AlphaEvidenceFile(path, content, bytes_digest(content.encode("utf-8")))
