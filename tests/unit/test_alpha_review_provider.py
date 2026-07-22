from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import cast

import pytest

from blackcell.adapters.models.alpha_review_provider import (
    AlphaReviewProviderError,
    AlphaReviewProviderFailureCode,
    GatewayAlphaReviewer,
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
from blackcell.kernel._json import freeze_json, json_digest
from blackcell.orchestration.alpha_review import (
    AlphaAdmittedReview,
    AlphaReviewContext,
    AlphaReviewProviderCall,
    alpha_review_context_payload,
)
from tests.unit.test_alpha_review_contracts import review_context, review_output

NOW = datetime(2026, 7, 22, 18, tzinfo=UTC)


class RecordingGateway:
    def __init__(
        self,
        output: dict[str, JsonInput],
        *,
        capability: ModelCapability = ModelCapability.REVIEW,
        response_profile_id: str | None = None,
    ) -> None:
        self.output = cast("Mapping[str, JsonValue]", freeze_json(output))
        self.capability = capability
        self.response_profile_id = response_profile_id
        self.requests: list[ModelRequest] = []

    def invoke(self, request: ModelRequest) -> GatewayResult:
        self.requests.append(request)
        decision = RoutingDecision(
            "alpha-review",
            "codex-cli",
            "gpt-test",
            self.capability,
            False,
            False,
        )
        return GatewayResult(
            decision,
            ModelResponse(
                request_id=request.request_id,
                output=self.output,
                profile_id=self.response_profile_id or decision.profile_id,
                adapter_id=decision.adapter_id,
                model_id=decision.model_id,
                input_tokens=80,
                output_tokens=24,
                latency_ms=150,
                cost_microusd=9,
                deterministic=False,
                completed_at=NOW,
            ),
        )


def test_gateway_reviewer_uses_review_capability_closed_schema_and_no_tools() -> None:
    context = review_context()
    gateway = RecordingGateway(review_output(context))

    result = GatewayAlphaReviewer(gateway).review(_call(context))

    assert result.proposal.context_digest == context.digest
    assert result.proposal.findings[0].finding_id == "finding-1"
    assert not isinstance(result.proposal, AlphaAdmittedReview)
    assert not hasattr(result.proposal, "acceptance_digest")
    assert result.profile_id == "alpha-review"
    assert result.adapter_id == "codex-cli"
    assert result.model_id == "gpt-test"
    assert (result.input_tokens, result.output_tokens, result.latency_ms) == (80, 24, 150)
    assert result.provider_output_digest.startswith("sha256:")

    request = gateway.requests[0]
    assert request.capability is ModelCapability.REVIEW
    assert request.classification is DataClassification.PRIVATE
    assert request.locality is LocalityPolicy.REMOTE_ALLOWED
    assert request.budget == GatewayBudget(2_000, 500, 10_000, 2_000)
    assert request.estimated_input_tokens == 750
    assert request.run_id == context.acceptance.run_id
    assert request.node_id == "review-1"
    assert request.tools_allowed is False
    assert request.deterministic_required is False
    assert json_digest(request.input) == json_digest(alpha_review_context_payload(context))
    assert request.output_schema["additionalProperties"] is False
    raw_properties = request.output_schema["properties"]
    assert isinstance(raw_properties, Mapping)
    assert "admitted" not in raw_properties
    assert "acceptance_digest" not in raw_properties


def test_gateway_reviewer_rejects_wrong_capability_identity_and_malformed_output() -> None:
    context = review_context()
    call = _call(context)

    wrong_capability = GatewayAlphaReviewer(
        RecordingGateway(review_output(context), capability=ModelCapability.CODE)
    )
    with pytest.raises(AlphaReviewProviderError) as capability_error:
        wrong_capability.review(call)
    assert capability_error.value.code is AlphaReviewProviderFailureCode.INVALID_GATEWAY_RESULT

    identity_mismatch = GatewayAlphaReviewer(
        RecordingGateway(review_output(context), response_profile_id="different-profile")
    )
    with pytest.raises(AlphaReviewProviderError) as identity_error:
        identity_mismatch.review(call)
    assert identity_error.value.code is AlphaReviewProviderFailureCode.INVALID_GATEWAY_RESULT

    malformed = review_output(context)
    malformed["admitted"] = True
    with pytest.raises(AlphaReviewProviderError) as proposal_error:
        GatewayAlphaReviewer(RecordingGateway(malformed)).review(call)
    assert proposal_error.value.code is AlphaReviewProviderFailureCode.INVALID_PROPOSAL


def test_gateway_reviewer_rejects_context_mismatch_before_host_admission() -> None:
    context = review_context()
    mismatch = deepcopy(review_output(context))
    mismatch["context_digest"] = "sha256:" + "9" * 64

    with pytest.raises(AlphaReviewProviderError) as error:
        GatewayAlphaReviewer(RecordingGateway(mismatch)).review(_call(context))

    assert error.value.code is AlphaReviewProviderFailureCode.CONTEXT_MISMATCH
    assert context.acceptance.objective not in str(error.value)


def _call(context: AlphaReviewContext) -> AlphaReviewProviderCall:
    return AlphaReviewProviderCall(
        request_id="request-1",
        correlation_id="correlation-1",
        review_id="review-1",
        context=context,
        classification=DataClassification.PRIVATE,
        locality=LocalityPolicy.REMOTE_ALLOWED,
        budget=GatewayBudget(2_000, 500, 10_000, 2_000),
        estimated_input_tokens=750,
        causation_id="event-1",
    )
