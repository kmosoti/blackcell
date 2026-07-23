from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
from litestar.testing import TestClient

from blackcell.adapters.execution.bubblewrap import (
    BubblewrapAcceptanceRunner,
    BubblewrapExecutable,
    BubblewrapIsolationPolicy,
)
from blackcell.adapters.execution.text_changes import TextChangeExecutor
from blackcell.adapters.execution.worktree import GitWorktreeLifecycle
from blackcell.adapters.runtime_http import (
    RuntimeHttpClient,
    RuntimeHttpResponse,
)
from blackcell.bootstrap.alpha_review_runtime import AlphaReviewRuntimeService
from blackcell.bootstrap.alpha_review_worker import (
    AlphaReviewWorker,
    AlphaReviewWorkerPolicy,
)
from blackcell.bootstrap.alpha_runtime import AlphaRuntimeApiService
from blackcell.bootstrap.alpha_verify_runtime import AlphaVerificationRuntimeService
from blackcell.bootstrap.alpha_verify_source import AlphaVerificationSourceService
from blackcell.bootstrap.alpha_verify_worker import (
    AlphaVerificationWorker,
    AlphaVerificationWorkerPolicy,
    DeterministicAlphaVerifier,
)
from blackcell.bootstrap.alpha_worker import (
    AlphaRuntimeWorker,
    AlphaWorkerPolicy,
)
from blackcell.bootstrap.runtime_api import RuntimeApiService
from blackcell.cli.app import app
from blackcell.config import API_TOKEN_ENV, API_TOKEN_FILE_ENV, SecretValue
from blackcell.gateway import GatewayBudget
from blackcell.interfaces import (
    BearerAuthenticator,
    ScopeAuthorizer,
    ServicePrincipal,
    ServiceScope,
)
from blackcell.interfaces.http import (
    AlphaAcceptanceCheck,
    AlphaEventPageResponse,
    AlphaIntentRequest,
    AlphaNodeBudget,
    AlphaPlanNode,
    AlphaPlanRequest,
    AlphaProjectRequest,
    AlphaRunRequest,
    StrictStruct,
    create_http_app,
    decode_contract,
    encode_contract,
)
from blackcell.kernel import ArtifactStore, EventStore
from blackcell.kernel._json import json_digest
from blackcell.orchestration.alpha_changes import (
    AlphaChangeProposal,
    AlphaChangeProviderCall,
    AlphaChangeProviderResult,
    AlphaFileChange,
    AlphaTextOperation,
    alpha_change_proposal_payload,
)
from blackcell.orchestration.alpha_review import (
    AlphaReviewProposal,
    AlphaReviewProviderCall,
    AlphaReviewProviderResult,
    alpha_review_proposal_payload,
)
from tests.cli_runner import CycloptsCliRunner

_TOKEN = "Alpha-a08-proof-token.0123456789-ABCDEFG"
_CONFIGURATION_DIGEST = "sha256:" + ("a" * 64)
_NOW = datetime(2026, 7, 22, 21, tzinfo=UTC)
_ENDPOINT = "http://127.0.0.1:8080"


class _RecordedChangeProvider:
    def __init__(self) -> None:
        self.calls: list[AlphaChangeProviderCall] = []

    def propose(self, call: AlphaChangeProviderCall) -> AlphaChangeProviderResult:
        self.calls.append(call)
        source = next(item for item in call.context.files if item.path == "src/value.py")
        proposal = AlphaChangeProposal(
            proposal_id=f"proposal-{call.node_id}",
            evidence_digest=call.context.digest,
            operations=(
                AlphaFileChange(
                    AlphaTextOperation.REPLACE,
                    "src/value.py",
                    source.content_digest,
                    "VALUE = 2\n",
                ),
            ),
            summary="Correct the bounded project value.",
        )
        return AlphaChangeProviderResult(
            proposal=proposal,
            provider_output_digest=json_digest(alpha_change_proposal_payload(proposal)),
            profile_id="alpha-code",
            adapter_id="recorded-a08-proof",
            model_id="deterministic-test-model",
            input_tokens=100,
            output_tokens=20,
            latency_ms=10,
            cost_microusd=1,
            completed_at=_NOW,
        )


class _ClearReviewer:
    def review(self, call: AlphaReviewProviderCall) -> AlphaReviewProviderResult:
        proposal = AlphaReviewProposal(
            context_digest=call.context.digest,
            findings=(),
            summary="No source-bound findings.",
        )
        return AlphaReviewProviderResult(
            proposal=proposal,
            provider_output_digest=json_digest(alpha_review_proposal_payload(proposal)),
            profile_id="alpha-review",
            adapter_id="recorded-a08-reviewer",
            model_id="deterministic-review-model",
            input_tokens=200,
            output_tokens=20,
            latency_ms=10,
            cost_microusd=1,
            completed_at=_NOW,
        )


class _LegacyOperatorTrap:
    def __init__(self, repository: Path, database_path: Path) -> None:
        self.repo_root = repository
        self.database_path = database_path
        self.run_calls = 0

    def run(self, **_: object) -> None:
        self.run_calls += 1
        raise AssertionError("legacy RepositoryOperator.run must not be called")


class _TestClientTransport:
    def __init__(self, client: TestClient[Any]) -> None:
        self.client = client
        self.requests: list[tuple[str, str]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> RuntimeHttpResponse:
        del timeout_seconds
        parsed = urlsplit(url)
        assert parsed.scheme == "http"
        assert parsed.hostname == "127.0.0.1"
        assert parsed.port == 8080
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        self.requests.append((method, target))
        response = self.client.request(
            method,
            target,
            headers=dict(headers),
            content=body,
        )
        return RuntimeHttpResponse(
            status_code=response.status_code,
            content_type=response.headers.get("content-type", ""),
            body=response.content,
        )


def test_real_project_completes_through_cli_daemon_workers_restart_and_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, base_commit = _repository(tmp_path)
    data_root = tmp_path / "data"
    database_path = data_root / "state.sqlite3"
    artifact_root = data_root / "artifacts"
    isolation_root = (tmp_path / "worktrees").resolve()
    events = EventStore(database_path)
    artifacts = ArtifactStore(
        artifact_root,
        database_path=database_path,
        max_total_bytes=16 * 1024 * 1024,
    )
    legacy = _LegacyOperatorTrap(repository, database_path)
    api_service = RuntimeApiService(
        cast(Any, legacy),
        cast(Any, object()),
        events=events,
        artifacts=artifacts,
        alpha_isolation_root=isolation_root,
    )
    principal = ServicePrincipal(
        "alpha-proof:operator",
        (ServiceScope.READ, ServiceScope.RUN),
    )
    http_app = create_http_app(
        api_service,
        authenticator=BearerAuthenticator(SecretValue(_TOKEN), principal),
        authorizer=ScopeAuthorizer(),
        web_poll_seconds=0.05,
    )
    requests = _requests(repository, base_commit)
    request_files = {
        name: _request_file(tmp_path / f"{name}.json", request)
        for name, request in requests.items()
    }

    with TestClient(http_app) as client:
        transport = _TestClientTransport(client)

        def client_factory(*, endpoint: str, token: SecretValue) -> RuntimeHttpClient:
            return RuntimeHttpClient(endpoint=endpoint, token=token, transport=transport)

        monkeypatch.setattr("blackcell.cli.app.RuntimeHttpClient", client_factory)
        monkeypatch.setenv(API_TOKEN_ENV, _TOKEN)
        monkeypatch.delenv(API_TOKEN_FILE_ENV, raising=False)
        cli = CycloptsCliRunner()
        submitted = {
            operation: _invoke_cli(cli, command)
            for operation, command in (
                (
                    "project",
                    (
                        "alpha",
                        "project",
                        "register",
                        "--request",
                        request_files["project"],
                    ),
                ),
                (
                    "intent",
                    (
                        "alpha",
                        "intent",
                        "accept",
                        "--request",
                        request_files["intent"],
                    ),
                ),
                (
                    "plan",
                    (
                        "alpha",
                        "plan",
                        "accept",
                        "--request",
                        request_files["plan"],
                    ),
                ),
                (
                    "run",
                    (
                        "alpha",
                        "run",
                        "submit",
                        "--request",
                        request_files["run"],
                    ),
                ),
            )
        }
        assert submitted["project"]["project_id"] == "project-a08"
        assert submitted["intent"]["intent_id"] == "intent-a08"
        assert submitted["plan"]["topological_order"] == ["write", "verify"]
        assert submitted["run"]["status"] == "queued"
        assert legacy.run_calls == 0

        execution_events = EventStore(database_path)
        execution_artifacts = ArtifactStore(
            artifact_root,
            database_path=database_path,
            max_total_bytes=16 * 1024 * 1024,
        )
        worktrees = GitWorktreeLifecycle()
        execution = AlphaRuntimeApiService(
            execution_events,
            repository,
            isolation_root=isolation_root,
            worktrees=worktrees,
            artifacts=execution_artifacts,
        )
        provider = _RecordedChangeProvider()
        worker = AlphaRuntimeWorker(
            runtime=execution,
            artifacts=execution_artifacts,
            provider=provider,
            change_executor=TextChangeExecutor(worktrees),
            acceptance=_isolated_acceptance(worktrees),
            worktrees=worktrees,
            policy=AlphaWorkerPolicy("alpha-proof-executor"),
        )

        writer = worker.run_once()
        verifier_node = worker.run_once()

        assert writer.status == "node-succeeded"
        assert writer.run_status == "queued"
        assert verifier_node.status == "node-succeeded"
        assert verifier_node.run_status == "succeeded"
        assert worker.run_once().status == "idle"
        assert len(provider.calls) == 1
        assert (repository / "src" / "value.py").read_text(encoding="utf-8") == "VALUE = 1\n"
        provider_evidence = _provider_evidence(execution_artifacts, writer.outcome_artifact_digest)
        assert provider_evidence == {
            "adapter_id": "recorded-a08-proof",
            "cost_microusd": 1,
            "input_tokens": 100,
            "latency_ms": 10,
            "output_tokens": 20,
        }

        review = AlphaReviewWorker(
            execution=execution,
            scheduler=AlphaReviewRuntimeService(EventStore(database_path)),
            artifacts=execution_artifacts,
            reviewer=_ClearReviewer(),
            policy=AlphaReviewWorkerPolicy(
                worker_id="alpha-proof-reviewer",
                budget=GatewayBudget(20_000, 2_000, 30_000, 10_000),
            ),
            clock=lambda: _NOW,
        ).run_once()
        assert review.status == "review-succeeded"
        assert review.finding_count == 0

        verification = AlphaVerificationWorker(
            source=AlphaVerificationSourceService(
                EventStore(database_path),
                execution,
                execution_artifacts,
            ),
            scheduler=AlphaVerificationRuntimeService(EventStore(database_path)),
            artifacts=execution_artifacts,
            verifier=DeterministicAlphaVerifier(),
            policy=AlphaVerificationWorkerPolicy("alpha-proof-verifier"),
            clock=lambda: _NOW,
        ).run_once()
        assert verification.status == "verification-completed"
        assert verification.verdict is not None
        assert verification.verdict.value == "pass"

        reopened_artifacts = ArtifactStore(
            artifact_root,
            database_path=database_path,
            max_total_bytes=16 * 1024 * 1024,
        )
        reopened = AlphaRuntimeApiService(
            EventStore(database_path),
            repository,
            isolation_root=isolation_root,
            artifacts=reopened_artifacts,
        )
        replay = reopened.replay_run("run-a08")
        assert replay.run.status == "succeeded"
        assert replay.processed_events == 12
        assert replay.artifact_integrity == "verified"
        assert replay.findings == ()
        assert replay.verification.lifecycle_status == "completed"
        assert replay.verification.verdict == "pass"
        assert replay.verification.artifact_integrity == "verified"
        assert len(provider.calls) == 1

        cli_status = _invoke_cli(cli, ("alpha", "run", "status", "run-a08"))
        cli_replay = _invoke_cli(cli, ("alpha", "run", "replay", "run-a08"))
        assert cli_status["status"] == "succeeded"
        assert cli_replay["state_digest"] == replay.state_digest
        assert cli_replay["verification"]["verdict"] == "pass"

        shell = client.get("/alpha")
        browser_status = client.get(
            "/api/alpha/v1/runs/run-a08/status",
            headers=_auth(),
        )
        browser_replay = client.get(
            "/api/alpha/v1/runs/run-a08/replay",
            headers=_auth(),
        )
        assert shell.status_code == 200
        assert b"BlackCell Alpha" in shell.content
        assert browser_status.json()["status"] == "succeeded"
        assert browser_replay.json()["state_digest"] == replay.state_digest

        ticket_response = client.post(
            "/api/alpha/v1/ui/socket-tickets",
            headers=_auth(),
        )
        assert ticket_response.status_code == 201
        ticket = ticket_response.json()["ticket"]
        after_cursor = cast(int, submitted["run"]["cursor"])
        with client.websocket_connect(
            f"/api/alpha/v1/ui/events?ticket={ticket}&after={after_cursor}"
        ) as socket:
            page = decode_contract(socket.receive_bytes(), AlphaEventPageResponse)
        assert page.after_cursor == after_cursor
        assert tuple(event.cursor for event in page.events) == tuple(range(5, 18))
        assert tuple(event.event_type for event in page.events) == (
            "alpha.node.claimed",
            "alpha.node.worktree-prepared",
            "alpha.node.provider-dispatch-started",
            "alpha.node.succeeded",
            "alpha.node.claimed",
            "alpha.node.worktree-prepared",
            "alpha.node.succeeded",
            "alpha.run.succeeded",
            "alpha.review.claimed",
            "alpha.review.provider-dispatch-started",
            "alpha.review.succeeded",
            "alpha.verification.claimed",
            "alpha.verification.completed",
        )
        assert page.next_cursor == 17
        assert page.has_more is False
        assert transport.requests == [
            ("POST", "/api/alpha/v1/projects"),
            ("POST", "/api/alpha/v1/intents"),
            ("POST", "/api/alpha/v1/plans"),
            ("POST", "/api/alpha/v1/runs"),
            ("GET", "/api/alpha/v1/runs/run-a08/status"),
            ("GET", "/api/alpha/v1/runs/run-a08/replay"),
        ]


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "project"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "BlackCell Alpha Proof")
    _git(repository, "config", "user.email", "blackcell@example.invalid")
    (repository / "src").mkdir()
    (repository / "src" / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "README.md").write_text(
        "# Alpha proof project\n\nThe accepted change corrects VALUE from 1 to 2.\n",
        encoding="utf-8",
    )
    _git(repository, "add", "README.md", "src/value.py")
    _git(repository, "commit", "-m", "initial alpha proof project")
    return repository.resolve(), _git_text(repository, "rev-parse", "HEAD")


def _requests(repository: Path, base_commit: str) -> dict[str, StrictStruct]:
    project = AlphaProjectRequest(
        schema_version="alpha-project-request/v1",
        project_id="project-a08",
        root=str(repository),
        configuration_provider="kernform",
        configuration_version="0.1.0",
        configuration_digest=_CONFIGURATION_DIGEST,
        idempotency_key="project-a08",
    )
    intent = AlphaIntentRequest(
        schema_version="alpha-intent-request/v1",
        intent_id="intent-a08",
        project_id="project-a08",
        objective="Correct and independently verify the bounded project value.",
        constraints=("Only change src/value.py.", "Do not invoke the legacy V2 runtime."),
        assumptions=("Python 3 is admitted by the isolation policy.",),
        unresolved_questions=(),
        idempotency_key="intent-a08",
    )
    writer_budget = AlphaNodeBudget(1_000, 1_000, 30, 1_000, 1)
    verifier_budget = AlphaNodeBudget(0, 0, 30, 0, 0)
    assertion = ("python", "-c", "from src.value import VALUE; assert VALUE == 2")
    writer = AlphaPlanNode(
        node_id="write",
        objective="Correct the bounded value.",
        depends_on=(),
        budget=writer_budget,
        effects=("repository-read", "repository-write", "process"),
        allowed_paths=("src/value.py",),
        checks=(AlphaAcceptanceCheck("writer-check", assertion),),
    )
    verifier = AlphaPlanNode(
        node_id="verify",
        objective="Verify the committed value independently of the writer.",
        depends_on=("write",),
        budget=verifier_budget,
        effects=("repository-read", "process"),
        allowed_paths=(),
        checks=(AlphaAcceptanceCheck("verifier-check", assertion),),
    )
    plan = AlphaPlanRequest(
        schema_version="alpha-plan-request/v1",
        plan_id="plan-a08",
        project_id="project-a08",
        intent_id="intent-a08",
        base_commit=base_commit,
        allowed_effects=("repository-read", "repository-write", "process"),
        nodes=(writer, verifier),
        idempotency_key="plan-a08",
    )
    run = AlphaRunRequest(
        schema_version="alpha-run-request/v1",
        run_id="run-a08",
        project_id="project-a08",
        intent_id="intent-a08",
        plan_id="plan-a08",
        idempotency_key="run-a08",
    )
    return {"project": project, "intent": intent, "plan": plan, "run": run}


def _request_file(path: Path, request: StrictStruct) -> str:
    path.write_bytes(encode_contract(request))
    return str(path)


def _invoke_cli(
    runner: CycloptsCliRunner,
    arguments: tuple[str, ...],
) -> dict[str, Any]:
    result = runner.invoke(
        app,
        [*arguments, "--endpoint", _ENDPOINT],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.stderr
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return cast("dict[str, Any]", value)


def _isolated_acceptance(worktrees: GitWorktreeLifecycle) -> BubblewrapAcceptanceRunner:
    return BubblewrapAcceptanceRunner(
        BubblewrapIsolationPolicy(
            (BubblewrapExecutable("python", Path("/usr/bin/python3")),),
        ),
        worktrees,
        bubblewrap_executable=Path("/usr/bin/bwrap"),
        prlimit_executable=Path("/usr/bin/prlimit"),
        probe_executable=Path("/usr/bin/true"),
    )


def _provider_evidence(
    artifacts: ArtifactStore,
    outcome_digest: str | None,
) -> dict[str, object]:
    assert outcome_digest is not None
    outcome = artifacts.get_json(outcome_digest)
    assert isinstance(outcome, dict)
    outcome_mapping = cast("dict[str, object]", outcome)
    provider_link = outcome_mapping["provider_artifact"]
    assert isinstance(provider_link, dict)
    provider_link_mapping = cast("dict[str, object]", provider_link)
    provider_digest = provider_link_mapping["digest"]
    assert isinstance(provider_digest, str)
    provider = artifacts.get_json(provider_digest)
    assert isinstance(provider, dict)
    provider_mapping = cast("dict[str, object]", provider)
    return {
        "adapter_id": provider_mapping["adapter_id"],
        "cost_microusd": provider_mapping["cost_microusd"],
        "input_tokens": provider_mapping["input_tokens"],
        "latency_ms": provider_mapping["latency_ms"],
        "output_tokens": provider_mapping["output_tokens"],
    }


def _auth() -> dict[str, str]:
    return {"authorization": f"Bearer {_TOKEN}"}


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        env=_git_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
    )


def _git_text(cwd: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        env=_git_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def _git_environment() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
