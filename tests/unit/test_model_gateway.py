from datetime import UTC, datetime

import pytest

from blackcell.adapters.models import RecordedModelAdapter
from blackcell.gateway import (
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
) -> ModelRequest:
    return ModelRequest(
        "request:1",
        capability,
        {"prompt": "inspect"},
        SCHEMA,
        classification,
        locality,
        GatewayBudget(100, 20, 1_000, 100),
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
        100,
        20,
        100,
    )
