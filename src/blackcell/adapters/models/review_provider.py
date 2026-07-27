"""Gateway-backed, proposal-only reviewer for execution execution evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from blackcell.gateway import GatewayResult, ModelCapability, ModelRequest
from blackcell.kernel import JsonValue
from blackcell.kernel._json import json_digest
from blackcell.orchestration.review import (
    REVIEW_PROPOSAL_OUTPUT_SCHEMA,
    ReviewContractError,
    ReviewProviderCall,
    ReviewProviderResult,
    review_context_payload,
    review_proposal_from_mapping,
)


class ReviewProviderFailureCode(StrEnum):
    INVALID_GATEWAY_RESULT = "invalid-review-gateway-result"
    INVALID_PROPOSAL = "invalid-review-provider-proposal"
    CONTEXT_MISMATCH = "review-provider-context-mismatch"


class ReviewProviderError(RuntimeError):
    """A content-free failure at the untrusted reviewer boundary."""

    def __init__(self, code: ReviewProviderFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class GatewayInvoker(Protocol):
    def invoke(self, request: ModelRequest) -> GatewayResult: ...


@dataclass(frozen=True, slots=True)
class GatewayReviewer:
    """Request cited findings without granting approval or execution authority."""

    gateway: GatewayInvoker

    def review(self, call: ReviewProviderCall) -> ReviewProviderResult:
        if not isinstance(call, ReviewProviderCall):
            raise ReviewProviderError(ReviewProviderFailureCode.INVALID_GATEWAY_RESULT)
        request = ModelRequest(
            request_id=call.request_id,
            capability=ModelCapability.REVIEW,
            input=cast("dict[str, JsonValue]", review_context_payload(call.context)),
            output_schema=cast("dict[str, JsonValue]", REVIEW_PROPOSAL_OUTPUT_SCHEMA),
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
            raise ReviewProviderError(ReviewProviderFailureCode.INVALID_GATEWAY_RESULT)
        try:
            proposal = review_proposal_from_mapping(response.output)
        except ReviewContractError as error:
            raise ReviewProviderError(ReviewProviderFailureCode.INVALID_PROPOSAL) from error
        if proposal.context_digest != call.context.digest:
            raise ReviewProviderError(ReviewProviderFailureCode.CONTEXT_MISMATCH)
        return ReviewProviderResult(
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
    "GatewayInvoker",
    "GatewayReviewer",
    "ReviewProviderError",
    "ReviewProviderFailureCode",
]
