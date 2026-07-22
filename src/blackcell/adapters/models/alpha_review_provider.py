"""Gateway-backed, proposal-only reviewer for alpha execution evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from blackcell.gateway import GatewayResult, ModelCapability, ModelRequest
from blackcell.kernel import JsonValue
from blackcell.kernel._json import json_digest
from blackcell.orchestration.alpha_review import (
    ALPHA_REVIEW_PROPOSAL_OUTPUT_SCHEMA,
    AlphaReviewContractError,
    AlphaReviewProviderCall,
    AlphaReviewProviderResult,
    alpha_review_context_payload,
    alpha_review_proposal_from_mapping,
)


class AlphaReviewProviderFailureCode(StrEnum):
    INVALID_GATEWAY_RESULT = "invalid-alpha-review-gateway-result"
    INVALID_PROPOSAL = "invalid-alpha-review-provider-proposal"
    CONTEXT_MISMATCH = "alpha-review-provider-context-mismatch"


class AlphaReviewProviderError(RuntimeError):
    """A content-free failure at the untrusted reviewer boundary."""

    def __init__(self, code: AlphaReviewProviderFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class GatewayInvoker(Protocol):
    def invoke(self, request: ModelRequest) -> GatewayResult: ...


@dataclass(frozen=True, slots=True)
class GatewayAlphaReviewer:
    """Request cited findings without granting approval or execution authority."""

    gateway: GatewayInvoker

    def review(self, call: AlphaReviewProviderCall) -> AlphaReviewProviderResult:
        if not isinstance(call, AlphaReviewProviderCall):
            raise AlphaReviewProviderError(AlphaReviewProviderFailureCode.INVALID_GATEWAY_RESULT)
        request = ModelRequest(
            request_id=call.request_id,
            capability=ModelCapability.REVIEW,
            input=cast("dict[str, JsonValue]", alpha_review_context_payload(call.context)),
            output_schema=cast("dict[str, JsonValue]", ALPHA_REVIEW_PROPOSAL_OUTPUT_SCHEMA),
            classification=call.classification,
            locality=call.locality,
            budget=call.budget,
            estimated_input_tokens=call.estimated_input_tokens,
            correlation_id=call.correlation_id,
            run_id=call.context.acceptance.run_id,
            node_id=call.review_id,
            deterministic_required=False,
            causation_id=call.causation_id,
            tools_allowed=False,
        )
        result = self.gateway.invoke(request)
        decision = result.decision
        response = result.response
        if (
            decision.capability is not ModelCapability.REVIEW
            or response.request_id != call.request_id
            or response.profile_id != decision.profile_id
            or response.adapter_id != decision.adapter_id
            or response.model_id != decision.model_id
        ):
            raise AlphaReviewProviderError(AlphaReviewProviderFailureCode.INVALID_GATEWAY_RESULT)
        try:
            proposal = alpha_review_proposal_from_mapping(response.output)
        except AlphaReviewContractError as error:
            raise AlphaReviewProviderError(
                AlphaReviewProviderFailureCode.INVALID_PROPOSAL
            ) from error
        if proposal.context_digest != call.context.digest:
            raise AlphaReviewProviderError(AlphaReviewProviderFailureCode.CONTEXT_MISMATCH)
        return AlphaReviewProviderResult(
            proposal=proposal,
            provider_output_digest=json_digest(response.output),
            profile_id=response.profile_id,
            adapter_id=response.adapter_id,
            model_id=response.model_id,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=response.latency_ms,
            cost_microusd=response.cost_microusd,
            completed_at=response.completed_at,
        )


__all__ = [
    "AlphaReviewProviderError",
    "AlphaReviewProviderFailureCode",
    "GatewayAlphaReviewer",
    "GatewayInvoker",
]
