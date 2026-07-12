from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

import pytest

from blackcell.features.authorize_action import AffordancePolicy
from blackcell.features.build_context import BuildContext
from blackcell.features.derive_signal_packet import DeriveSignalPacket
from blackcell.features.evaluate_outcome import EvaluationCriterion, EvaluationSpec
from blackcell.features.execute_affordance import (
    AffordanceArgumentSpec,
    AffordanceDefinition,
    SideEffectClass,
)
from blackcell.features.ingest_observation import (
    EvidencePointer,
    IngestObservation,
    ObservationInput,
    ObservedClaim,
)
from blackcell.features.request_decision import (
    DecisionBudget,
    DecisionCapability,
    DecisionClassification,
    DecisionLocality,
    DecisionRequirements,
)
from blackcell.features.retrieve_evidence import EvidenceKey, RetrieveEvidence
from blackcell.features.solve_constraints import (
    ConstraintDefinition,
    ConstraintOperator,
    SolveConstraints,
)
from blackcell.kernel._json import canonical_json_bytes
from blackcell.workflows.daily_operator_v2_identity import (
    DAILY_OPERATOR_REQUEST_V2_MEDIA_TYPE,
    DAILY_OPERATOR_REQUEST_V2_SCHEMA_VERSION,
    DailyOperatorV2RequestCodecError,
    daily_operator_v2_request_digest,
    daily_operator_v2_request_identity_payload,
    daily_operator_v2_request_payload,
    decode_daily_operator_v2_request,
    encode_daily_operator_v2_request,
)
from blackcell.workflows.daily_operator_v2_request import DailyOperatorV2Request
from blackcell.workflows.run_protocol import RUN_WORKFLOW_VERSION_V2

NOW = datetime(2026, 7, 12, 18, tzinfo=UTC)
GOLDEN_DIGEST = "sha256:1df1dc4acc1ba90754d19abc2919f7c5fb196b95d348ac27f4843e89f3794c04"


def test_v2_request_golden_identity_and_strict_roundtrip() -> None:
    request = _request()
    encoded = encode_daily_operator_v2_request(request)
    payload = json.loads(encoded)

    assert DAILY_OPERATOR_REQUEST_V2_SCHEMA_VERSION == "daily-operator-request/v2"
    assert (
        DAILY_OPERATOR_REQUEST_V2_MEDIA_TYPE
        == "application/vnd.blackcell.daily-operator-request+json"
    )
    assert payload["schema_version"] == DAILY_OPERATOR_REQUEST_V2_SCHEMA_VERSION
    assert payload["workflow_version"] == RUN_WORKFLOW_VERSION_V2
    assert payload["request_digest"] == GOLDEN_DIGEST
    assert payload["evaluation_spec"]["spec_id"] == request.evaluation_spec.spec_id
    assert payload["initial_effective_time_cutoff"] == "2026-07-12T18:00:00+00:00"
    assert decode_daily_operator_v2_request(encoded) == request
    assert encode_daily_operator_v2_request(decode_daily_operator_v2_request(encoded)) == encoded
    assert daily_operator_v2_request_digest(request) == GOLDEN_DIGEST
    assert daily_operator_v2_request_payload(request)["request_digest"] == GOLDEN_DIGEST


def test_request_is_frozen_and_normalizes_set_semantics_and_instants() -> None:
    request = _request()
    offset = timezone(-timedelta(hours=5))
    same = NOW.astimezone(offset)
    observation = request.ingestion.observations[0]
    constraint = request.constraints.constraints[0]
    reordered = DailyOperatorV2Request(
        run_id=request.run_id,
        ingestion=replace(
            request.ingestion,
            observations=(
                replace(
                    observation,
                    effective_at=same,
                    claims=(replace(observation.claims[0], expires_at=same + timedelta(hours=1)),),
                ),
            ),
        ),
        initial_effective_time_cutoff=same,
        signal=replace(request.signal, generated_at=same),
        retrieval=request.retrieval,
        context=replace(request.context, generated_at=same),
        constraints=replace(
            request.constraints,
            evaluated_at=same,
            constraints=(
                replace(
                    constraint,
                    expected_values=("ready", "queued", "ready"),
                ),
            ),
        ),
        evaluation_spec=EvaluationSpec(
            request.evaluation_spec.name,
            request.evaluation_spec.objective,
            tuple(reversed(request.evaluation_spec.criteria)),
        ),
        gateway_requirements=replace(request.gateway_requirements, requested_at=same),
        authorization_affordance=replace(
            request.authorization_affordance,
            allowed_arguments=tuple(reversed(request.authorization_affordance.allowed_arguments)),
        ),
        execution_affordance=replace(
            request.execution_affordance,
            arguments=tuple(reversed(request.execution_affordance.arguments)),
        ),
        invocation_id=request.invocation_id,
        idempotency_key=request.idempotency_key,
        expected_observer_id=request.expected_observer_id,
        expected_observer_contract_version=request.expected_observer_contract_version,
    )

    assert reordered == request
    assert encode_daily_operator_v2_request(reordered) == encode_daily_operator_v2_request(request)
    assert reordered.initial_effective_time_cutoff.tzinfo is UTC
    assert reordered.authorization_affordance.allowed_arguments == ("encoding", "path")
    assert tuple(item.name for item in reordered.execution_affordance.arguments) == (
        "encoding",
        "path",
    )
    with pytest.raises(FrozenInstanceError):
        reordered.run_id = "run:changed"  # ty: ignore[invalid-assignment]


@pytest.mark.parametrize(
    "mutate",
    (
        lambda item: replace(
            item,
            ingestion=replace(
                item.ingestion,
                observations=(
                    replace(
                        item.ingestion.observations[0],
                        evidence=(EvidencePointer(locator="fixture://changed"),),
                    ),
                ),
            ),
        ),
        lambda item: replace(item, signal=replace(item.signal, stale_after_seconds=1)),
        lambda item: replace(item, retrieval=replace(item.retrieval, max_results=2)),
        lambda item: replace(item, context=replace(item.context, max_characters=99)),
        lambda item: replace(
            item,
            constraints=replace(
                item.constraints,
                constraints=(
                    replace(item.constraints.constraints[0], expected_values=("blocked",)),
                ),
            ),
        ),
        lambda item: replace(
            item,
            evaluation_spec=EvaluationSpec(
                item.evaluation_spec.name,
                item.evaluation_spec.objective,
                (
                    replace(
                        item.evaluation_spec.criteria[0],
                        expected_value="changed",
                    ),
                    *item.evaluation_spec.criteria[1:],
                ),
            ),
        ),
        lambda item: replace(
            item,
            gateway_requirements=replace(
                item.gateway_requirements,
                budget=replace(item.gateway_requirements.budget, max_cost_microusd=99),
            ),
        ),
        lambda item: replace(
            item,
            authorization_affordance=replace(item.authorization_affordance, external=True),
        ),
        lambda item: replace(
            item,
            execution_affordance=replace(item.execution_affordance, timeout_seconds=20),
        ),
        lambda item: replace(item, invocation_id="invocation:changed"),
        lambda item: replace(item, idempotency_key="daily:changed"),
        lambda item: replace(item, expected_observer_id="observer:changed"),
        lambda item: replace(
            item,
            expected_observer_contract_version="observer/v2",
        ),
        lambda item: replace(item, approval_granted=True),
    ),
)
def test_every_material_nested_change_changes_request_identity(mutate) -> None:
    request = _request()

    assert daily_operator_v2_request_digest(mutate(request)) != daily_operator_v2_request_digest(
        request
    )


def test_evaluation_spec_content_is_part_of_request_identity_not_only_its_name() -> None:
    request = _request()
    changed_spec = EvaluationSpec(
        request.evaluation_spec.name,
        request.evaluation_spec.objective,
        (
            replace(request.evaluation_spec.criteria[0], minimum_confidence=0.95),
            *request.evaluation_spec.criteria[1:],
        ),
    )
    changed = replace(request, evaluation_spec=changed_spec)

    assert changed.evaluation_spec.name == request.evaluation_spec.name
    assert changed.evaluation_spec.spec_id != request.evaluation_spec.spec_id
    assert daily_operator_v2_request_digest(changed) != daily_operator_v2_request_digest(request)


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (
            lambda item: replace(
                item,
                ingestion=replace(item.ingestion, correlation_id="run:other"),
            ),
            "correlation_id",
        ),
        (
            lambda item: replace(
                item,
                ingestion=replace(item.ingestion, causation_id="event:caller"),
            ),
            "owns ingestion causation",
        ),
        (
            lambda item: replace(
                item,
                initial_effective_time_cutoff=NOW - timedelta(seconds=1),
            ),
            "must include",
        ),
        (
            lambda item: replace(
                item,
                signal=replace(item.signal, generated_at=NOW - timedelta(seconds=1)),
            ),
            "signal generation",
        ),
        (
            lambda item: replace(
                item,
                context=replace(item.context, generated_at=NOW - timedelta(seconds=1)),
            ),
            "context generation",
        ),
        (
            lambda item: replace(
                item,
                gateway_requirements=replace(
                    item.gateway_requirements,
                    requested_at=NOW - timedelta(seconds=1),
                ),
            ),
            "gateway request",
        ),
        (
            lambda item: replace(
                item,
                constraints=replace(
                    item.constraints,
                    evaluated_at=NOW - timedelta(seconds=1),
                ),
            ),
            "constraint evaluation",
        ),
        (
            lambda item: replace(
                item,
                retrieval=replace(item.retrieval, objective="other objective"),
            ),
            "retrieval and context",
        ),
        (
            lambda item: replace(
                item,
                evaluation_spec=EvaluationSpec(
                    item.evaluation_spec.name,
                    "other objective",
                    item.evaluation_spec.criteria,
                ),
            ),
            "EvaluationSpec",
        ),
        (
            lambda item: replace(
                item,
                gateway_requirements=replace(
                    item.gateway_requirements,
                    capability=DecisionCapability.CODE,
                ),
            ),
            "reason capability",
        ),
        (
            lambda item: replace(
                item,
                execution_affordance=replace(item.execution_affordance, name="other"),
            ),
            "affordances must match",
        ),
        (
            lambda item: replace(
                item,
                execution_affordance=replace(
                    item.execution_affordance,
                    side_effect_class=SideEffectClass.REVERSIBLE,
                ),
            ),
            "side-effect classes",
        ),
        (
            lambda item: replace(
                item,
                authorization_affordance=replace(
                    item.authorization_affordance,
                    allowed_arguments=("path",),
                ),
            ),
            "arguments must match",
        ),
        (
            lambda item: replace(
                item,
                initial_effective_time_cutoff=datetime(2026, 7, 12, 18),
            ),
            "timezone-aware",
        ),
        (
            lambda item: replace(item, approval_granted=cast("bool", 1)),
            "must be a boolean",
        ),
    ),
)
def test_request_rejects_cross_field_contract_violations(mutate, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        mutate(_request())


@pytest.mark.parametrize(
    "mutation",
    (
        lambda payload: payload.update({"unexpected": True}),
        lambda payload: payload["ingestion"].update({"unexpected": True}),
        lambda payload: payload["evaluation_spec"].update({"unexpected": True}),
        lambda payload: payload["gateway_requirements"]["budget"].update({"unexpected": True}),
        lambda payload: payload["execution_affordance"]["arguments"][0].update(
            {"unexpected": True}
        ),
    ),
)
def test_decoder_rejects_unknown_fields_at_every_owned_boundary(mutation) -> None:
    payload = _mutable_payload()
    mutation(payload)

    with pytest.raises(
        DailyOperatorV2RequestCodecError,
        match=r"fields differ|owner artifact contract",
    ):
        decode_daily_operator_v2_request(canonical_json_bytes(payload))


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda payload: payload.update(request_digest="sha256:" + "0" * 64), "request_digest"),
        (lambda payload: payload.update(approval_granted=1), "must be a boolean"),
        (
            lambda payload: payload["retrieval"].update(max_results=True),
            "must be an integer",
        ),
        (
            lambda payload: payload["execution_affordance"].update(timeout_seconds="10"),
            "must be numeric",
        ),
        (
            lambda payload: payload["gateway_requirements"].update(capability="unknown"),
            "not recognized",
        ),
        (
            lambda payload: payload.update(initial_effective_time_cutoff="2026-07-12T18:00:00"),
            "timezone-aware",
        ),
    ),
)
def test_decoder_rejects_false_identity_and_malformed_typed_fields(mutation, message: str) -> None:
    payload = _mutable_payload()
    mutation(payload)

    with pytest.raises(DailyOperatorV2RequestCodecError, match=message):
        decode_daily_operator_v2_request(canonical_json_bytes(payload))


@pytest.mark.parametrize(
    "data",
    (
        b"not-json",
        b"[]",
        b'{"run_id": "pretty"}',
        b"\xff",
    ),
)
def test_decoder_rejects_malformed_or_noncanonical_json(data: bytes) -> None:
    with pytest.raises(DailyOperatorV2RequestCodecError):
        decode_daily_operator_v2_request(data)


def test_decoder_rejects_noncanonical_domain_order_and_non_utc_instants() -> None:
    ordered = _mutable_payload()
    ordered["authorization_affordance"]["allowed_arguments"].reverse()
    ordered["execution_affordance"]["arguments"].reverse()
    with pytest.raises(DailyOperatorV2RequestCodecError, match="canonical domain ordering"):
        decode_daily_operator_v2_request(canonical_json_bytes(ordered))

    timestamp = _mutable_payload()
    timestamp["signal"]["generated_at"] = "2026-07-12T13:00:00-05:00"
    with pytest.raises(DailyOperatorV2RequestCodecError, match="canonical domain ordering"):
        decode_daily_operator_v2_request(canonical_json_bytes(timestamp))


def test_identity_payload_excludes_its_digest_but_persisted_payload_binds_it() -> None:
    request = _request()

    assert "request_digest" not in daily_operator_v2_request_identity_payload(request)
    assert daily_operator_v2_request_payload(request)[
        "request_digest"
    ] == daily_operator_v2_request_digest(request)


def _request() -> DailyOperatorV2Request:
    objective = "inspect project status"
    observation = ObservationInput(
        "observation:1",
        NOW,
        (
            ObservedClaim(
                "claim:status",
                "project:blackcell",
                "status",
                "ready",
                0.9,
                NOW + timedelta(hours=1),
            ),
        ),
        (
            EvidencePointer(
                locator="fixture://status",
                artifact_id="artifact:status",
                digest="sha256:" + "a" * 64,
            ),
        ),
        "ingest:observation:1",
    )
    constraint = ConstraintDefinition(
        "constraint:status",
        "status must be actionable",
        "project:blackcell",
        "status",
        ConstraintOperator.IN,
        ("ready", "queued"),
        0.75,
        3_600,
    )
    evaluation = EvaluationSpec(
        "daily-success",
        objective,
        (
            EvaluationCriterion(
                "criterion:status",
                "project:blackcell",
                "status",
                "ready",
                0.8,
                True,
            ),
            EvaluationCriterion(
                "criterion:owner",
                "project:blackcell",
                "owner",
                "operator",
                0.5,
                False,
            ),
        ),
    )
    return DailyOperatorV2Request(
        run_id="run:daily:1",
        ingestion=IngestObservation(
            "observations:daily",
            0,
            "operator",
            "fixture",
            "run:daily:1",
            (observation,),
        ),
        initial_effective_time_cutoff=NOW,
        signal=DeriveSignalPacket("daily", NOW, 86_400),
        retrieval=RetrieveEvidence(
            objective,
            (
                EvidenceKey("project:blackcell", "status"),
                EvidenceKey("project:blackcell", "owner"),
            ),
            12,
        ),
        context=BuildContext("task:daily", objective, NOW, 12_000),
        constraints=SolveConstraints(NOW, (constraint,)),
        evaluation_spec=evaluation,
        gateway_requirements=DecisionRequirements(
            "decision:daily:1",
            "node:planner",
            DecisionCapability.REASON,
            DecisionClassification.PRIVATE,
            DecisionLocality.LOCAL_ONLY,
            DecisionBudget(2_048, 512, 5_000, 1_000),
            512,
            True,
            NOW,
        ),
        authorization_affordance=AffordancePolicy(
            "inspect",
            True,
            allowed_arguments=("path", "encoding"),
        ),
        execution_affordance=AffordanceDefinition(
            "inspect",
            "adapter:fixture",
            SideEffectClass.READ_ONLY,
            10,
            (
                AffordanceArgumentSpec("path"),
                AffordanceArgumentSpec("encoding", required=False),
            ),
        ),
        invocation_id="invocation:daily:1",
        idempotency_key="daily:1",
        expected_observer_id="observer:fixture",
        expected_observer_contract_version="observer-fixture/v1",
    )


def _mutable_payload() -> dict[str, Any]:
    return json.loads(encode_daily_operator_v2_request(_request()))
