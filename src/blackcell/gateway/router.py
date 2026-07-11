from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from blackcell.gateway.models import (
    GatewayAuditRecord,
    GatewayBudget,
    GatewayFailureCode,
    GatewayResult,
    LocalityPolicy,
    ModelRequest,
    ModelResponse,
    PreparedGatewayCall,
    RoutingDecision,
)
from blackcell.gateway.ports import AdapterRegistry, Clock, GatewayAuditSink, ModelAdapter
from blackcell.gateway.profiles import GatewayProfile
from blackcell.gateway.schema import validate_output


class GatewayAdmissionError(RuntimeError):
    def __init__(self, code: GatewayFailureCode, message: str) -> None:
        if not isinstance(code, GatewayFailureCode):
            raise TypeError("gateway admission failures require a stable failure code")
        if not message.strip():
            raise ValueError("gateway admission failure message must not be empty")
        super().__init__(message)
        self.code = code


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
        for registry_key, adapter in adapters.items():
            if registry_key != adapter.adapter_id:
                raise ValueError(
                    f"adapter registry key {registry_key!r} does not match "
                    f"adapter_id {adapter.adapter_id!r}"
                )
        self._profiles = profiles
        self._adapters = dict(adapters)
        self._audit_sink = audit_sink
        self._clock = clock

    def invoke(self, request: ModelRequest) -> GatewayResult:
        """Compatibility entry point for one-shot callers."""

        return self.invoke_prepared(self.prepare(request))

    def prepare(self, request: ModelRequest) -> PreparedGatewayCall:
        """Select and freeze an exact route without invoking a model adapter."""

        profile, adapter = self._select(request)
        return PreparedGatewayCall(
            request=request,
            decision=self._decision(request, profile, adapter),
            effective_budget=self._effective_budget(request, profile),
        )

    def invoke_prepared(self, call: PreparedGatewayCall) -> GatewayResult:
        """Invoke only the exact route admitted by :meth:`prepare`."""

        expected = self.prepare(call.request)
        if call != expected:
            raise GatewayAdmissionError(
                GatewayFailureCode.PREPARED_CALL_INVALID,
                "prepared gateway call no longer matches gateway policy",
            )
        profile, adapter = self._profile_adapter(expected.decision)
        adapter_request = replace(call.request, budget=call.effective_budget)
        result = adapter.invoke(adapter_request, model_id=profile.model_id)
        request = call.request
        if result.input_tokens > request.budget.max_input_tokens:
            raise GatewayAdmissionError(
                GatewayFailureCode.ADAPTER_INPUT_BUDGET_EXCEEDED,
                "adapter exceeded the input-token budget",
            )
        if result.input_tokens > profile.max_input_tokens:
            raise GatewayAdmissionError(
                GatewayFailureCode.PROFILE_INPUT_LIMIT_EXCEEDED,
                "adapter exceeded the profile input-token limit",
            )
        if result.output_tokens > request.budget.max_output_tokens:
            raise GatewayAdmissionError(
                GatewayFailureCode.ADAPTER_OUTPUT_BUDGET_EXCEEDED,
                "adapter exceeded the output-token budget",
            )
        if result.output_tokens > profile.max_output_tokens:
            raise GatewayAdmissionError(
                GatewayFailureCode.PROFILE_OUTPUT_LIMIT_EXCEEDED,
                "adapter exceeded the profile output-token limit",
            )
        if result.latency_ms > request.budget.max_latency_ms:
            raise GatewayAdmissionError(
                GatewayFailureCode.ADAPTER_LATENCY_BUDGET_EXCEEDED,
                "adapter exceeded the latency budget",
            )
        if result.cost_microusd > request.budget.max_cost_microusd:
            raise GatewayAdmissionError(
                GatewayFailureCode.ADAPTER_COST_BUDGET_EXCEEDED,
                "adapter exceeded the cost budget",
            )
        if result.cost_microusd > profile.max_cost_microusd:
            raise GatewayAdmissionError(
                GatewayFailureCode.PROFILE_COST_LIMIT_EXCEEDED,
                "adapter exceeded the profile cost limit",
            )
        if profile.deterministic and not result.deterministic:
            raise GatewayAdmissionError(
                GatewayFailureCode.PROFILE_DETERMINISM_VIOLATED,
                "adapter returned a non-deterministic result for a deterministic profile",
            )
        if request.deterministic_required and not result.deterministic:
            raise GatewayAdmissionError(
                GatewayFailureCode.REQUEST_DETERMINISM_VIOLATED,
                "adapter returned a non-deterministic result",
            )
        validate_output(result.output, request.output_schema)
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
        return GatewayResult(call.decision, response)

    def _select(self, request: ModelRequest) -> tuple[GatewayProfile, ModelAdapter]:
        if request.estimated_input_tokens > request.budget.max_input_tokens:
            raise GatewayAdmissionError(
                GatewayFailureCode.REQUEST_INPUT_BUDGET_EXCEEDED,
                "request exceeds its input-token budget",
            )
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
            raise GatewayAdmissionError(
                GatewayFailureCode.NO_PROFILE,
                "no model profile satisfies the request policy",
            )
        candidates.sort(key=lambda item: (item[0].priority, item[0].profile_id))
        return candidates[0]

    @staticmethod
    def _decision(
        request: ModelRequest,
        profile: GatewayProfile,
        adapter: ModelAdapter,
    ) -> RoutingDecision:
        return RoutingDecision(
            profile.profile_id,
            adapter.adapter_id,
            profile.model_id,
            request.capability,
            adapter.local,
            adapter.deterministic,
        )

    @staticmethod
    def _effective_budget(
        request: ModelRequest,
        profile: GatewayProfile,
    ) -> GatewayBudget:
        return GatewayBudget(
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
        )

    def _profile_adapter(
        self,
        decision: RoutingDecision,
    ) -> tuple[GatewayProfile, ModelAdapter]:
        profile = next(
            (item for item in self._profiles if item.profile_id == decision.profile_id),
            None,
        )
        adapter = self._adapters.get(decision.adapter_id)
        if profile is None or adapter is None:
            raise GatewayAdmissionError(
                GatewayFailureCode.PREPARED_CALL_INVALID,
                "prepared gateway route is no longer registered",
            )
        return profile, adapter

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
