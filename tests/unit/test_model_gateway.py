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
from blackcell.gateway.schema import OutputSchemaError, validate_output
from blackcell.kernel import JsonValue
from blackcell.kernel._json import json_digest

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
        result_deterministic: bool = True,
    ) -> None:
        super().__init__("recorded", {}, capabilities={ModelCapability.REASON})
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._cost_microusd = cost_microusd
        self._result_deterministic = result_deterministic
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
            deterministic=self._result_deterministic,
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
    with pytest.raises(OutputSchemaError, match="missing required") as caught:
        gateway.invoke(_request())

    assert caught.value.completion is not None
    assert caught.value.completion.output_digest == json_digest({"unexpected": True})


def test_gateway_postflight_budget_failure_carries_content_free_completion() -> None:
    adapter = UsageAdapter(output_tokens=21)
    gateway = ModelGateway(
        (_profile("reason", ModelCapability.REASON),),
        {"recorded": adapter},
        clock=lambda: NOW,
    )

    with pytest.raises(GatewayAdmissionError, match="output-token") as caught:
        gateway.invoke(_request())

    completion = caught.value.completion
    assert completion is not None
    assert completion.output_digest == json_digest({"answer": "ready"})
    assert completion.output_tokens == 21
    assert completion.completed_at == NOW
    assert not hasattr(completion, "output")


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


def test_gateway_rejects_adapter_registry_identity_mismatch() -> None:
    adapter = RecordedModelAdapter(
        "actual-id",
        {},
        capabilities={ModelCapability.REASON},
    )

    with pytest.raises(ValueError, match=r"registry key.*does not match adapter_id"):
        ModelGateway(
            (_profile("reason", ModelCapability.REASON),),
            {"recorded": adapter},
        )


def test_gateway_rejects_determinism_downgrade_from_advertised_profile() -> None:
    adapter = UsageAdapter(result_deterministic=False)
    gateway = ModelGateway(
        (_profile("reason", ModelCapability.REASON),),
        {"recorded": adapter},
    )

    with pytest.raises(GatewayAdmissionError, match="deterministic profile"):
        gateway.invoke(_request(deterministic_required=False))


def test_output_schema_recursively_validates_objects_arrays_and_const() -> None:
    schema: dict[str, JsonValue] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ("payload",),
        "properties": {
            "payload": {
                "type": "object",
                "additionalProperties": False,
                "required": ("version", "items"),
                "properties": {
                    "version": {"type": "string", "const": "v1"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ("name", "score"),
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "minLength": 2,
                                    "maxLength": 8,
                                },
                                "score": {
                                    "type": "number",
                                    "exclusiveMinimum": 0,
                                    "exclusiveMaximum": 1,
                                },
                            },
                        },
                    },
                },
            }
        },
    }

    validate_output(
        {"payload": {"version": "v1", "items": ({"name": "ready", "score": 0.5},)}},
        schema,
    )

    invalid_outputs: tuple[tuple[dict[str, JsonValue], str], ...] = (
        (
            {"payload": {"version": "v1", "items": ({"score": 0.5},)}},
            "missing required fields",
        ),
        (
            {
                "payload": {
                    "version": "v1",
                    "items": ({"name": "ready", "score": 0.5, "extra": True},),
                }
            },
            "undeclared fields",
        ),
        (
            {"payload": {"version": "v2", "items": ()}},
            "const value",
        ),
        (
            {"payload": {"version": "v1", "items": ({"name": 1, "score": 0.5},)}},
            "does not match type",
        ),
    )
    for output, message in invalid_outputs:
        with pytest.raises(OutputSchemaError, match=message):
            validate_output(output, schema)


@pytest.mark.parametrize(
    ("keyword", "boundary", "value", "expected_message"),
    (
        ("minimum", 1, 0, "less than minimum"),
        ("maximum", 1, 2, "exceeds maximum"),
        ("exclusiveMinimum", 1, 1, "greater than exclusiveMinimum"),
        ("exclusiveMaximum", 1, 1, "less than exclusiveMaximum"),
    ),
)
def test_output_schema_enforces_numeric_boundaries(
    keyword: str,
    boundary: int,
    value: int,
    expected_message: str,
) -> None:
    schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {"value": {"type": "number", keyword: boundary}},
    }

    with pytest.raises(OutputSchemaError, match=expected_message):
        validate_output({"value": value}, schema)


@pytest.mark.parametrize(
    ("keyword", "boundary", "value", "expected_message"),
    (
        ("minLength", 2, "x", "shorter than minLength"),
        ("maxLength", 2, "xxx", "longer than maxLength"),
    ),
)
def test_output_schema_enforces_string_boundaries(
    keyword: str,
    boundary: int,
    value: str,
    expected_message: str,
) -> None:
    schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {"value": {"type": "string", keyword: boundary}},
    }

    with pytest.raises(OutputSchemaError, match=expected_message):
        validate_output({"value": value}, schema)


def test_output_schema_rejects_unknown_keyword_in_unvisited_branch() -> None:
    schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {
            "optional": {
                "type": "string",
                "pattern": "unsupported-regex",
            }
        },
    }

    with pytest.raises(OutputSchemaError, match="unsupported output schema keywords"):
        validate_output({}, schema)


def test_output_schema_rejects_schema_valued_additional_properties() -> None:
    schema: dict[str, JsonValue] = {
        "type": "object",
        "additionalProperties": {"type": "string"},
    }

    with pytest.raises(OutputSchemaError, match="additionalProperties must be a boolean"):
        validate_output({}, schema)


def test_output_schema_enforces_enum_max_items_and_unique_items() -> None:
    schema: dict[str, JsonValue] = {
        "type": "object",
        "additionalProperties": False,
        "required": ("values",),
        "properties": {
            "values": {
                "type": "array",
                "maxItems": 2,
                "uniqueItems": True,
                "items": {"type": "string", "enum": ("left", "right")},
            }
        },
    }

    validate_output({"values": ("left", "right")}, schema)
    for values, message in (
        (("outside",), "enum value"),
        (("left", "right", "left"), "maxItems"),
        (("left", "left"), "duplicate items"),
    ):
        with pytest.raises(OutputSchemaError, match=message):
            validate_output({"values": values}, schema)


@pytest.mark.parametrize(
    ("property_schema", "message"),
    (
        ({"type": "string", "enum": ()}, "non-empty"),
        ({"type": "string", "enum": ("same", "same")}, "duplicates"),
        ({"type": "array", "maxItems": -1}, "non-negative"),
        ({"type": "array", "uniqueItems": 1}, "boolean"),
    ),
)
def test_output_schema_rejects_invalid_collection_constraints(
    property_schema: dict[str, JsonValue],
    message: str,
) -> None:
    schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {"value": property_schema},
    }

    with pytest.raises(OutputSchemaError, match=message):
        validate_output({}, schema)


def _request(
    *,
    capability: ModelCapability = ModelCapability.REASON,
    classification: DataClassification = DataClassification.INTERNAL,
    locality: LocalityPolicy = LocalityPolicy.REMOTE_ALLOWED,
    estimated_input_tokens: int = 10,
    tools_allowed: bool = False,
    budget: GatewayBudget | None = None,
    deterministic_required: bool = True,
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
        deterministic_required=deterministic_required,
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
