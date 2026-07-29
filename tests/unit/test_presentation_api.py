from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import msgspec
import pytest
from litestar.testing import TestClient

from blackcell.adapters.models import tooling_surface_catalog
from blackcell.bootstrap.runtime_service import RuntimeService
from blackcell.config import SecretValue
from blackcell.interfaces import (
    BearerAuthenticator,
    ScopeAuthorizer,
    ServicePrincipal,
    ServiceScope,
)
from blackcell.interfaces.http import (
    PRESENTATION_MEDIA_TYPE,
    RunQueryItem,
    RunQueryRequest,
    RunSurfaceWindow,
    RuntimeApiError,
    RuntimeApiFailureCode,
    RuntimeArtifactPayload,
    create_http_app,
)
from blackcell.interfaces.presentation import (
    FormComponent,
    PlanGraphComponent,
    PresentationSurface,
    TableComponent,
)
from blackcell.kernel import EventStore
from tests.unit.test_runtime_http_api import (
    _TOKEN,
    _auth,
    _intent_body,
    _plan_body,
    _project_body,
    _run_body,
)
from tests.unit.test_runtime_service import _base_commit, _repository


def test_presentation_routes_are_authenticated_deterministic_and_conditional(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    runtime = RuntimeService(EventStore(tmp_path / "state.sqlite3"), repository)
    before = runtime.list_events(after_cursor=0, limit=20)

    with _client(cast(Any, runtime)) as client:
        client.post("/api/v1/projects", json=_project_body(repository), headers=_auth())
        client.post("/api/v1/intents", json=_intent_body(), headers=_auth())
        client.post(
            "/api/v1/plans",
            json={**_plan_body(_base_commit(repository)), "planning_mode": "declared"},
            headers=_auth(),
        )
        client.post("/api/v1/runs", json=_run_body(), headers=_auth())
        unauthenticated = client.get("/api/v1/ui/surfaces/workspace")
        workspace = client.get("/api/v1/ui/surfaces/workspace", headers=_auth())
        conditional = client.get(
            "/api/v1/ui/surfaces/workspace",
            headers={**_auth(), "if-none-match": workspace.headers["etag"]},
        )
        run = client.get("/api/v1/ui/surfaces/runs/run-1", headers=_auth())

    workspace_surface = PresentationSurface.model_validate_json(workspace.content)
    run_surface = PresentationSurface.model_validate_json(run.content)
    assert unauthenticated.status_code == 401
    assert workspace.status_code == run.status_code == 200
    assert workspace.headers["content-type"].split(";", 1)[0] == PRESENTATION_MEDIA_TYPE
    assert workspace.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert conditional.status_code == 304
    assert conditional.content == b""
    assert workspace_surface.surface_id == "workspace"
    assert run_surface.surface_id == "run:run-1"
    assert any(
        isinstance(component, FormComponent)
        and component.action.operation == "accept-plan"
        and any(field.json_pointer == "/planning_mode" for field in component.action.fields)
        for component in workspace_surface.components
    )
    assert any(isinstance(component, PlanGraphComponent) for component in run_surface.components)
    assert any(
        isinstance(component, TableComponent) and component.component_id == "plan-table"
        for component in run_surface.components
    )
    after = runtime.list_events(after_cursor=0, limit=20)
    assert after.events[: len(before.events)] == before.events
    assert [event.event_type for event in after.events[-4:]] == [
        "project.registered",
        "intent.accepted",
        "plan.accepted",
        "run.queued",
    ]


def test_run_artifact_route_applies_binding_and_safe_response_headers() -> None:
    content = b'{"result":"ok"}'
    digest = "sha256:" + "a" * 64
    service = _ArtifactPort(
        RuntimeArtifactPayload(
            digest=digest,
            size_bytes=len(content),
            media_type="application/json",
            encoding="utf-8",
            content=content,
        )
    )

    with _client(cast(Any, service)) as client:
        unauthenticated = client.get(f"/api/v1/runs/run-1/artifacts/{digest}")
        response = client.get(f"/api/v1/runs/run-1/artifacts/{digest}", headers=_auth())
        conditional = client.get(
            f"/api/v1/runs/run-1/artifacts/{digest}",
            headers={**_auth(), "if-none-match": response.headers["etag"]},
        )
        missing = client.get(
            f"/api/v1/runs/run-2/artifacts/{digest}",
            headers=_auth(),
        )

    assert unauthenticated.status_code == 401
    assert service.calls == [("run-1", digest), ("run-1", digest), ("run-2", digest)]
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"].split(";", 1)[0] == "application/json"
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert conditional.status_code == 304
    assert conditional.content == b""
    assert missing.status_code == 404
    assert missing.json() == {"error": "not-found", "schema_version": "error/v1"}


def test_runtime_service_refuses_artifacts_not_bound_by_replay(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    runtime = RuntimeService(EventStore(tmp_path / "state.sqlite3"), repository)

    with pytest.raises(RuntimeApiError) as missing:
        runtime.read_run_artifact("missing-run", "sha256:" + "a" * 64)
    assert missing.value.code is RuntimeApiFailureCode.NOT_FOUND


def test_workspace_surface_projects_the_bounded_latest_run_window(
    tmp_path: Path,
) -> None:
    runtime = _queued_runtime(tmp_path)
    seed = runtime.query_runs(RunQueryRequest(schema_version="run-query-request/v1", limit=1)).runs[
        0
    ]
    runs = tuple(_run_item(seed, index) for index in range(53))
    service = _PresentationPort(
        window=RunSurfaceWindow(
            limit=50,
            scanned_events=53,
            runs=runs[-50:],
            event_cursor=1_080,
            has_older_runs=True,
        )
    )

    with _client(cast(Any, service)) as client:
        response = client.get("/api/v1/ui/surfaces/workspace", headers=_auth())

    surface = PresentationSurface.model_validate_json(response.content)
    run_table = next(
        component
        for component in surface.components
        if isinstance(component, TableComponent) and component.component_id == "run-table"
    )
    assert response.status_code == 200
    assert [row.cells["run"] for row in run_table.rows] == [
        f"run-{index:03d}" for index in range(3, 53)
    ]
    assert surface.revision.event_cursor == 1_080
    assert service.window_limits == [50]


def test_run_surface_loads_the_selected_projection_directly(
    tmp_path: Path,
) -> None:
    runtime = _queued_runtime(tmp_path)
    seed = runtime.query_runs(RunQueryRequest(schema_version="run-query-request/v1", limit=1)).runs[
        0
    ]
    selected = msgspec.structs.replace(
        seed,
        nodes=(msgspec.structs.replace(seed.nodes[0], status="running"), *seed.nodes[1:]),
    )
    service = _PresentationPort(
        item=selected,
        replay=runtime.replay_run("run-1"),
    )

    with _client(cast(Any, service)) as client:
        response = client.get("/api/v1/ui/surfaces/runs/run-1", headers=_auth())

    surface = PresentationSurface.model_validate_json(response.content)
    graph = next(
        component for component in surface.components if isinstance(component, PlanGraphComponent)
    )
    assert response.status_code == 200
    assert graph.nodes[0].status == "running"
    assert service.item_run_ids == ["run-1"]


class _ArtifactPort:
    def __init__(self, payload: RuntimeArtifactPayload) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def read_run_artifact(self, run_id: str, digest: str) -> RuntimeArtifactPayload:
        self.calls.append((run_id, digest))
        if run_id != "run-1" or digest != self.payload.digest:
            raise RuntimeApiError(RuntimeApiFailureCode.NOT_FOUND)
        return self.payload


class _PresentationPort:
    def __init__(
        self,
        *,
        window: RunSurfaceWindow | None = None,
        item: RunQueryItem | None = None,
        replay: object | None = None,
    ) -> None:
        self.window = window
        self.item = item
        self.replay = replay
        self.window_limits: list[int] = []
        self.item_run_ids: list[str] = []

    def presentation_run_window(self, *, limit: int) -> RunSurfaceWindow:
        self.window_limits.append(limit)
        if self.window is None:
            raise RuntimeApiError(RuntimeApiFailureCode.NOT_FOUND)
        return self.window

    def presentation_run_item(self, run_id: str) -> RunQueryItem:
        self.item_run_ids.append(run_id)
        if self.item is None or self.item.run.run_id != run_id:
            raise RuntimeApiError(RuntimeApiFailureCode.NOT_FOUND)
        return self.item

    def replay_run(self, run_id: str) -> object:
        if run_id != "run-1" or self.replay is None:
            raise RuntimeApiError(RuntimeApiFailureCode.NOT_FOUND)
        return self.replay


def _queued_runtime(tmp_path: Path) -> RuntimeService:
    repository = _repository(tmp_path)
    runtime = RuntimeService(EventStore(tmp_path / "state.sqlite3"), repository)
    with _client(cast(Any, runtime)) as client:
        assert (
            client.post(
                "/api/v1/projects", json=_project_body(repository), headers=_auth()
            ).status_code
            == 201
        )
        assert (
            client.post("/api/v1/intents", json=_intent_body(), headers=_auth()).status_code == 201
        )
        assert (
            client.post(
                "/api/v1/plans",
                json={**_plan_body(_base_commit(repository)), "planning_mode": "declared"},
                headers=_auth(),
            ).status_code
            == 201
        )
        assert client.post("/api/v1/runs", json=_run_body(), headers=_auth()).status_code == 202
    return runtime


def _run_item(seed: RunQueryItem, index: int) -> RunQueryItem:
    return msgspec.structs.replace(
        seed,
        queued_cursor=index + 1,
        run=msgspec.structs.replace(
            seed.run,
            run_id=f"run-{index:03d}",
            cursor=index + 1,
        ),
    )


def _client(service: Any) -> TestClient[Any]:
    principal = ServicePrincipal(
        "client:test",
        (ServiceScope.READ, ServiceScope.RUN),
    )
    return TestClient(
        create_http_app(
            service,
            authenticator=BearerAuthenticator(SecretValue(_TOKEN), principal),
            authorizer=ScopeAuthorizer(),
            tooling_catalog=tooling_surface_catalog(),
        )
    )
