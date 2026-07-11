import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from blackcell.features.authorize_action import AffordancePolicy
from blackcell.features.build_context import BuildContext
from blackcell.features.derive_signal_packet import DeriveSignalPacket
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
from blackcell.features.retrieve_evidence import EvidenceKey, RetrieveEvidence
from blackcell.features.solve_constraints import (
    ConstraintDefinition,
    ConstraintOperator,
    SolveConstraints,
)
from blackcell.kernel._json import canonical_json
from blackcell.workflows.daily_operator import DailyOperatorRequest
from blackcell.workflows.daily_operator_identity import (
    daily_operator_request_digest,
    daily_operator_request_payload,
)

NOW = datetime(2026, 7, 11, 16, tzinfo=UTC)


def test_daily_operator_request_identity_is_complete_and_inspectable() -> None:
    request = _request()
    payload = json.loads(canonical_json(daily_operator_request_payload(request)))

    assert payload["schema_version"] == "daily-operator-request/v1"
    assert payload["run_id"] == request.run_id
    assert payload["ingestion"]["observations"][0]["claims"][0]["value"] == "ready"
    assert payload["constraints"]["definitions"][0]["expected_values"] == [
        "queued",
        "ready",
    ]
    assert daily_operator_request_digest(request).startswith("sha256:")


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
        lambda item: replace(
            item,
            signal=replace(item.signal, stale_after_seconds=1),
        ),
        lambda item: replace(
            item,
            retrieval=replace(
                item.retrieval,
                required_keys=tuple(reversed(item.retrieval.required_keys)),
            ),
        ),
        lambda item: replace(
            item,
            context=replace(item.context, max_characters=99),
        ),
        lambda item: replace(
            item,
            constraints=replace(
                item.constraints,
                constraints=(
                    replace(
                        item.constraints.constraints[0],
                        expected_values=("ready", "blocked"),
                    ),
                ),
            ),
        ),
        lambda item: replace(
            item,
            authorization_affordance=replace(
                item.authorization_affordance,
                external=True,
            ),
        ),
        lambda item: replace(
            item,
            execution_affordance=replace(
                item.execution_affordance,
                timeout_seconds=20.0,
            ),
        ),
        lambda item: replace(item, idempotency_key="daily:changed"),
        lambda item: replace(item, approval_granted=True),
    ),
)
def test_every_material_nested_change_changes_request_identity(mutate) -> None:
    request = _request()

    assert daily_operator_request_digest(mutate(request)) != daily_operator_request_digest(request)


def test_request_identity_normalizes_instants_and_set_semantic_fields() -> None:
    request = _request()
    same_instant = NOW.astimezone(timezone(-timedelta(hours=5)))
    equivalent = replace(
        request,
        ingestion=replace(
            request.ingestion,
            observations=(replace(request.ingestion.observations[0], effective_at=same_instant),),
        ),
        signal=replace(request.signal, generated_at=same_instant),
        context=replace(request.context, generated_at=same_instant),
        constraints=replace(
            request.constraints,
            evaluated_at=same_instant,
            constraints=(
                replace(
                    request.constraints.constraints[0],
                    expected_values=tuple(
                        reversed(request.constraints.constraints[0].expected_values)
                    ),
                ),
            ),
        ),
        authorization_affordance=replace(
            request.authorization_affordance,
            allowed_arguments=tuple(reversed(request.authorization_affordance.allowed_arguments)),
        ),
        execution_affordance=replace(
            request.execution_affordance,
            arguments=tuple(reversed(request.execution_affordance.arguments)),
        ),
    )

    assert daily_operator_request_digest(equivalent) == daily_operator_request_digest(request)


def _request() -> DailyOperatorRequest:
    observation = ObservationInput(
        "obs:1",
        NOW,
        (ObservedClaim("claim:1", "project:blackcell", "status", "ready", 0.9),),
        (EvidencePointer(locator="fixture://status"),),
    )
    constraint = ConstraintDefinition(
        "status-policy",
        "status must be actionable",
        "project:blackcell",
        "status",
        ConstraintOperator.IN,
        ("ready", "queued"),
    )
    return DailyOperatorRequest(
        "run:1",
        IngestObservation(
            "observations:daily",
            0,
            "operator",
            "fixture",
            "run:1",
            (observation,),
        ),
        DeriveSignalPacket("daily", NOW),
        RetrieveEvidence(
            "inspect project status",
            required_keys=(
                EvidenceKey("project:blackcell", "status"),
                EvidenceKey("project:blackcell", "owner"),
            ),
        ),
        BuildContext("task:daily", "inspect project status", NOW),
        SolveConstraints(NOW, (constraint,)),
        AffordancePolicy(
            "inspect",
            True,
            allowed_arguments=("path", "encoding"),
        ),
        AffordanceDefinition(
            "inspect",
            "fixture",
            SideEffectClass.READ_ONLY,
            10.0,
            (
                AffordanceArgumentSpec("path"),
                AffordanceArgumentSpec("encoding", required=False),
            ),
        ),
        "invocation:1",
        "daily:1",
    )
