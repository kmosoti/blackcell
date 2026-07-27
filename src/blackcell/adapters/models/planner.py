"""Gateway-backed, proposal-only planner for the execution compiler."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast

from blackcell.gateway import GatewayResult, ModelCapability, ModelRequest
from blackcell.kernel import JsonValue
from blackcell.kernel._json import json_digest
from blackcell.orchestration.execution_plan import (
    PLAN_DRAFT_OUTPUT_SCHEMA,
    PlanContractError,
    PlanningRequest,
    PlanningResult,
    planning_payload,
)


class GatewayInvoker(Protocol):
    def invoke(self, request: ModelRequest) -> GatewayResult: ...


@dataclass(frozen=True, slots=True)
class GatewayPlanner:
    """Request an inert plan draft; deterministic compilation remains host-owned."""

    gateway: GatewayInvoker

    def propose_plan(self, request: PlanningRequest) -> PlanningResult:
        if not isinstance(request, PlanningRequest):
            raise PlanContractError()
        gateway_request = ModelRequest(
            request_id=f"plan:{request.goal.goal_id}",
            capability=ModelCapability.REASON,
            input=cast("dict[str, JsonValue]", planning_payload(request.goal)),
            output_schema=cast("dict[str, JsonValue]", PLAN_DRAFT_OUTPUT_SCHEMA),
            classification=request.classification,
            locality=request.locality,
            budget=request.budget,
            estimated_input_tokens=request.estimated_input_tokens,
            correlation_id=request.correlation_id,
            run_id=request.run_id,
            node_id="plan",
            tools_allowed=False,
        )
        result = self.gateway.invoke(gateway_request)
        response = result.response
        decision = result.decision
        if (
            decision.capability is not ModelCapability.REASON
            or response.request_id != gateway_request.request_id
            or response.profile_id != decision.profile_id
            or response.adapter_id != decision.adapter_id
            or response.model_id != decision.model_id
        ):
            raise PlanContractError("invalid-planning-gateway-result")
        return PlanningResult(
            draft=response.output,
            provider_output_digest=json_digest(response.output),
            profile_id=response.profile_id,
            adapter_id=response.adapter_id,
            model_id=response.model_id,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=response.latency_ms,
            cost_microusd=response.cost_microusd,
        )


__all__ = ["GatewayInvoker", "GatewayPlanner"]
