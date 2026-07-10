from datetime import UTC, datetime

import pytest

from blackcell.adapters.models import RecordedModelAdapter
from blackcell.gateway import (
    AdapterResult,
    DataClassification,
    GatewayAdmissionError,
    GatewayBudget,
    GatewayProfile,
    LocalityPolicy,
    ModelCapability,
    ModelGateway,
    ModelRequest,
)
from blackcell.gateway.schema import OutputSchemaError
from blackcell.kernel import JsonValue

NOW = datetime(2026, 7, 10, 18, tzinfo=UTC)
SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": ("answer",),
    "properties": {"answer": {"type": "string"}},
}


class AuditSink:
    def __init__(self) -> None:
        self.records = []

    def record(self, record) -> None:
        self.records.append(record)


class UsageAdapter(RecordedModelAdapter):
    def __init__(
        self,
        *,
        input_tokens: int = 10,
        output_tokens: int = 1,
        cost_microusd: int = 0,
    ) -> None:
        super().__init__("recorded", {}, capabilities={ModelCapability.REASON})
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._cost_microusd = cost_microusd
        self.seen_budget: GatewayBudget | None = None

    def invoke(self, request: ModelRequest, *, model_id: str) -> AdapterResult:
        assert model_id == "reason-v1"
        self.seen_budget = request.budget
        return AdapterResult(
            {"answer": "ready"},
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            latency_ms=0,
            cost_microusd=self._cost_microusd,
            deterministic=True,
        )


def test_gateway_routes_by_capability_and_emits_content_free_audit() -> None:
    audit = AuditSink()
    adapter = RecordedModelAdapter(
        "recorded",
        {("reason-v1", "request:1"): {"answer": "ready"}},
        capabilities={ModelCapability.REASON},
    )
    gateway = ModelGateway(
        (_profile("reason", ModelCapability.REASON),),
        {"recorded": adapter},
        audit_sink=audit,
        clock=lambda: NOW,
    )

    result = gateway.invoke(_request())

    assert result.response.output["answer"] == "ready"
    assert result.decision.profile_id == "reason"
    assert result.response.completed_at == NOW
    assert audit.records[0].request_id == "request:1"
    assert not hasattr(audit.records[0], "input")


def test_gateway_enforces_locality_classification_and_capability() -> None:
    remote = RecordedModelAdapter(
        "remote",
        {("reason-v1", "request:1"): {"answer": "remote"}},
        capabilities={ModelCapability.REASON},
        local=False,
    )
    local = RecordedModelAdapter(
        "local",
        {("reason-v1", "request:1"): {"answer": "local"}},
        capabilities={ModelCapability.REASON},
    )
    profiles = (
        _profile("remote", ModelCapability.REASON, adapter="remote", priority=0),
        _profile(
            "local",
            ModelCapability.REASON,
            adapter="local",
            priority=1,
            classification=DataClassification.SECRET,
        ),
    )
    gateway = ModelGateway(profiles, {"remote": remote, "local": local})

    result = gateway.invoke(
        _request(classification=DataClassification.PRIVATE, locality=LocalityPolicy.LOCAL_ONLY)
    )

    assert result.decision.adapter_id == "local"
    with pytest.raises(GatewayAdmissionError, match="no model profile"):
        gateway.invoke(_request(capability=ModelCapability.EMBED))


def test_gateway_rejects_budget_and_schema_violations() -> None:
    adapter = RecordedModelAdapter(
        "recorded",
        {("reason-v1", "request:1"): {"unexpected": True}},
        capabilities={ModelCapability.REASON},
    )
    gateway = ModelGateway((_profile("reason", ModelCapability.REASON),), {"recorded": adapter})

    with pytest.raises(GatewayAdmissionError, match="input-token"):
        gateway.invoke(_request(estimated_input_tokens=101))
    with pytest.raises(OutputSchemaError, match="missing required"):
        gateway.invoke(_request())


def test_gateway_routes_profile_with_tighter_output_and_cost_ceilings() -> None:
    adapter = UsageAdapter(output_tokens=1, cost_microusd=0)
    gateway = ModelGateway(
        (
            _profile(
                "free",
                ModelCapability.REASON,
                max_output_tokens=1,
                max_cost_microusd=0,
            ),
        ),
        {"recorded": adapter},
    )

    result = gateway.invoke(_request())

    assert result.decision.profile_id == "free"
    assert result.response.output_tokens == 1
    assert result.response.cost_microusd == 0
    assert adapter.seen_budget == GatewayBudget(100, 1, 1_000, 0)


@pytest.mark.parametrize(
    ("request_max_cost", "profile_max_cost", "expected_message"),
    (
        (10, 100, "cost budget"),
        (100, 10, "profile cost limit"),
    ),
)
def test_gateway_enforces_actual_cost_against_both_ceilings(
    request_max_cost: int,
    profile_max_cost: int,
    expected_message: str,
) -> None:
    adapter = UsageAdapter(cost_microusd=11)
    gateway = ModelGateway(
        (
            _profile(
                "reason",
                ModelCapability.REASON,
                max_cost_microusd=profile_max_cost,
            ),
        ),
        {"recorded": adapter},
    )

    with pytest.raises(GatewayAdmissionError, match=expected_message):
        gateway.invoke(
            _request(
                budget=GatewayBudget(
                    max_input_tokens=100,
                    max_output_tokens=20,
                    max_latency_ms=1_000,
                    max_cost_microusd=request_max_cost,
                )
            )
        )


@pytest.mark.parametrize(
    (
        "actual_input_tokens",
        "actual_output_tokens",
        "profile_max_input_tokens",
        "profile_max_output_tokens",
        "expected_message",
    ),
    (
        (11, 1, 10, 20, "profile input-token limit"),
        (10, 2, 100, 1, "profile output-token limit"),
    ),
)
def test_gateway_enforces_actual_tokens_against_profile_ceilings(
    actual_input_tokens: int,
    actual_output_tokens: int,
    profile_max_input_tokens: int,
    profile_max_output_tokens: int,
    expected_message: str,
) -> None:
    adapter = UsageAdapter(
        input_tokens=actual_input_tokens,
        output_tokens=actual_output_tokens,
    )
    gateway = ModelGateway(
        (
            _profile(
                "reason",
                ModelCapability.REASON,
                max_input_tokens=profile_max_input_tokens,
                max_output_tokens=profile_max_output_tokens,
            ),
        ),
        {"recorded": adapter},
    )

    with pytest.raises(GatewayAdmissionError, match=expected_message):
        gateway.invoke(_request())


def test_gateway_refuses_direct_tool_authority() -> None:
    with pytest.raises(ValueError, match="tool authority"):
        _request(tools_allowed=True)


def _request(
    *,
    capability: ModelCapability = ModelCapability.REASON,
    classification: DataClassification = DataClassification.INTERNAL,
    locality: LocalityPolicy = LocalityPolicy.REMOTE_ALLOWED,
    estimated_input_tokens: int = 10,
    tools_allowed: bool = False,
    budget: GatewayBudget | None = None,
) -> ModelRequest:
    return ModelRequest(
        "request:1",
        capability,
        {"prompt": "inspect"},
        SCHEMA,
        classification,
        locality,
        budget if budget is not None else GatewayBudget(100, 20, 1_000, 100),
        estimated_input_tokens,
        "correlation:1",
        "run:1",
        "node:1",
        deterministic_required=True,
        tools_allowed=tools_allowed,
    )


def _profile(
    profile_id: str,
    capability: ModelCapability,
    *,
    adapter: str = "recorded",
    priority: int = 0,
    classification: DataClassification = DataClassification.INTERNAL,
    max_input_tokens: int = 100,
    max_output_tokens: int = 20,
    max_cost_microusd: int = 100,
) -> GatewayProfile:
    return GatewayProfile(
        profile_id,
        capability,
        adapter,
        "reason-v1",
        priority,
        adapter != "remote",
        True,
        classification,
        max_input_tokens,
        max_output_tokens,
        max_cost_microusd,
    )
