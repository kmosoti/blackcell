"""Gateway-backed proposal-only provider for execution text changes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from blackcell.gateway import GatewayResult, ModelCapability, ModelRequest
from blackcell.kernel import JsonValue
from blackcell.kernel._json import json_digest
from blackcell.orchestration.changes import (
    CHANGE_PROPOSAL_OUTPUT_SCHEMA,
    ChangeContractError,
    ChangeProviderCall,
    ChangeProviderResult,
    change_context_payload,
    change_proposal_from_mapping,
)


class ChangeProviderFailureCode(StrEnum):
    INVALID_GATEWAY_RESULT = "invalid-change-gateway-result"
    INVALID_PROPOSAL = "invalid-change-provider-proposal"
    EVIDENCE_MISMATCH = "change-provider-evidence-mismatch"


class ChangeProviderError(RuntimeError):
    """A content-free provider-boundary failure."""

    def __init__(self, code: ChangeProviderFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class GatewayInvoker(Protocol):
    def invoke(self, request: ModelRequest) -> GatewayResult: ...


@dataclass(frozen=True, slots=True)
class GatewayChangeProvider:
    """Ask a policy-admitted model for inert structured changes, never execution."""

    gateway: GatewayInvoker

    def propose(self, call: ChangeProviderCall) -> ChangeProviderResult:
        if not isinstance(call, ChangeProviderCall):
            raise ChangeProviderError(ChangeProviderFailureCode.INVALID_GATEWAY_RESULT)
        request = ModelRequest(
            request_id=call.request_id,
            capability=ModelCapability.CODE,
            input=cast("dict[str, JsonValue]", change_context_payload(call.context)),
            output_schema=cast("dict[str, JsonValue]", CHANGE_PROPOSAL_OUTPUT_SCHEMA),
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
            raise ChangeProviderError(ChangeProviderFailureCode.INVALID_GATEWAY_RESULT)
        try:
            proposal = change_proposal_from_mapping(response.output)
        except ChangeContractError as error:
            raise ChangeProviderError(ChangeProviderFailureCode.INVALID_PROPOSAL) from error
        if proposal.evidence_digest != call.context.digest:
            raise ChangeProviderError(ChangeProviderFailureCode.EVIDENCE_MISMATCH)
        return ChangeProviderResult(
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
    "ChangeProviderError",
    "ChangeProviderFailureCode",
    "GatewayChangeProvider",
    "GatewayInvoker",
]
