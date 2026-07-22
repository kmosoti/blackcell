"""Gateway-backed proposal-only provider for alpha text changes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from blackcell.gateway import GatewayResult, ModelCapability, ModelRequest
from blackcell.kernel import JsonValue
from blackcell.kernel._json import json_digest
from blackcell.orchestration.alpha_changes import (
    ALPHA_CHANGE_PROPOSAL_OUTPUT_SCHEMA,
    AlphaChangeContractError,
    AlphaChangeProviderCall,
    AlphaChangeProviderResult,
    alpha_change_context_payload,
    alpha_change_proposal_from_mapping,
)


class AlphaChangeProviderFailureCode(StrEnum):
    INVALID_GATEWAY_RESULT = "invalid-alpha-change-gateway-result"
    INVALID_PROPOSAL = "invalid-alpha-change-provider-proposal"
    EVIDENCE_MISMATCH = "alpha-change-provider-evidence-mismatch"


class AlphaChangeProviderError(RuntimeError):
    """A content-free provider-boundary failure."""

    def __init__(self, code: AlphaChangeProviderFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class GatewayInvoker(Protocol):
    def invoke(self, request: ModelRequest) -> GatewayResult: ...


@dataclass(frozen=True, slots=True)
class GatewayAlphaChangeProvider:
    """Ask a policy-admitted model for inert structured changes, never execution."""

    gateway: GatewayInvoker

    def propose(self, call: AlphaChangeProviderCall) -> AlphaChangeProviderResult:
        if not isinstance(call, AlphaChangeProviderCall):
            raise AlphaChangeProviderError(AlphaChangeProviderFailureCode.INVALID_GATEWAY_RESULT)
        request = ModelRequest(
            request_id=call.request_id,
            capability=ModelCapability.CODE,
            input=cast("dict[str, JsonValue]", alpha_change_context_payload(call.context)),
            output_schema=cast("dict[str, JsonValue]", ALPHA_CHANGE_PROPOSAL_OUTPUT_SCHEMA),
            classification=call.classification,
            locality=call.locality,
            budget=call.budget,
            estimated_input_tokens=call.estimated_input_tokens,
            correlation_id=call.correlation_id,
            run_id=call.run_id,
            node_id=call.node_id,
            deterministic_required=False,
            causation_id=call.causation_id,
            tools_allowed=False,
        )
        result = self.gateway.invoke(request)
        decision = result.decision
        response = result.response
        if (
            decision.capability is not ModelCapability.CODE
            or response.request_id != call.request_id
            or response.profile_id != decision.profile_id
            or response.adapter_id != decision.adapter_id
            or response.model_id != decision.model_id
        ):
            raise AlphaChangeProviderError(AlphaChangeProviderFailureCode.INVALID_GATEWAY_RESULT)
        try:
            proposal = alpha_change_proposal_from_mapping(response.output)
        except AlphaChangeContractError as error:
            raise AlphaChangeProviderError(
                AlphaChangeProviderFailureCode.INVALID_PROPOSAL
            ) from error
        if proposal.evidence_digest != call.context.digest:
            raise AlphaChangeProviderError(AlphaChangeProviderFailureCode.EVIDENCE_MISMATCH)
        return AlphaChangeProviderResult(
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
    "AlphaChangeProviderError",
    "AlphaChangeProviderFailureCode",
    "GatewayAlphaChangeProvider",
    "GatewayInvoker",
]
