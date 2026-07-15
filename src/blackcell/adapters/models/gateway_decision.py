from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from blackcell.features.request_decision import (
    DecisionAdapterResult,
    DecisionCapability,
    DecisionClassification,
    DecisionFailureKind,
    DecisionGatewayError,
    DecisionLocality,
    DecisionRoute,
    RequestDecision,
)
from blackcell.gateway import (
    DataClassification,
    GatewayAdmissionError,
    GatewayBudget,
    GatewayFailureCode,
    LocalityPolicy,
    ModelCapability,
    ModelGateway,
    ModelRequest,
    PreparedGatewayCall,
)
from blackcell.gateway.schema import OutputSchemaError

Clock = Callable[[], datetime]

_CAPABILITIES = {
    DecisionCapability.REASON: ModelCapability.REASON,
    DecisionCapability.CODE: ModelCapability.CODE,
    DecisionCapability.REVIEW: ModelCapability.REVIEW,
    DecisionCapability.VERIFY: ModelCapability.VERIFY,
    DecisionCapability.EMBED: ModelCapability.EMBED,
}
_CLASSIFICATIONS = {
    DecisionClassification.PUBLIC: DataClassification.PUBLIC,
    DecisionClassification.INTERNAL: DataClassification.INTERNAL,
    DecisionClassification.PRIVATE: DataClassification.PRIVATE,
    DecisionClassification.SECRET: DataClassification.SECRET,
}
_LOCALITIES = {
    DecisionLocality.LOCAL_ONLY: LocalityPolicy.LOCAL_ONLY,
    DecisionLocality.REMOTE_ALLOWED: LocalityPolicy.REMOTE_ALLOWED,
}
_BUDGET_FAILURES = frozenset(
    {
        GatewayFailureCode.ADAPTER_INPUT_BUDGET_EXCEEDED,
        GatewayFailureCode.PROFILE_INPUT_LIMIT_EXCEEDED,
        GatewayFailureCode.ADAPTER_OUTPUT_BUDGET_EXCEEDED,
        GatewayFailureCode.PROFILE_OUTPUT_LIMIT_EXCEEDED,
        GatewayFailureCode.ADAPTER_LATENCY_BUDGET_EXCEEDED,
        GatewayFailureCode.ADAPTER_COST_BUDGET_EXCEEDED,
        GatewayFailureCode.PROFILE_COST_LIMIT_EXCEEDED,
    }
)
_INTEGRITY_FAILURES = frozenset(
    {
        GatewayFailureCode.PREPARED_CALL_INVALID,
        GatewayFailureCode.PROFILE_DETERMINISM_VIOLATED,
        GatewayFailureCode.REQUEST_DETERMINISM_VIOLATED,
    }
)


class GatewayDecisionAdapter:
    """Translate the request-decision feature contract to the model gateway."""

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._gateway = gateway
        self._clock = clock

    def route(self, request: RequestDecision) -> DecisionRoute:
        try:
            prepared = self._gateway.prepare(_model_request(request))
        except GatewayAdmissionError as error:
            raise _decision_error(error) from error
        selected_at = self._clock()
        try:
            return _decision_route(prepared, selected_at=selected_at)
        except (TypeError, ValueError) as error:
            raise DecisionGatewayError(
                DecisionFailureKind.INTEGRITY,
                "gateway_route_contract_invalid",
                exception_type=type(error).__name__,
            ) from error

    def invoke(
        self,
        request: RequestDecision,
        route: DecisionRoute,
    ) -> DecisionAdapterResult:
        try:
            prepared = self._gateway.prepare(_model_request(request))
        except GatewayAdmissionError as error:
            raise _decision_error(error) from error
        if not _same_route(prepared, route):
            raise DecisionGatewayError(
                DecisionFailureKind.INTEGRITY,
                "gateway_route_changed",
            )
        try:
            result = self._gateway.invoke_prepared(prepared)
        except GatewayAdmissionError as error:
            raise _decision_error(error) from error
        except OutputSchemaError as error:
            raise DecisionGatewayError(
                DecisionFailureKind.SCHEMA,
                "gateway_output_schema_invalid",
                exception_type=type(error).__name__,
            ) from error
        except TimeoutError as error:
            raise DecisionGatewayError(
                DecisionFailureKind.TIMEOUT,
                "gateway_adapter_timeout",
                retryable=True,
                exception_type=type(error).__name__,
            ) from error
        except Exception as error:
            raise DecisionGatewayError(
                DecisionFailureKind.ADAPTER,
                "gateway_adapter_failed",
                exception_type=type(error).__name__,
            ) from error
        response = result.response
        try:
            return DecisionAdapterResult(
                output=response.output,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                latency_ms=response.latency_ms,
                cost_microusd=response.cost_microusd,
                deterministic=response.deterministic,
                completed_at=response.completed_at,
            )
        except (TypeError, ValueError) as error:
            raise DecisionGatewayError(
                DecisionFailureKind.INTEGRITY,
                "gateway_response_contract_invalid",
                exception_type=type(error).__name__,
            ) from error


def _model_request(request: RequestDecision) -> ModelRequest:
    return ModelRequest(
        request_id=request.request_id,
        capability=_CAPABILITIES[request.capability],
        input=request.model_input,
        output_schema=request.output_schema,
        classification=_CLASSIFICATIONS[request.classification],
        locality=_LOCALITIES[request.locality],
        budget=GatewayBudget(
            request.budget.max_input_tokens,
            request.budget.max_output_tokens,
            request.budget.max_latency_ms,
            request.budget.max_cost_microusd,
        ),
        estimated_input_tokens=request.estimated_input_tokens,
        correlation_id=request.correlation_id,
        run_id=request.run_id,
        node_id=request.node_id,
        deterministic_required=request.deterministic_required,
        causation_id=request.causation_id,
        tools_allowed=False,
    )


def _decision_route(
    prepared: PreparedGatewayCall,
    *,
    selected_at: datetime,
) -> DecisionRoute:
    decision = prepared.decision
    return DecisionRoute(
        profile_id=decision.profile_id,
        adapter_id=decision.adapter_id,
        model_id=decision.model_id,
        capability=DecisionCapability(decision.capability.value),
        local=decision.local,
        deterministic=decision.deterministic,
        selected_at=selected_at,
    )


def _same_route(prepared: PreparedGatewayCall, route: DecisionRoute) -> bool:
    decision = prepared.decision
    return (
        route.profile_id == decision.profile_id
        and route.adapter_id == decision.adapter_id
        and route.model_id == decision.model_id
        and route.capability.value == decision.capability.value
        and route.local is decision.local
        and route.deterministic is decision.deterministic
    )


def _decision_error(error: GatewayAdmissionError) -> DecisionGatewayError:
    if error.code in _BUDGET_FAILURES:
        kind = DecisionFailureKind.BUDGET
    elif error.code in _INTEGRITY_FAILURES:
        kind = DecisionFailureKind.INTEGRITY
    else:
        kind = DecisionFailureKind.ADMISSION
    return DecisionGatewayError(kind, error.code.value)


__all__ = ["GatewayDecisionAdapter"]
