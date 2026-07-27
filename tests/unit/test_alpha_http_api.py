from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import msgspec
from litestar.testing import TestClient

from blackcell.bootstrap.alpha_runtime import AlphaRuntimeApiService
from blackcell.config import SecretValue
from blackcell.interfaces import (
    BearerAuthenticator,
    ScopeAuthorizer,
    ServicePrincipal,
    ServiceScope,
)
from blackcell.interfaces.http import (
    ALPHA_RUN_QUERY_MEDIA_TYPE,
    ALPHA_RUN_QUERY_RESULT_MEDIA_TYPE,
    AlphaCancelRunRequest,
    AlphaEventPageResponse,
    AlphaIntentRequest,
    AlphaIntentResponse,
    AlphaPlanRequest,
    AlphaPlanResponse,
    AlphaProjectRequest,
    AlphaProjectResponse,
    AlphaReplayResponse,
    AlphaRunQueryRequest,
    AlphaRunQueryResponse,
    AlphaRunRequest,
    AlphaRunResponse,
    create_http_app,
)
from blackcell.kernel import EventStore
from tests.unit.test_alpha_runtime import _base_commit, _repository

_TOKEN = "Alpha_http-token.0123456789-ABCDEFG"
_DIGEST = "sha256:" + ("a" * 64)


def test_alpha_routes_are_authenticated_typed_and_async(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    service = _AlphaHttpPort(
        AlphaRuntimeApiService(EventStore(tmp_path / "state.sqlite3"), repository)
    )

    with _client(service) as client:
        unauthenticated = client.get("/api/alpha/v1/events")
        project = client.post(
            "/api/alpha/v1/projects", json=_project_body(repository), headers=_auth()
        )
        intent = client.post("/api/alpha/v1/intents", json=_intent_body(), headers=_auth())
        plan = client.post(
            "/api/alpha/v1/plans",
            json=_plan_body(_base_commit(repository)),
            headers=_auth(),
        )
        run = client.post("/api/alpha/v1/runs", json=_run_body(), headers=_auth())
        status = client.get("/api/alpha/v1/runs/run-1/status", headers=_auth())
        events = client.get("/api/alpha/v1/events?after=0&limit=20", headers=_auth())
        replay = client.get("/api/alpha/v1/runs/run-1/replay", headers=_auth())

    assert unauthenticated.status_code == 401
    assert project.status_code == intent.status_code == plan.status_code == 201
    assert run.status_code == 202
    assert run.json()["status"] == "queued"
    assert run.json()["schema_version"] == "alpha-run/v1"
    assert status.status_code == 200
    assert status.json() == run.json()
    assert events.status_code == 200
    assert [item["event_type"] for item in events.json()["events"]] == [
        "alpha.project.registered",
        "alpha.intent.accepted",
        "alpha.plan.accepted",
        "alpha.run.queued",
    ]
    assert {item["event_schema_version"] for item in events.json()["events"]} == {1}
    assert replay.status_code == 200
    assert replay.json()["schema_version"] == "alpha-replay/v2"
    assert replay.json()["artifact_integrity"] == "not-applicable"
    assert replay.json()["artifacts"] == []
    assert replay.json()["findings"] == []
    assert replay.json()["processed_events"] == 4
    assert replay.json()["verification"]["schema_version"] == "alpha-verification-replay/v1"
    assert replay.json()["verification"]["lifecycle_status"] == "not-started"
    assert replay.json()["verification"]["artifact_integrity"] == "not-applicable"
    assert replay.json()["verification"]["verdict"] is None
    assert replay.json()["plan"]["topological_order"] == ["inspect", "verify"]
    assert service.principal_ids == ["client:test"] * 4


def test_alpha_route_rejects_malformed_version_before_service(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    service = _AlphaHttpPort(
        AlphaRuntimeApiService(EventStore(tmp_path / "state.sqlite3"), repository)
    )
    invalid = {**_run_body(), "schema_version": "alpha-run-request/v2"}

    with _client(service) as client:
        response = client.post("/api/alpha/v1/runs", json=invalid, headers=_auth())

    assert response.status_code == 400
    assert response.json() == {"error": "invalid-request", "schema_version": "error/v1"}
    assert service.run_submissions == 0


def test_alpha_cancel_route_is_authenticated_typed_and_async(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    service = _AlphaHttpPort(
        AlphaRuntimeApiService(EventStore(tmp_path / "state.sqlite3"), repository)
    )

    with _client(service) as client:
        client.post("/api/alpha/v1/projects", json=_project_body(repository), headers=_auth())
        client.post("/api/alpha/v1/intents", json=_intent_body(), headers=_auth())
        client.post(
            "/api/alpha/v1/plans",
            json=_plan_body(_base_commit(repository)),
            headers=_auth(),
        )
        client.post("/api/alpha/v1/runs", json=_run_body(), headers=_auth())
        unauthenticated = client.post("/api/alpha/v1/runs/run-1/cancel", json=_cancel_body())
        malformed = client.post(
            "/api/alpha/v1/runs/run-1/cancel",
            json={**_cancel_body(), "schema_version": "alpha-cancel-run-request/v2"},
            headers=_auth(),
        )
        canceled = client.post(
            "/api/alpha/v1/runs/run-1/cancel", json=_cancel_body(), headers=_auth()
        )

    assert unauthenticated.status_code == 401
    assert malformed.status_code == 400
    assert canceled.status_code == 202
    assert canceled.json()["status"] == "canceled"
    assert canceled.json()["cancellation_requested"] is True
    assert canceled.json()["active_node_id"] is None
    assert service.cancellations == 1
    assert service.principal_ids[-1] == "client:test"


def test_alpha_run_query_is_safe_cacheable_typed_and_discoverable(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    runtime = AlphaRuntimeApiService(EventStore(tmp_path / "state.sqlite3"), repository)
    service = _AlphaHttpPort(runtime)
    query = AlphaRunQueryRequest(
        schema_version="alpha-run-query-request/v1",
        statuses=("queued",),
        project_ids=("project-1",),
        limit=10,
    )
    headers = {
        **_auth(),
        "accept": ALPHA_RUN_QUERY_RESULT_MEDIA_TYPE,
        "content-type": ALPHA_RUN_QUERY_MEDIA_TYPE,
    }

    with _client(service) as client:
        client.post("/api/alpha/v1/projects", json=_project_body(repository), headers=_auth())
        client.post("/api/alpha/v1/intents", json=_intent_body(), headers=_auth())
        client.post(
            "/api/alpha/v1/plans",
            json=_plan_body(_base_commit(repository)),
            headers=_auth(),
        )
        client.post("/api/alpha/v1/runs", json=_run_body(), headers=_auth())
        before = runtime.list_events(after_cursor=0, limit=20)
        response = client.request(
            "QUERY",
            "/api/alpha/v1/run-query",
            content=msgspec.json.encode(query),
            headers=headers,
        )
        conditional = client.request(
            "QUERY",
            "/api/alpha/v1/run-query",
            content=msgspec.json.encode(query),
            headers={**headers, "if-none-match": response.headers["etag"]},
        )
        head = client.head("/api/alpha/v1/run-query")
        options = client.options("/api/alpha/v1/run-query")
        after = runtime.list_events(after_cursor=0, limit=20)

    decoded = msgspec.json.decode(response.content, type=AlphaRunQueryResponse)
    assert response.status_code == 200
    assert response.headers["content-type"].split(";", 1)[0] == ALPHA_RUN_QUERY_RESULT_MEDIA_TYPE
    assert response.headers["accept-query"] == ALPHA_RUN_QUERY_MEDIA_TYPE
    assert response.headers["allow"] == "HEAD, OPTIONS, QUERY"
    assert decoded.query == query
    assert tuple(item.run.run_id for item in decoded.runs) == ("run-1",)
    assert decoded.runs[0].run.status == "queued"
    assert tuple(node.node_id for node in decoded.runs[0].nodes) == ("inspect", "verify")
    assert conditional.status_code == 304
    assert conditional.content == b""
    assert head.status_code == 200
    assert head.content == b""
    assert options.status_code == 204
    assert options.headers["accept-query"] == ALPHA_RUN_QUERY_MEDIA_TYPE
    assert after == before


def test_alpha_run_query_rejects_boundary_failures_before_service(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    service = _AlphaHttpPort(
        AlphaRuntimeApiService(EventStore(tmp_path / "state.sqlite3"), repository)
    )
    valid = msgspec.json.encode(AlphaRunQueryRequest(schema_version="alpha-run-query-request/v1"))
    vendor_headers = {
        **_auth(),
        "accept": ALPHA_RUN_QUERY_RESULT_MEDIA_TYPE,
        "content-type": ALPHA_RUN_QUERY_MEDIA_TYPE,
    }

    with _client(service) as client:
        unauthenticated = client.request(
            "QUERY",
            "/api/alpha/v1/run-query",
            content=valid,
            headers={key: value for key, value in vendor_headers.items() if key != "authorization"},
        )
        missing_content_type = client.request(
            "QUERY",
            "/api/alpha/v1/run-query",
            content=valid,
            headers={**_auth(), "accept": ALPHA_RUN_QUERY_RESULT_MEDIA_TYPE},
        )
        unsupported = client.request(
            "QUERY",
            "/api/alpha/v1/run-query",
            content=valid,
            headers={**vendor_headers, "content-type": "application/json"},
        )
        unacceptable = client.request(
            "QUERY",
            "/api/alpha/v1/run-query",
            content=valid,
            headers={**vendor_headers, "accept": "text/plain"},
        )
        malformed = client.request(
            "QUERY",
            "/api/alpha/v1/run-query",
            content=b'{"limit": 1, "limit": 2}',
            headers=vendor_headers,
        )
        unprocessable = client.request(
            "QUERY",
            "/api/alpha/v1/run-query",
            content=b'{"schema_version":"alpha-run-query-request/v1","limit":0}',
            headers=vendor_headers,
        )
        wrong_method = client.post("/api/alpha/v1/run-query", headers=_auth())

    assert unauthenticated.status_code == 401
    assert missing_content_type.status_code == 400
    assert unsupported.status_code == 415
    assert unsupported.headers["accept-query"] == ALPHA_RUN_QUERY_MEDIA_TYPE
    assert unacceptable.status_code == 406
    assert malformed.status_code == 400
    assert unprocessable.status_code == 422
    assert wrong_method.status_code == 405
    assert service.run_queries == 0


class _AlphaHttpPort:
    def __init__(self, service: AlphaRuntimeApiService) -> None:
        self.service = service
        self.principal_ids: list[str] = []
        self.run_submissions = 0
        self.cancellations = 0
        self.run_queries = 0

    def register_alpha_project(
        self,
        request: AlphaProjectRequest,
        *,
        principal_id: str,
    ) -> AlphaProjectResponse:
        self.principal_ids.append(principal_id)
        return self.service.register_project(request, principal_id=principal_id)

    def accept_alpha_intent(
        self,
        request: AlphaIntentRequest,
        *,
        principal_id: str,
    ) -> AlphaIntentResponse:
        self.principal_ids.append(principal_id)
        return self.service.accept_intent(request, principal_id=principal_id)

    def accept_alpha_plan(
        self,
        request: AlphaPlanRequest,
        *,
        principal_id: str,
    ) -> AlphaPlanResponse:
        self.principal_ids.append(principal_id)
        return self.service.accept_plan(request, principal_id=principal_id)

    def submit_alpha_run(
        self,
        request: AlphaRunRequest,
        *,
        principal_id: str,
    ) -> AlphaRunResponse:
        self.run_submissions += 1
        self.principal_ids.append(principal_id)
        return self.service.submit_run(request, principal_id=principal_id)

    def inspect_alpha_run(self, run_id: str) -> AlphaRunResponse:
        return self.service.inspect_run(run_id)

    def query_alpha_runs(self, request: AlphaRunQueryRequest) -> AlphaRunQueryResponse:
        self.run_queries += 1
        return self.service.query_runs(request)

    def cancel_alpha_run(
        self,
        run_id: str,
        request: AlphaCancelRunRequest,
        *,
        principal_id: str,
    ) -> AlphaRunResponse:
        self.cancellations += 1
        self.principal_ids.append(principal_id)
        return self.service.cancel_run(run_id, request, principal_id=principal_id)

    def list_alpha_events(
        self,
        *,
        after_cursor: int,
        limit: int,
    ) -> AlphaEventPageResponse:
        return self.service.list_events(after_cursor=after_cursor, limit=limit)

    def replay_alpha_run(self, run_id: str) -> AlphaReplayResponse:
        return self.service.replay_run(run_id)


def _client(service: _AlphaHttpPort) -> TestClient[Any]:
    principal = ServicePrincipal(
        "client:test",
        (ServiceScope.READ, ServiceScope.RUN),
    )
    app = create_http_app(
        cast(Any, service),
        authenticator=BearerAuthenticator(SecretValue(_TOKEN), principal),
        authorizer=ScopeAuthorizer(),
    )
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"authorization": f"Bearer {_TOKEN}"}


def _project_body(repository: Path) -> dict[str, object]:
    return {
        "schema_version": "alpha-project-request/v1",
        "project_id": "project-1",
        "root": str(repository.resolve()),
        "configuration_provider": "kernform",
        "configuration_version": "0.2.0",
        "configuration_digest": _DIGEST,
        "idempotency_key": "project-1",
    }


def _intent_body() -> dict[str, object]:
    return {
        "schema_version": "alpha-intent-request/v1",
        "intent_id": "intent-1",
        "project_id": "project-1",
        "objective": "Implement the alpha contracts.",
        "constraints": ["Do not invoke V2."],
        "assumptions": ["The event ledger is reusable."],
        "unresolved_questions": ["Which executor is selected in A04?"],
        "idempotency_key": "intent-1",
    }


def _plan_body(base_commit: str) -> dict[str, object]:
    budget = {
        "max_input_tokens": 1_000,
        "max_output_tokens": 1_000,
        "timeout_seconds": 30,
        "max_cost_microusd": 0,
        "max_changed_files": 0,
    }
    return {
        "schema_version": "alpha-plan-request/v1",
        "plan_id": "plan-1",
        "project_id": "project-1",
        "intent_id": "intent-1",
        "base_commit": base_commit,
        "allowed_effects": ["repository-read", "process"],
        "nodes": [
            {
                "node_id": "inspect",
                "objective": "Inspect bounded source evidence.",
                "depends_on": [],
                "budget": budget,
                "effects": ["repository-read", "process"],
                "allowed_paths": [],
                "checks": [
                    {
                        "check_id": "inspect-pass",
                        "argv": ["python", "-m", "compileall", "src"],
                        "expected_exit_code": 0,
                    }
                ],
            },
            {
                "node_id": "verify",
                "objective": "Verify the declared outcome.",
                "depends_on": ["inspect"],
                "budget": budget,
                "effects": ["repository-read", "process"],
                "allowed_paths": [],
                "checks": [
                    {
                        "check_id": "verify-pass",
                        "argv": ["pytest", "tests/unit/test_alpha_runtime.py", "-q"],
                        "expected_exit_code": 0,
                    }
                ],
            },
        ],
        "idempotency_key": "plan-1",
    }


def _run_body() -> dict[str, object]:
    return {
        "schema_version": "alpha-run-request/v1",
        "run_id": "run-1",
        "project_id": "project-1",
        "intent_id": "intent-1",
        "plan_id": "plan-1",
        "idempotency_key": "run-1",
    }


def _cancel_body() -> dict[str, object]:
    return {
        "schema_version": "alpha-cancel-run-request/v1",
        "idempotency_key": "cancel-run-1",
    }
