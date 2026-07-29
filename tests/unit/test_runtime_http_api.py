from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import msgspec
from litestar.testing import TestClient

from blackcell.bootstrap.runtime_service import RuntimeService
from blackcell.config import SecretValue
from blackcell.interfaces import (
    BearerAuthenticator,
    ScopeAuthorizer,
    ServicePrincipal,
    ServiceScope,
)
from blackcell.interfaces.http import (
    RUN_QUERY_MEDIA_TYPE,
    RUN_QUERY_RESULT_MEDIA_TYPE,
    CancelRunRequest,
    IntentRequest,
    IntentResponse,
    PlanRequest,
    PlanResponse,
    ProjectRequest,
    ProjectResponse,
    ReplayResponse,
    RunQueryItem,
    RunQueryRequest,
    RunQueryResponse,
    RunRequest,
    RunResponse,
    RunSurfaceSnapshot,
    RunSurfaceWindow,
    RuntimeEventPageResponse,
    create_http_app,
)
from blackcell.kernel import EventStore
from tests.unit.test_runtime_service import _base_commit, _repository

_TOKEN = "Runtime_http-token.0123456789-ABCDEFG"
_DIGEST = "sha256:" + ("a" * 64)


def test_runtime_routes_are_authenticated_typed_and_async(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    service = _HttpPort(RuntimeService(EventStore(tmp_path / "state.sqlite3"), repository))

    with _client(service) as client:
        unauthenticated = client.get("/api/v1/events")
        project = client.post("/api/v1/projects", json=_project_body(repository), headers=_auth())
        intent = client.post("/api/v1/intents", json=_intent_body(), headers=_auth())
        plan = client.post(
            "/api/v1/plans",
            json=_plan_body(_base_commit(repository)),
            headers=_auth(),
        )
        run = client.post("/api/v1/runs", json=_run_body(), headers=_auth())
        status = client.get("/api/v1/runs/run-1/status", headers=_auth())
        events = client.get("/api/v1/events?after=0&limit=20", headers=_auth())
        replay = client.get("/api/v1/runs/run-1/replay", headers=_auth())

    assert unauthenticated.status_code == 401
    assert project.status_code == intent.status_code == plan.status_code == 201
    assert run.status_code == 202
    assert run.json()["status"] == "queued"
    assert run.json()["schema_version"] == "run/v1"
    assert status.status_code == 200
    assert status.json() == run.json()
    assert events.status_code == 200
    assert [item["event_type"] for item in events.json()["events"]] == [
        "project.registered",
        "intent.accepted",
        "plan.accepted",
        "run.queued",
    ]
    assert {item["event_schema_version"] for item in events.json()["events"]} == {1}
    assert replay.status_code == 200
    assert replay.json()["schema_version"] == "replay/v2"
    assert replay.json()["artifact_integrity"] == "not-applicable"
    assert replay.json()["artifacts"] == []
    assert replay.json()["findings"] == []
    assert replay.json()["processed_events"] == 4
    assert replay.json()["verification"]["schema_version"] == "verification-replay/v1"
    assert replay.json()["verification"]["lifecycle_status"] == "not-started"
    assert replay.json()["verification"]["artifact_integrity"] == "not-applicable"
    assert replay.json()["verification"]["verdict"] is None
    assert replay.json()["plan"]["topological_order"] == ["inspect", "verify"]
    assert service.principal_ids == ["client:test"] * 4


def test_runtime_route_rejects_an_unsupported_schema_before_service(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    service = _HttpPort(RuntimeService(EventStore(tmp_path / "state.sqlite3"), repository))
    invalid = {**_run_body(), "schema_version": "run-request/v2"}

    with _client(service) as client:
        response = client.post("/api/v1/runs", json=invalid, headers=_auth())

    assert response.status_code == 400
    assert response.json() == {"error": "invalid-request", "schema_version": "error/v1"}
    assert service.run_submissions == 0


def test_runtime_cancel_route_is_authenticated_typed_and_async(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    service = _HttpPort(RuntimeService(EventStore(tmp_path / "state.sqlite3"), repository))

    with _client(service) as client:
        client.post("/api/v1/projects", json=_project_body(repository), headers=_auth())
        client.post("/api/v1/intents", json=_intent_body(), headers=_auth())
        client.post(
            "/api/v1/plans",
            json=_plan_body(_base_commit(repository)),
            headers=_auth(),
        )
        client.post("/api/v1/runs", json=_run_body(), headers=_auth())
        unauthenticated = client.post("/api/v1/runs/run-1/cancel", json=_cancel_body())
        malformed = client.post(
            "/api/v1/runs/run-1/cancel",
            json={**_cancel_body(), "schema_version": "execution-cancel-run-request/v2"},
            headers=_auth(),
        )
        canceled = client.post("/api/v1/runs/run-1/cancel", json=_cancel_body(), headers=_auth())

    assert unauthenticated.status_code == 401
    assert malformed.status_code == 400
    assert canceled.status_code == 202
    assert canceled.json()["status"] == "canceled"
    assert canceled.json()["cancellation_requested"] is True
    assert canceled.json()["active_node_id"] is None
    assert service.cancellations == 1
    assert service.principal_ids[-1] == "client:test"


def test_run_query_is_safe_cacheable_typed_and_discoverable(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    runtime = RuntimeService(EventStore(tmp_path / "state.sqlite3"), repository)
    service = _HttpPort(runtime)
    query = RunQueryRequest(
        schema_version="run-query-request/v1",
        statuses=("queued",),
        project_ids=("project-1",),
        limit=10,
    )
    headers = {
        **_auth(),
        "accept": RUN_QUERY_RESULT_MEDIA_TYPE,
        "content-type": RUN_QUERY_MEDIA_TYPE,
    }

    with _client(service) as client:
        client.post("/api/v1/projects", json=_project_body(repository), headers=_auth())
        client.post("/api/v1/intents", json=_intent_body(), headers=_auth())
        client.post(
            "/api/v1/plans",
            json=_plan_body(_base_commit(repository)),
            headers=_auth(),
        )
        client.post("/api/v1/runs", json=_run_body(), headers=_auth())
        before = runtime.list_events(after_cursor=0, limit=20)
        response = client.request(
            "QUERY",
            "/api/v1/runs",
            content=msgspec.json.encode(query),
            headers=headers,
        )
        conditional = client.request(
            "QUERY",
            "/api/v1/runs",
            content=msgspec.json.encode(query),
            headers={**headers, "if-none-match": response.headers["etag"]},
        )
        head = client.head("/api/v1/runs")
        options = client.options("/api/v1/runs")
        after = runtime.list_events(after_cursor=0, limit=20)

    decoded = msgspec.json.decode(response.content, type=RunQueryResponse)
    assert response.status_code == 200
    assert response.headers["content-type"].split(";", 1)[0] == RUN_QUERY_RESULT_MEDIA_TYPE
    assert response.headers["accept-query"] == RUN_QUERY_MEDIA_TYPE
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
    assert options.headers["accept-query"] == RUN_QUERY_MEDIA_TYPE
    assert after == before


def test_run_query_rejects_boundary_failures_before_service(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    service = _HttpPort(RuntimeService(EventStore(tmp_path / "state.sqlite3"), repository))
    valid = msgspec.json.encode(RunQueryRequest(schema_version="run-query-request/v1"))
    vendor_headers = {
        **_auth(),
        "accept": RUN_QUERY_RESULT_MEDIA_TYPE,
        "content-type": RUN_QUERY_MEDIA_TYPE,
    }

    with _client(service) as client:
        unauthenticated = client.request(
            "QUERY",
            "/api/v1/runs",
            content=valid,
            headers={key: value for key, value in vendor_headers.items() if key != "authorization"},
        )
        missing_content_type = client.request(
            "QUERY",
            "/api/v1/runs",
            content=valid,
            headers={**_auth(), "accept": RUN_QUERY_RESULT_MEDIA_TYPE},
        )
        unsupported = client.request(
            "QUERY",
            "/api/v1/runs",
            content=valid,
            headers={**vendor_headers, "content-type": "application/json"},
        )
        unacceptable = client.request(
            "QUERY",
            "/api/v1/runs",
            content=valid,
            headers={**vendor_headers, "accept": "text/plain"},
        )
        malformed = client.request(
            "QUERY",
            "/api/v1/runs",
            content=b'{"limit": 1, "limit": 2}',
            headers=vendor_headers,
        )
        unprocessable = client.request(
            "QUERY",
            "/api/v1/runs",
            content=b'{"schema_version":"run-query-request/v1","limit":0}',
            headers=vendor_headers,
        )
        wrong_method = client.patch("/api/v1/runs", headers=_auth())

    assert unauthenticated.status_code == 401
    assert missing_content_type.status_code == 400
    assert unsupported.status_code == 415
    assert unsupported.headers["accept-query"] == RUN_QUERY_MEDIA_TYPE
    assert unacceptable.status_code == 406
    assert malformed.status_code == 400
    assert unprocessable.status_code == 422
    assert wrong_method.status_code == 405
    assert service.run_queries == 0


class _HttpPort:
    def __init__(self, service: RuntimeService) -> None:
        self.service = service
        self.principal_ids: list[str] = []
        self.run_submissions = 0
        self.cancellations = 0
        self.run_queries = 0

    def readiness(self):
        return self.service.readiness()

    def register_project(
        self,
        request: ProjectRequest,
        *,
        principal_id: str,
    ) -> ProjectResponse:
        self.principal_ids.append(principal_id)
        return self.service.register_project(request, principal_id=principal_id)

    def accept_intent(
        self,
        request: IntentRequest,
        *,
        principal_id: str,
    ) -> IntentResponse:
        self.principal_ids.append(principal_id)
        return self.service.accept_intent(request, principal_id=principal_id)

    def accept_plan(
        self,
        request: PlanRequest,
        *,
        principal_id: str,
    ) -> PlanResponse:
        self.principal_ids.append(principal_id)
        return self.service.accept_plan(request, principal_id=principal_id)

    def submit_run(
        self,
        request: RunRequest,
        *,
        principal_id: str,
    ) -> RunResponse:
        self.run_submissions += 1
        self.principal_ids.append(principal_id)
        return self.service.submit_run(request, principal_id=principal_id)

    def inspect_run(self, run_id: str) -> RunResponse:
        return self.service.inspect_run(run_id)

    def query_runs(self, request: RunQueryRequest) -> RunQueryResponse:
        self.run_queries += 1
        return self.service.query_runs(request)

    def presentation_run_window(self, *, limit: int) -> RunSurfaceWindow:
        return self.service.presentation_run_window(limit=limit)

    def presentation_run_item(self, run_id: str) -> RunQueryItem:
        return self.service.presentation_run_item(run_id)

    def presentation_run_snapshot(self, run_id: str) -> RunSurfaceSnapshot:
        return self.service.presentation_run_snapshot(run_id)

    def cancel_run(
        self,
        run_id: str,
        request: CancelRunRequest,
        *,
        principal_id: str,
    ) -> RunResponse:
        self.cancellations += 1
        self.principal_ids.append(principal_id)
        return self.service.cancel_run(run_id, request, principal_id=principal_id)

    def list_events(
        self,
        *,
        after_cursor: int,
        limit: int,
    ) -> RuntimeEventPageResponse:
        return self.service.list_events(after_cursor=after_cursor, limit=limit)

    def replay_run(self, run_id: str) -> ReplayResponse:
        return self.service.replay_run(run_id)


def _client(service: _HttpPort) -> TestClient[Any]:
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
        "schema_version": "project-request/v1",
        "project_id": "project-1",
        "root": str(repository.resolve()),
        "configuration_provider": "kernform",
        "configuration_version": "0.2.0",
        "configuration_digest": _DIGEST,
        "idempotency_key": "project-1",
    }


def _intent_body() -> dict[str, object]:
    return {
        "schema_version": "intent-request/v1",
        "intent_id": "intent-1",
        "project_id": "project-1",
        "objective": "Implement the execution contracts.",
        "constraints": ["Do not invoke an undeclared execution path."],
        "assumptions": ["The event ledger is reusable."],
        "unresolved_questions": ["Which execution provider is selected?"],
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
        "schema_version": "plan-request/v1",
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
                        "argv": ["pytest", "tests/unit/test_runtime.py", "-q"],
                        "expected_exit_code": 0,
                    }
                ],
            },
        ],
        "idempotency_key": "plan-1",
    }


def _run_body() -> dict[str, object]:
    return {
        "schema_version": "run-request/v1",
        "run_id": "run-1",
        "project_id": "project-1",
        "intent_id": "intent-1",
        "plan_id": "plan-1",
        "idempotency_key": "run-1",
    }


def _cancel_body() -> dict[str, object]:
    return {
        "schema_version": "execution-cancel-run-request/v1",
        "idempotency_key": "cancel-run-1",
    }
