from __future__ import annotations

import stat
import subprocess
from pathlib import Path

from litestar.testing import TestClient

from blackcell.adapters.persistence.sqlite import SQLiteOrchestrationScheduler
from blackcell.bootstrap import RuntimeApiService, build_runtime_http_app
from blackcell.config import API_TOKEN_ENV, DATA_DIR_ENV, RuntimeSecurityConfig
from blackcell.interfaces.http import create_http_app
from blackcell.orchestration import (
    DagDefinition,
    DagNode,
    NodeBudget,
    NodeSideEffect,
    OrchestrationRole,
    RetryPolicy,
)

TOKEN = "Runtime-v1_integration-token.0123456789-ABCDEFG"


def test_http_composition_shares_operator_use_cases_and_replays_live_free(
    tmp_path: Path,
) -> None:
    repository = _git_repository(tmp_path / "repository")
    config = _config(tmp_path / "runtime-data")
    app = build_runtime_http_app(config, repository_root=repository)

    with TestClient(app) as client:
        submitted = client.post(
            "/api/v1/runs",
            headers=_auth(),
            json={
                "schema_version": "run-submission-request/v1",
                "objective": "Inspect repository readiness.",
                "approval_granted": False,
                "token_budget": 2_000,
                "character_budget": 8_000,
            },
        )
        assert submitted.status_code == 201, submitted.text
        run_id = submitted.json()["run_id"]

        context = client.get(f"/api/v1/runs/{run_id}/context", headers=_auth())
        evaluation = client.get(f"/api/v1/runs/{run_id}/evaluation", headers=_auth())
        events = client.get("/api/v1/events?after=0&limit=200", headers=_auth())

        repository.rename(tmp_path / "repository-offline")
        inspected = client.get(f"/api/v1/runs/{run_id}", headers=_auth())
        replayed = client.get(f"/api/v1/runs/{run_id}/replay", headers=_auth())

    assert stat.S_IMODE(config.paths.database_path.stat().st_mode) == 0o600
    assert context.status_code == 200
    assert context.json()["run_id"] == run_id
    assert evaluation.status_code == 200
    assert evaluation.json()["verdict"] in {"pass", "fail", "inconclusive"}
    assert events.status_code == 200
    assert events.json()["events"]
    assert events.json()["events"][0]["payload_hash"].startswith("sha256:")
    assert inspected.status_code == 200
    assert inspected.json()["run_id"] == run_id
    assert replayed.status_code == 200
    assert replayed.json()["classification"] == inspected.json()["status"]


def test_http_observation_ingest_and_scheduler_approval_use_authenticated_identity(
    tmp_path: Path,
) -> None:
    repository = _git_repository(tmp_path / "repository")
    config = _config(tmp_path / "runtime-data")
    service = RuntimeApiService.from_config(config, repository_root=repository)
    scheduler = SQLiteOrchestrationScheduler(config.paths.database_path)
    scheduler.submit(
        "orchestration-1",
        _approval_dag(),
        submitted_by="bootstrap:test",
    )
    app = create_http_app(
        service,
        authenticator=config.authenticator(),
        authorizer=config.authorizer(),
    )

    with TestClient(app) as client:
        observation = client.post(
            "/api/v1/observations",
            headers=_auth(),
            json={
                "schema_version": "observation-ingest-request/v1",
                "stream_id": "observation:integration",
                "expected_sequence": 0,
                "source": "integration/v1",
                "correlation_id": "correlation-1",
                "observations": [
                    {
                        "observation_id": "observation-1",
                        "effective_at": "2026-07-13T12:00:00Z",
                        "claims": [
                            {
                                "claim_id": "claim-1",
                                "subject": "runtime",
                                "predicate": "ready",
                                "value": True,
                            }
                        ],
                        "evidence": [{"locator": "integration://fixture"}],
                    }
                ],
            },
        )
        approval = client.post(
            "/api/v1/orchestration/runs/orchestration-1/nodes/execute/approvals",
            headers=_auth(),
            json={
                "schema_version": "orchestration-approval-request/v1",
                "role": "reviewer",
                "approved": True,
            },
        )
        inspected = client.get(
            "/api/v1/orchestration/runs/orchestration-1",
            headers=_auth(),
        )

    assert observation.status_code == 201
    assert observation.json()["first_sequence"] == 1
    assert approval.status_code == 200
    assert approval.json()["principal_id"] == "service:runtime-v1"
    assert inspected.status_code == 200
    assert inspected.json()["approvals"][0]["principal_id"] == "service:runtime-v1"


def test_storage_exhaustion_fails_readiness_and_mutations_but_preserves_reads(
    tmp_path: Path,
) -> None:
    repository = _git_repository(tmp_path / "repository")
    config = _config(tmp_path / "runtime-data")

    class ExhaustedStorage:
        def has_mutation_capacity(self) -> bool:
            return False

    service = RuntimeApiService.from_config(
        config,
        repository_root=repository,
        storage_quota=ExhaustedStorage(),
    )
    app = create_http_app(
        service,
        authenticator=config.authenticator(),
        authorizer=config.authorizer(),
    )

    with TestClient(app) as client:
        readiness = client.get("/health/ready")
        events = client.get("/api/v1/events", headers=_auth())
        submitted = client.post(
            "/api/v1/runs",
            headers=_auth(),
            json={
                "schema_version": "run-submission-request/v1",
                "objective": "This mutation must be rejected.",
                "approval_granted": False,
                "token_budget": 2_000,
                "character_budget": 8_000,
            },
        )

    assert readiness.status_code == 503
    assert events.status_code == 200
    assert submitted.status_code == 507
    assert submitted.json() == {
        "error": "storage-quota-exceeded",
        "schema_version": "error/v1",
    }


def _config(data_root: Path) -> RuntimeSecurityConfig:
    return RuntimeSecurityConfig.from_environment(
        {DATA_DIR_ENV: str(data_root), API_TOKEN_ENV: TOKEN}
    )


def _git_repository(path: Path) -> Path:
    path.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def _auth() -> dict[str, str]:
    return {"authorization": f"Bearer {TOKEN}"}


def _approval_dag() -> DagDefinition:
    return DagDefinition(
        "dag:approval-api",
        (
            DagNode(
                node_id="execute",
                role=OrchestrationRole.EXECUTOR,
                principal_id="worker:execute",
                handler="handler:execute",
                output_schema="result/v1",
                depends_on=(),
                inputs=(),
                retry=RetryPolicy(),
                timeout_seconds=30,
                budget=NodeBudget(100, 100, 10_000, 1_000),
                side_effect=NodeSideEffect.REVERSIBLE,
                required_approvals=(OrchestrationRole.REVIEWER,),
            ),
        ),
    )
