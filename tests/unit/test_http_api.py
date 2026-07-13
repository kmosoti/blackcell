from __future__ import annotations

from typing import Any

import pytest
from litestar.testing import TestClient

from blackcell.config import SecretValue
from blackcell.interfaces import (
    BearerAuthenticator,
    ScopeAuthorizer,
    ServicePrincipal,
    ServiceScope,
)
from blackcell.interfaces.http import (
    ApprovalRequest,
    ContextResponse,
    EvaluationResponse,
    EventPageResponse,
    HealthResponse,
    ObservationIngestRequest,
    ObservationIngestResponse,
    OrchestrationApprovalResponse,
    OrchestrationRunResponse,
    ReplayResponse,
    RunResponse,
    RunSubmissionRequest,
    RuntimeApiError,
    RuntimeApiFailureCode,
    SlidingWindowRequestQuota,
    create_http_app,
)

TOKEN = "Runtime-v1_http-token.0123456789-ABCDEFG"


class FakeRuntimeApi:
    def __init__(self) -> None:
        self.principal_ids: list[str] = []

    def readiness(self) -> HealthResponse:
        return HealthResponse(status="ready")

    def ingest_observations(
        self,
        request: ObservationIngestRequest,
        *,
        principal_id: str,
    ) -> ObservationIngestResponse:
        self.principal_ids.append(principal_id)
        return ObservationIngestResponse(
            stream_id=request.stream_id,
            event_ids=("event-1",),
            first_sequence=request.expected_sequence + 1,
            last_sequence=request.expected_sequence + 1,
        )

    def submit_run(
        self,
        request: RunSubmissionRequest,
        *,
        principal_id: str,
    ) -> RunResponse:
        self.principal_ids.append(principal_id)
        return _run_response(objective=request.objective)

    def inspect_run(self, run_id: str) -> RunResponse:
        if run_id == "missing":
            raise RuntimeApiError(RuntimeApiFailureCode.NOT_FOUND)
        if run_id == "conflict":
            raise RuntimeApiError(RuntimeApiFailureCode.CONFLICT)
        if run_id == "explode":
            raise RuntimeError(f"provider failed with {TOKEN}")
        return _run_response(run_id=run_id)

    def inspect_context(self, run_id: str) -> ContextResponse:
        return ContextResponse(
            run_id=run_id,
            frame_id="frame-1",
            artifact_digest="sha256:" + "a" * 64,
            payload={"objective": "fixture"},
        )

    def replay_run(self, run_id: str) -> ReplayResponse:
        return ReplayResponse(
            run_id=run_id,
            run_stream_id=f"daily-operator-run:{run_id}",
            protocol_version="daily-operator/v2",
            classification="completed",
            outcome="succeeded",
            events=(),
            artifacts=(),
            projections=(),
            finding=None,
        )

    def inspect_evaluation(self, run_id: str) -> EvaluationResponse:
        return EvaluationResponse(
            run_id=run_id,
            evaluation_id="evaluation-1",
            evaluation_spec_id="spec-1",
            verdict="pass",
            artifact_digest="sha256:" + "b" * 64,
        )

    def list_events(self, *, after_position: int, limit: int) -> EventPageResponse:
        return EventPageResponse(
            after_position=after_position,
            limit=limit,
            events=(),
            next_after_position=after_position,
        )

    def inspect_orchestration(self, run_id: str) -> OrchestrationRunResponse:
        return OrchestrationRunResponse(
            run_id=run_id,
            dag_id="dag-1",
            dag_digest="digest-1",
            status="pending",
            submitted_by="submitter",
            submitted_at="2026-07-13T12:00:00+00:00",
            updated_at="2026-07-13T12:00:00+00:00",
            nodes=(),
            approvals=(),
        )

    def record_orchestration_approval(
        self,
        run_id: str,
        node_id: str,
        request: ApprovalRequest,
        *,
        principal_id: str,
    ) -> OrchestrationApprovalResponse:
        del run_id
        self.principal_ids.append(principal_id)
        return OrchestrationApprovalResponse(
            node_id=node_id,
            role=request.role,
            principal_id=principal_id,
            approved=request.approved,
            decided_at="2026-07-13T12:00:00+00:00",
            decision_digest="decision-1",
        )


def test_health_routes_are_public_and_openapi_is_not_exposed() -> None:
    service = FakeRuntimeApi()
    with _client(service) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        openapi = client.get("/schema/openapi.json")

    assert live.status_code == 200
    assert live.json() == {"status": "live", "schema_version": "health/v1"}
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert openapi.status_code == 404
    assert openapi.json() == {"error": "not-found", "schema_version": "error/v1"}


@pytest.mark.parametrize(
    "headers",
    (
        {},
        {"authorization": "Bearer wrong-credential"},
        {"authorization": f"Bearer {TOKEN},Bearer {TOKEN}"},
        {"authorization": f"Basic {TOKEN}"},
    ),
)
def test_protected_routes_fail_with_content_free_authentication_errors(
    headers: dict[str, str],
) -> None:
    with _client(FakeRuntimeApi()) as client:
        response = client.get("/api/v1/events", headers=headers)

    assert response.status_code == 401
    assert response.json() == {
        "error": "authentication-required",
        "schema_version": "error/v1",
    }
    assert TOKEN not in response.text
    assert response.headers["www-authenticate"] == "Bearer"


def test_duplicate_authorization_headers_are_not_comma_folded() -> None:
    headers = [
        ("authorization", f"Bearer {TOKEN}"),
        ("authorization", f"Bearer {TOKEN}"),
    ]
    with _client(FakeRuntimeApi()) as client:
        response = client.get("/api/v1/events", headers=headers)

    assert response.status_code == 401
    assert response.json()["error"] == "authentication-required"


def test_route_scope_is_explicit_and_admin_does_not_expand_it() -> None:
    service = FakeRuntimeApi()
    with _client(service, scopes=(ServiceScope.READ,)) as client:
        readable = client.get("/api/v1/events", headers=_auth())
        forbidden = client.post(
            "/api/v1/runs",
            headers=_auth(),
            json=_run_request(),
        )
    with _client(FakeRuntimeApi(), scopes=(ServiceScope.ADMIN,)) as client:
        admin_only = client.get("/api/v1/events", headers=_auth())

    assert readable.status_code == 200
    assert forbidden.status_code == 403
    assert forbidden.json()["error"] == "insufficient-scope"
    assert admin_only.status_code == 403


def test_run_submission_uses_strict_contract_and_authenticated_principal() -> None:
    service = FakeRuntimeApi()
    with _client(service) as client:
        accepted = client.post(
            "/api/v1/runs",
            headers=_auth(),
            json=_run_request(),
        )
        unknown = client.post(
            "/api/v1/runs",
            headers=_auth(),
            json={**_run_request(), "extra": "rejected"},
        )
        wrong_media = client.post(
            "/api/v1/runs",
            headers={**_auth(), "content-type": "text/plain"},
            content=b"{}",
        )

    assert accepted.status_code == 201
    assert accepted.json()["schema_version"] == "runtime-run/v1"
    assert service.principal_ids == ["client:test"]
    assert unknown.status_code == 400
    assert unknown.json()["error"] == "invalid-request"
    assert wrong_media.status_code == 415
    assert wrong_media.json()["error"] == "unsupported-media-type"


def test_request_body_limit_fails_before_contract_decode() -> None:
    with _client(FakeRuntimeApi()) as client:
        response = client.post(
            "/api/v1/runs",
            headers={**_auth(), "content-type": "application/json"},
            content=b"x" * 1_048_577,
        )

    assert response.status_code == 413
    assert response.json()["error"] == "request-too-large"


def test_observation_and_approval_actor_identity_comes_from_authentication() -> None:
    service = FakeRuntimeApi()
    observation = {
        "schema_version": "observation-ingest-request/v1",
        "stream_id": "observation:fixture",
        "expected_sequence": 0,
        "source": "fixture/v1",
        "correlation_id": "correlation-1",
        "observations": [
            {
                "observation_id": "observation-1",
                "effective_at": "2026-07-13T12:00:00Z",
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "subject": "repository",
                        "predicate": "git.clean",
                        "value": True,
                    }
                ],
                "evidence": [{"locator": "fixture://status"}],
            }
        ],
    }
    approval = {
        "schema_version": "orchestration-approval-request/v1",
        "role": "reviewer",
        "approved": True,
    }

    with _client(service) as client:
        observed = client.post(
            "/api/v1/observations",
            headers=_auth(),
            json=observation,
        )
        approved = client.post(
            "/api/v1/orchestration/runs/run-1/nodes/node-1/approvals",
            headers=_auth(),
            json=approval,
        )

    assert observed.status_code == 201
    assert approved.status_code == 200
    assert approved.json()["principal_id"] == "client:test"
    assert service.principal_ids == ["client:test", "client:test"]


def test_query_and_service_failures_are_bounded_and_content_free() -> None:
    with _client(FakeRuntimeApi()) as client:
        bad_limit = client.get("/api/v1/events?limit=201", headers=_auth())
        missing = client.get("/api/v1/runs/missing", headers=_auth())
        conflict = client.get("/api/v1/runs/conflict", headers=_auth())
        internal = client.get("/api/v1/runs/explode", headers=_auth())

    assert bad_limit.status_code == 400
    assert missing.status_code == 404
    assert conflict.status_code == 409
    assert internal.status_code == 500
    assert internal.json()["error"] == "internal-error"
    assert TOKEN not in internal.text


def test_request_quota_counts_failed_authentication_and_exempts_health() -> None:
    quota = SlidingWindowRequestQuota(2, monotonic_clock=lambda: 100.0)
    with _client(FakeRuntimeApi(), request_quota=quota) as client:
        first = client.get("/api/v1/events")
        second = client.get("/api/v1/events", headers=_auth())
        exhausted = client.get("/api/v1/events", headers=_auth())
        live = client.get("/health/live")
        ready = client.get("/health/ready")

    assert first.status_code == 401
    assert second.status_code == 200
    assert exhausted.status_code == 429
    assert exhausted.json()["error"] == "request-quota-exceeded"
    assert live.status_code == ready.status_code == 200


def _client(
    service: FakeRuntimeApi,
    *,
    scopes: tuple[ServiceScope, ...] = (
        ServiceScope.READ,
        ServiceScope.RUN,
        ServiceScope.APPROVE,
    ),
    request_quota: SlidingWindowRequestQuota | None = None,
) -> TestClient[Any]:
    principal = ServicePrincipal("client:test", scopes)
    app = create_http_app(
        service,
        authenticator=BearerAuthenticator(SecretValue(TOKEN), principal),
        authorizer=ScopeAuthorizer(),
        request_quota=request_quota,
    )
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"authorization": f"Bearer {TOKEN}"}


def _run_request() -> dict[str, object]:
    return {
        "schema_version": "run-submission-request/v1",
        "objective": "Inspect the repository",
        "approval_granted": False,
        "token_budget": 2_000,
        "character_budget": 8_000,
    }


def _run_response(
    *,
    run_id: str = "run-1",
    objective: str = "fixture",
) -> RunResponse:
    del objective
    return RunResponse(
        run_id=run_id,
        status="completed",
        outcome="succeeded",
        workflow_version="daily-operator/v2",
        repository_stream_id="repository:fixture",
        run_stream_id=f"daily-operator-run:{run_id}",
        context_frame_id="frame-1",
        authorization_outcome="allow",
        execution_status="succeeded",
        evaluation_verdict="pass",
        transition_recorded=True,
        run_event_count=20,
        artifact_count=10,
    )
