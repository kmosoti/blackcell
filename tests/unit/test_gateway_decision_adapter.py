from __future__ import annotations

from collections.abc import Set
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from blackcell.adapters.models import GatewayDecisionAdapter
from blackcell.features.request_decision import (
    DecisionAffordance,
    DecisionArgumentSpec,
    DecisionBudget,
    DecisionCapability,
    DecisionClassification,
    DecisionFailureKind,
    DecisionGatewayError,
    DecisionLocality,
    DecisionRequirements,
    RequestDecision,
)
from blackcell.gateway import (
    AdapterResult,
    DataClassification,
    GatewayAdmissionError,
    GatewayBudget,
    GatewayFailureCode,
    GatewayProfile,
    LocalityPolicy,
    ModelCapability,
    ModelGateway,
    ModelRequest,
)
from blackcell.kernel import JsonValue

NOW = datetime(2026, 7, 11, 19, tzinfo=UTC)


class Adapter:
    adapter_id = "fixture"
    local = True
    deterministic = True
    capabilities: Set[ModelCapability] = frozenset({ModelCapability.REASON})

    def __init__(
        self,
        *,
        output: dict[str, JsonValue] | None = None,
        input_tokens: int = 12,
        output_tokens: int = 4,
        latency_ms: int = 10,
        cost_microusd: int = 3,
        failure: Exception | None = None,
    ) -> None:
        self.output = _valid_output() if output is None else output
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms
        self.cost_microusd = cost_microusd
        self.failure = failure
        self.calls = 0
        self.seen_request: ModelRequest | None = None
        self.seen_model_id: str | None = None

    def invoke(self, request: ModelRequest, *, model_id: str) -> AdapterResult:
        self.calls += 1
        self.seen_request = request
        self.seen_model_id = model_id
        if self.failure is not None:
            raise self.failure
        return AdapterResult(
            self.output,
            self.input_tokens,
            self.output_tokens,
            self.latency_ms,
            self.cost_microusd,
            True,
        )


class Audit:
    def __init__(self) -> None:
        self.records = []

    def record(self, record) -> None:
        self.records.append(record)


def test_gateway_prepare_is_deterministic_and_does_not_invoke_the_adapter() -> None:
    adapter = Adapter()
    gateway = _gateway(adapter)
    request = _model_request()

    first = gateway.prepare(request)
    second = gateway.prepare(request)

    assert first == second
    assert adapter.calls == 0
    assert first.decision.profile_id == "reason-local"
    assert first.effective_budget == GatewayBudget(50, 10, 1_000, 50)


def test_invoke_prepared_uses_the_frozen_effective_budget_and_audits_once() -> None:
    adapter = Adapter()
    audit = Audit()
    gateway = _gateway(adapter, audit=audit)
    prepared = gateway.prepare(_model_request())

    result = gateway.invoke_prepared(prepared)

    assert result.decision == prepared.decision
    assert result.response.output["proposal_id"] == "proposal:1"
    assert adapter.calls == 1
    assert adapter.seen_request is not None
    assert adapter.seen_request.budget == prepared.effective_budget
    assert adapter.seen_model_id == "reason-v1"
    assert len(audit.records) == 1


def test_legacy_invoke_delegates_to_the_staged_protocol() -> None:
    adapter = Adapter()
    gateway = _gateway(adapter)

    result = gateway.invoke(_model_request())

    assert result.decision.profile_id == "reason-local"
    assert adapter.calls == 1


def test_forged_prepared_call_fails_before_adapter_invocation() -> None:
    adapter = Adapter()
    gateway = _gateway(adapter)
    prepared = gateway.prepare(_model_request())
    forged = replace(
        prepared,
        effective_budget=replace(prepared.effective_budget, max_output_tokens=9),
    )

    with pytest.raises(GatewayAdmissionError) as caught:
        gateway.invoke_prepared(forged)

    assert caught.value.code is GatewayFailureCode.PREPARED_CALL_INVALID
    assert adapter.calls == 0


def test_prepared_call_rejects_policy_expansion_and_route_mismatch() -> None:
    prepared = _gateway(Adapter()).prepare(_model_request())
    cases = (
        lambda: replace(
            prepared,
            decision=replace(prepared.decision, capability=ModelCapability.REVIEW),
        ),
        lambda: replace(
            prepared,
            decision=replace(prepared.decision, local=False),
        ),
        lambda: replace(
            prepared,
            decision=replace(prepared.decision, deterministic=False),
        ),
        lambda: replace(
            prepared,
            effective_budget=replace(
                prepared.effective_budget,
                max_input_tokens=prepared.request.budget.max_input_tokens + 1,
            ),
        ),
        lambda: replace(
            prepared,
            effective_budget=replace(
                prepared.effective_budget,
                max_output_tokens=prepared.request.budget.max_output_tokens + 1,
            ),
        ),
        lambda: replace(
            prepared,
            effective_budget=replace(
                prepared.effective_budget,
                max_latency_ms=prepared.request.budget.max_latency_ms + 1,
            ),
        ),
        lambda: replace(
            prepared,
            effective_budget=replace(
                prepared.effective_budget,
                max_cost_microusd=prepared.request.budget.max_cost_microusd + 1,
            ),
        ),
    )
    for factory in cases:
        with pytest.raises(ValueError):
            factory()


@pytest.mark.parametrize(
    ("request_factory", "expected_code"),
    (
        (
            lambda: replace(_model_request(), estimated_input_tokens=101),
            GatewayFailureCode.REQUEST_INPUT_BUDGET_EXCEEDED,
        ),
        (
            lambda: replace(_model_request(), capability=ModelCapability.REVIEW),
            GatewayFailureCode.NO_PROFILE,
        ),
    ),
)
def test_prepare_exposes_stable_content_free_failure_codes(
    request_factory,
    expected_code: GatewayFailureCode,
) -> None:
    gateway = _gateway(Adapter())
    request = request_factory()

    with pytest.raises(GatewayAdmissionError) as caught:
        gateway.prepare(request)

    assert caught.value.code is expected_code
    assert request.input["context_payload"] not in str(caught.value)


def test_gateway_decision_adapter_maps_the_complete_feature_request() -> None:
    model_adapter = Adapter()
    bridge = GatewayDecisionAdapter(
        _gateway(model_adapter),
        clock=lambda: NOW,
    )
    request = _decision_request()

    route = bridge.route(request)

    assert model_adapter.calls == 0
    assert route.profile_id == "reason-local"
    assert route.capability is DecisionCapability.REASON
    assert route.local and route.deterministic
    assert route.selected_at == NOW

    result = bridge.invoke(request, route)

    assert model_adapter.calls == 1
    assert result.output["context_frame_id"] == request.context_frame_id
    assert result.completed_at == NOW
    mapped = model_adapter.seen_request
    assert mapped is not None
    assert mapped.input == request.model_input
    assert mapped.output_schema == request.output_schema
    assert mapped.classification is DataClassification.PRIVATE
    assert mapped.locality is LocalityPolicy.LOCAL_ONLY
    assert mapped.correlation_id == mapped.run_id == request.run_id
    assert mapped.causation_id == request.causation_id
    assert mapped.node_id == request.node_id
    assert mapped.tools_allowed is False


def test_bridge_rejects_route_drift_before_model_invocation() -> None:
    model_adapter = Adapter()
    bridge = GatewayDecisionAdapter(_gateway(model_adapter), clock=lambda: NOW)
    request = _decision_request()
    route = bridge.route(request)

    with pytest.raises(DecisionGatewayError) as caught:
        bridge.invoke(request, replace(route, model_id="different-model"))

    assert caught.value.kind is DecisionFailureKind.INTEGRITY
    assert caught.value.code == "gateway_route_changed"
    assert model_adapter.calls == 0


def test_bridge_rechecks_admission_before_invoking_a_previously_selected_route() -> None:
    model_adapter = Adapter()
    bridge = GatewayDecisionAdapter(_gateway(model_adapter), clock=lambda: NOW)
    request = _decision_request()
    route = bridge.route(request)
    changed = replace(
        request,
        requirements=replace(
            request.requirements,
            capability=DecisionCapability.REVIEW,
        ),
    )

    with pytest.raises(DecisionGatewayError) as caught:
        bridge.invoke(changed, route)

    assert caught.value.kind is DecisionFailureKind.ADMISSION
    assert caught.value.code == GatewayFailureCode.NO_PROFILE.value
    assert model_adapter.calls == 0


def test_bridge_maps_invalid_route_and_response_timestamps_to_integrity_failures() -> None:
    naive = datetime(2026, 7, 11, 19)
    request = _decision_request()
    route_bridge = GatewayDecisionAdapter(_gateway(Adapter()), clock=lambda: naive)
    with pytest.raises(DecisionGatewayError) as route_failure:
        route_bridge.route(request)
    assert route_failure.value.code == "gateway_route_contract_invalid"

    model_adapter = Adapter()
    response_bridge = GatewayDecisionAdapter(
        ModelGateway(
            (_profile(model_adapter),),
            {model_adapter.adapter_id: model_adapter},
            clock=lambda: naive,
        ),
        clock=lambda: NOW,
    )
    route = response_bridge.route(request)
    with pytest.raises(DecisionGatewayError) as response_failure:
        response_bridge.invoke(request, route)
    assert response_failure.value.code == "gateway_response_contract_invalid"


def test_bridge_maps_route_admission_without_exposing_request_content() -> None:
    bridge = GatewayDecisionAdapter(ModelGateway((), {}), clock=lambda: NOW)
    request = _decision_request()

    with pytest.raises(DecisionGatewayError) as caught:
        bridge.route(request)

    assert caught.value.kind is DecisionFailureKind.ADMISSION
    assert caught.value.code == GatewayFailureCode.NO_PROFILE.value
    assert request.context_payload not in str(caught.value)


def test_bridge_maps_post_call_failure_classes_to_feature_codes() -> None:
    fixtures: tuple[tuple[Adapter, DecisionFailureKind, str, bool], ...] = (
        (
            Adapter(output_tokens=21),
            DecisionFailureKind.BUDGET,
            GatewayFailureCode.ADAPTER_OUTPUT_BUDGET_EXCEEDED.value,
            False,
        ),
        (
            Adapter(output={"unexpected": True}),
            DecisionFailureKind.SCHEMA,
            "gateway_output_schema_invalid",
            False,
        ),
        (
            Adapter(failure=TimeoutError("provider detail")),
            DecisionFailureKind.TIMEOUT,
            "gateway_adapter_timeout",
            True,
        ),
        (
            Adapter(failure=RuntimeError("provider detail")),
            DecisionFailureKind.ADAPTER,
            "gateway_adapter_failed",
            False,
        ),
    )
    for model_adapter, kind, code, retryable in fixtures:
        bridge = GatewayDecisionAdapter(_gateway(model_adapter), clock=lambda: NOW)
        request = _decision_request()
        route = bridge.route(request)

        with pytest.raises(DecisionGatewayError) as caught:
            bridge.invoke(request, route)

        assert caught.value.kind is kind
        assert caught.value.code == code
        assert caught.value.retryable is retryable
        assert "provider detail" not in str(caught.value)


def _gateway(adapter: Adapter, *, audit: Audit | None = None) -> ModelGateway:
    return ModelGateway(
        (_profile(adapter),),
        {adapter.adapter_id: adapter},
        audit_sink=audit,
        clock=lambda: NOW,
    )


def _profile(adapter: Adapter) -> GatewayProfile:
    return GatewayProfile(
        "reason-local",
        ModelCapability.REASON,
        adapter.adapter_id,
        "reason-v1",
        0,
        True,
        True,
        DataClassification.SECRET,
        50,
        10,
        50,
    )


def _model_request() -> ModelRequest:
    decision = _decision_request()
    return ModelRequest(
        decision.request_id,
        ModelCapability.REASON,
        decision.model_input,
        decision.output_schema,
        DataClassification.PRIVATE,
        LocalityPolicy.LOCAL_ONLY,
        GatewayBudget(100, 20, 1_000, 100),
        12,
        decision.correlation_id,
        decision.run_id,
        decision.node_id,
        deterministic_required=True,
        causation_id=decision.causation_id,
    )


def _decision_request() -> RequestDecision:
    return RequestDecision(
        DecisionRequirements(
            "decision:1",
            "node:planner",
            DecisionCapability.REASON,
            DecisionClassification.PRIVATE,
            DecisionLocality.LOCAL_ONLY,
            DecisionBudget(100, 20, 1_000, 100),
            12,
            True,
            NOW,
        ),
        "run:1",
        "run:1",
        "event:context-recorded",
        "sha256:" + "1" * 64,
        "inspect project status",
        '{"status":"ready"}',
        ("event:1",),
        (DecisionAffordance("inspect", (DecisionArgumentSpec("path"),)),),
    )


def _valid_output() -> dict[str, JsonValue]:
    return {
        "proposal_id": "proposal:1",
        "context_frame_id": "sha256:" + "1" * 64,
        "affordance": "inspect",
        "arguments": ({"name": "path", "value": "README.md"},),
        "rationale": "inspect the cited repository evidence",
        "evidence_event_ids": ("event:1",),
    }
