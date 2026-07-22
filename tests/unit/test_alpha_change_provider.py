from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

import pytest

from blackcell.adapters.models.alpha_change_provider import (
    AlphaChangeProviderError,
    AlphaChangeProviderFailureCode,
    GatewayAlphaChangeProvider,
)
from blackcell.gateway import (
    DataClassification,
    GatewayBudget,
    GatewayResult,
    LocalityPolicy,
    ModelCapability,
    ModelRequest,
    ModelResponse,
    RoutingDecision,
)
from blackcell.kernel import JsonInput, JsonValue
from blackcell.kernel._json import bytes_digest, freeze_json
from blackcell.orchestration.alpha_changes import (
    ALPHA_CHANGE_PROPOSAL_SCHEMA_VERSION,
    AlphaChangeContext,
    AlphaChangeProviderCall,
    AlphaEvidenceFile,
)

NOW = datetime(2026, 7, 22, 18, tzinfo=UTC)


class RecordingGateway:
    def __init__(self, output: dict[str, JsonInput]) -> None:
        self.output = cast("Mapping[str, JsonValue]", freeze_json(output))
        self.requests: list[ModelRequest] = []

    def invoke(self, request: ModelRequest) -> GatewayResult:
        self.requests.append(request)
        decision = RoutingDecision(
            "alpha-code",
            "codex-cli",
            "gpt-test",
            ModelCapability.CODE,
            False,
            False,
        )
        return GatewayResult(
            decision,
            ModelResponse(
                request_id=request.request_id,
                output=self.output,
                profile_id=decision.profile_id,
                adapter_id=decision.adapter_id,
                model_id=decision.model_id,
                input_tokens=40,
                output_tokens=12,
                latency_ms=100,
                cost_microusd=5,
                deterministic=False,
                completed_at=NOW,
            ),
        )


def test_gateway_provider_uses_code_capability_closed_schema_and_no_tools() -> None:
    context = _context()
    gateway = RecordingGateway(_output(context.digest))
    provider = GatewayAlphaChangeProvider(gateway)

    result = provider.propose(_call(context))

    assert result.proposal.evidence_digest == context.digest
    assert result.proposal.operations[0].path == "src/value.py"
    assert result.profile_id == "alpha-code"
    assert result.adapter_id == "codex-cli"
    assert result.model_id == "gpt-test"
    assert (result.input_tokens, result.output_tokens, result.latency_ms) == (40, 12, 100)
    assert result.provider_output_digest.startswith("sha256:")

    request = gateway.requests[0]
    assert request.capability is ModelCapability.CODE
    assert request.classification is DataClassification.PRIVATE
    assert request.locality is LocalityPolicy.REMOTE_ALLOWED
    assert request.budget == GatewayBudget(1_000, 200, 5_000, 1_000)
    assert request.estimated_input_tokens == 500
    assert request.tools_allowed is False
    assert request.deterministic_required is False
    assert request.input["schema_version"] == "alpha-change-context/v1"
    assert request.input["base_commit"] == "a" * 40
    assert request.output_schema["additionalProperties"] is False
    raw_properties = request.output_schema["properties"]
    assert isinstance(raw_properties, Mapping)
    properties = cast("Mapping[str, JsonValue]", raw_properties)
    raw_operations = properties["operations"]
    assert isinstance(raw_operations, Mapping)
    operations = cast("Mapping[str, JsonValue]", raw_operations)
    assert operations["minItems"] == 1
    assert operations["maxItems"] == 256
    raw_operation_items = operations["items"]
    assert isinstance(raw_operation_items, Mapping)
    operation_items = cast("Mapping[str, JsonValue]", raw_operation_items)
    assert operation_items["additionalProperties"] is False
    serialized_input = repr(dict(request.input))
    for forbidden in (
        "repository_root",
        "worktree",
        "executable",
        "credential",
        "secret",
        "network",
        "executor",
    ):
        assert forbidden not in serialized_input


def test_gateway_provider_rejects_evidence_mismatch_and_invalid_output() -> None:
    context = _context()
    mismatch = GatewayAlphaChangeProvider(RecordingGateway(_output("sha256:" + "9" * 64)))
    with pytest.raises(AlphaChangeProviderError) as mismatched:
        mismatch.propose(_call(context))
    assert mismatched.value.code is AlphaChangeProviderFailureCode.EVIDENCE_MISMATCH
    assert context.objective not in str(mismatched.value)

    malformed = _output(context.digest)
    malformed["unexpected"] = True
    with pytest.raises(AlphaChangeProviderError) as invalid:
        GatewayAlphaChangeProvider(RecordingGateway(malformed)).propose(_call(context))
    assert invalid.value.code is AlphaChangeProviderFailureCode.INVALID_PROPOSAL


def _context() -> AlphaChangeContext:
    content = "VALUE = 1\n"
    return AlphaChangeContext(
        objective="Update the bounded value.",
        constraints=("Only edit src/value.py.",),
        base_commit="a" * 40,
        allowed_paths=("src/value.py",),
        max_changed_paths=1,
        files=(
            AlphaEvidenceFile(
                "src/value.py",
                content,
                bytes_digest(content.encode("utf-8")),
            ),
        ),
    )


def _call(context: AlphaChangeContext) -> AlphaChangeProviderCall:
    return AlphaChangeProviderCall(
        request_id="request-1",
        correlation_id="correlation-1",
        run_id="run-1",
        node_id="node-1",
        context=context,
        classification=DataClassification.PRIVATE,
        locality=LocalityPolicy.REMOTE_ALLOWED,
        budget=GatewayBudget(1_000, 200, 5_000, 1_000),
        estimated_input_tokens=500,
        causation_id="event-1",
    )


def _output(evidence_digest: str) -> dict[str, JsonInput]:
    return {
        "schema_version": ALPHA_CHANGE_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": "proposal-1",
        "evidence_digest": evidence_digest,
        "operations": [
            {
                "operation": "replace",
                "path": "src/value.py",
                "expected_digest": bytes_digest(b"VALUE = 1\n"),
                "content": "VALUE = 2\n",
            }
        ],
        "summary": "Update the bounded value.",
    }
