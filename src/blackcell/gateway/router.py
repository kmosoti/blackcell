from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from blackcell.gateway.models import (
    GatewayAuditRecord,
    GatewayBudget,
    GatewayResult,
    LocalityPolicy,
    ModelRequest,
    ModelResponse,
    RoutingDecision,
)
from blackcell.gateway.ports import AdapterRegistry, Clock, GatewayAuditSink, ModelAdapter
from blackcell.gateway.profiles import GatewayProfile
from blackcell.gateway.schema import validate_output


class GatewayAdmissionError(RuntimeError):
    pass


class ModelGateway:
    def __init__(
        self,
        profiles: tuple[GatewayProfile, ...],
        adapters: AdapterRegistry,
        *,
        audit_sink: GatewayAuditSink | None = None,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        if len({profile.profile_id for profile in profiles}) != len(profiles):
            raise ValueError("gateway profile ids must be unique")
        self._profiles = profiles
        self._adapters = adapters
        self._audit_sink = audit_sink
        self._clock = clock

    def invoke(self, request: ModelRequest) -> GatewayResult:
        profile, adapter = self._select(request)
        adapter_request = replace(
            request,
            budget=GatewayBudget(
                max_input_tokens=min(
                    request.budget.max_input_tokens,
                    profile.max_input_tokens,
                ),
                max_output_tokens=min(
                    request.budget.max_output_tokens,
                    profile.max_output_tokens,
                ),
                max_latency_ms=request.budget.max_latency_ms,
                max_cost_microusd=min(
                    request.budget.max_cost_microusd,
                    profile.max_cost_microusd,
                ),
            ),
        )
        result = adapter.invoke(adapter_request, model_id=profile.model_id)
        if result.input_tokens > request.budget.max_input_tokens:
            raise GatewayAdmissionError("adapter exceeded the input-token budget")
        if result.input_tokens > profile.max_input_tokens:
            raise GatewayAdmissionError("adapter exceeded the profile input-token limit")
        if result.output_tokens > request.budget.max_output_tokens:
            raise GatewayAdmissionError("adapter exceeded the output-token budget")
        if result.output_tokens > profile.max_output_tokens:
            raise GatewayAdmissionError("adapter exceeded the profile output-token limit")
        if result.latency_ms > request.budget.max_latency_ms:
            raise GatewayAdmissionError("adapter exceeded the latency budget")
        if result.cost_microusd > request.budget.max_cost_microusd:
            raise GatewayAdmissionError("adapter exceeded the cost budget")
        if result.cost_microusd > profile.max_cost_microusd:
            raise GatewayAdmissionError("adapter exceeded the profile cost limit")
        if request.deterministic_required and not result.deterministic:
            raise GatewayAdmissionError("adapter returned a non-deterministic result")
        validate_output(result.output, request.output_schema)
        decision = RoutingDecision(
            profile.profile_id,
            adapter.adapter_id,
            profile.model_id,
            request.capability,
            adapter.local,
            adapter.deterministic,
        )
        response = ModelResponse(
            request.request_id,
            result.output,
            profile.profile_id,
            adapter.adapter_id,
            profile.model_id,
            result.input_tokens,
            result.output_tokens,
            result.latency_ms,
            result.cost_microusd,
            result.deterministic,
            self._clock(),
        )
        self._audit(request, response)
        return GatewayResult(decision, response)

    def _select(self, request: ModelRequest) -> tuple[GatewayProfile, ModelAdapter]:
        if request.estimated_input_tokens > request.budget.max_input_tokens:
            raise GatewayAdmissionError("request exceeds its input-token budget")
        candidates: list[tuple[GatewayProfile, ModelAdapter]] = []
        for profile in self._profiles:
            adapter = self._adapters.get(profile.adapter_id)
            if adapter is None or not profile.enabled or profile.capability != request.capability:
                continue
            if request.capability not in adapter.capabilities:
                continue
            if profile.local != adapter.local or profile.deterministic != adapter.deterministic:
                continue
            if request.locality == LocalityPolicy.LOCAL_ONLY and not adapter.local:
                continue
            if request.deterministic_required and not adapter.deterministic:
                continue
            if request.classification > profile.maximum_classification:
                continue
            if request.estimated_input_tokens > profile.max_input_tokens:
                continue
            candidates.append((profile, adapter))
        if not candidates:
            raise GatewayAdmissionError("no model profile satisfies the request policy")
        candidates.sort(key=lambda item: (item[0].priority, item[0].profile_id))
        return candidates[0]

    def _audit(self, request: ModelRequest, response: ModelResponse) -> None:
        if self._audit_sink is None:
            return
        self._audit_sink.record(
            GatewayAuditRecord(
                request.request_id,
                request.capability,
                request.classification,
                response.profile_id,
                response.adapter_id,
                response.model_id,
                request.correlation_id,
                request.run_id,
                request.node_id,
                response.input_tokens,
                response.output_tokens,
                response.latency_ms,
                response.cost_microusd,
                response.deterministic,
            )
        )
