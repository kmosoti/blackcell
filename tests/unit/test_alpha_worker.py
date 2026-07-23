from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from blackcell.adapters.execution.text_changes import TextChangeExecutor
from blackcell.adapters.execution.worktree import (
    GitWorktreeLifecycle,
    WorktreeExecutionSpec,
    worktree_inspection_payload,
)
from blackcell.adapters.models.alpha_change_provider import (
    AlphaChangeProviderError,
    AlphaChangeProviderFailureCode,
)
from blackcell.bootstrap.alpha_runtime import AlphaRuntimeApiService
from blackcell.bootstrap.alpha_worker import (
    AlphaRuntimeWorker,
    AlphaRuntimeWorkerPort,
    AlphaWorkerPolicy,
)
from blackcell.interfaces.http import (
    AlphaAcceptanceCheck,
    AlphaCancelRunRequest,
    AlphaIntentRequest,
    AlphaNodeBudget,
    AlphaPlanNode,
    AlphaPlanRequest,
    AlphaProjectRequest,
    AlphaRunRequest,
    AlphaRunResponse,
)
from blackcell.kernel import ArtifactStore, EventStore
from blackcell.kernel._json import json_digest
from blackcell.orchestration.alpha_acceptance import (
    AlphaAcceptanceCommand,
    AlphaAcceptanceError,
    AlphaAcceptanceFailureCode,
    AlphaAcceptanceResult,
    AlphaAcceptanceStream,
)
from blackcell.orchestration.alpha_changes import (
    AlphaChangeProposal,
    AlphaChangeProviderCall,
    AlphaChangeProviderResult,
    AlphaFileChange,
    AlphaTextOperation,
    alpha_change_proposal_payload,
)

_CONFIGURATION_DIGEST = "sha256:" + ("a" * 64)
_ISOLATION_POLICY_DIGEST = "sha256:" + ("b" * 64)
_NOW = datetime(2026, 7, 22, 18, tzinfo=UTC)


class ReplacingProvider:
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
            summary="Update the bounded value.",
        )
        return AlphaChangeProviderResult(
            proposal=proposal,
            provider_output_digest=json_digest(alpha_change_proposal_payload(proposal)),
            profile_id="alpha-code",
            adapter_id="recorded-test",
            model_id="test-model",
            input_tokens=100,
            output_tokens=20,
            latency_ms=10,
            cost_microusd=1,
            completed_at=_NOW,
        )


class DispatchObservingProvider(ReplacingProvider):
    def __init__(self, events: EventStore) -> None:
        super().__init__()
        self.events = events
        self.dispatch_event_id: str | None = None

    def propose(self, call: AlphaChangeProviderCall) -> AlphaChangeProviderResult:
        dispatch = self.events.read_stream(f"alpha:run:{call.run_id}")[-1]
        assert dispatch.event_type == "alpha.node.provider-dispatch-started"
        assert dispatch.payload["provider_request_id"] == call.request_id
        assert dispatch.payload["context_digest"] == call.context.digest
        assert dispatch.payload["context_artifact_digest"] == call.context.digest
        assert call.causation_id == dispatch.event_id
        self.dispatch_event_id = dispatch.event_id
        return super().propose(call)


class FailingProvider:
    def propose(self, call: AlphaChangeProviderCall) -> AlphaChangeProviderResult:
        del call
        error = AlphaChangeProviderError(AlphaChangeProviderFailureCode.INVALID_GATEWAY_RESULT)
        error.add_note("sensitive-provider-detail")
        raise error


class RecordingAcceptance:
    def __init__(
        self,
        lifecycle: GitWorktreeLifecycle,
        *,
        failing_check_id: str | None = None,
    ) -> None:
        self.lifecycle = lifecycle
        self.failing_check_id = failing_check_id
        self.commands: list[AlphaAcceptanceCommand] = []
        self.specs: list[WorktreeExecutionSpec] = []

    def run(
        self,
        command: AlphaAcceptanceCommand,
        spec: WorktreeExecutionSpec,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> AlphaAcceptanceResult:
        if callable(cancel_requested):
            assert not cancel_requested()
        self.commands.append(command)
        self.specs.append(spec)
        inspection = self.lifecycle.inspect(spec)
        inspection_digest = json_digest(worktree_inspection_payload(inspection))
        passed = command.check_id != self.failing_check_id
        return_code = (
            command.expected_exit_code if passed else (command.expected_exit_code + 1) % 256
        )
        return AlphaAcceptanceResult(
            check_id=command.check_id,
            command_digest=command.digest,
            worktree_spec_digest=spec.digest,
            isolation_policy_digest=_ISOLATION_POLICY_DIGEST,
            inspection_before_digest=inspection_digest,
            inspection_after_digest=inspection_digest,
            return_code=return_code,
            expected_exit_code=command.expected_exit_code,
            passed=passed,
            stdout=AlphaAcceptanceStream(f"{command.check_id}\n".encode()),
            stderr=AlphaAcceptanceStream(b""),
        )


class CancelingAcceptance:
    def __init__(self, runtime: AlphaRuntimeApiService) -> None:
        self.runtime = runtime

    def run(
        self,
        command: AlphaAcceptanceCommand,
        spec: WorktreeExecutionSpec,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> AlphaAcceptanceResult:
        del command
        self.runtime.cancel_run(
            spec.lease.run_id,
            AlphaCancelRunRequest(
                schema_version="alpha-cancel-run-request/v1",
                idempotency_key="cancel-during-check",
            ),
            principal_id="operator",
        )
        assert cancel_requested is not None and cancel_requested()
        raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.CANCELED)


class CancelBeforeSuccessRuntime:
    """Inject the cancellation race immediately before terminal success."""

    def __init__(self, service: AlphaRuntimeApiService) -> None:
        self.service = service

    def record_node_success(
        self,
        spec: WorktreeExecutionSpec,
        *,
        result_digest: str,
        principal_id: str,
    ) -> AlphaRunResponse:
        self.service.cancel_run(
            spec.lease.run_id,
            AlphaCancelRunRequest(
                schema_version="alpha-cancel-run-request/v1",
                idempotency_key="cancel-before-success",
            ),
            principal_id="operator",
        )
        return self.service.record_node_success(
            spec,
            result_digest=result_digest,
            principal_id=principal_id,
        )

    def __getattr__(self, name: str) -> object:
        return getattr(self.service, name)


def test_worker_executes_writer_then_check_from_persisted_artifact_chain(
    tmp_path: Path,
) -> None:
    runtime, events, artifacts, repository, isolation, base_commit = _runtime(tmp_path)
    _submit(runtime, repository, base_commit)
    lifecycle = GitWorktreeLifecycle()
    provider = ReplacingProvider()
    acceptance = RecordingAcceptance(lifecycle)
    worker = AlphaRuntimeWorker(
        runtime=runtime,
        artifacts=artifacts,
        provider=provider,
        change_executor=TextChangeExecutor(lifecycle),
        acceptance=acceptance,
        worktrees=lifecycle,
        policy=AlphaWorkerPolicy("worker-1"),
    )

    writer = worker.run_once()
    assert writer.status == "node-succeeded"
    assert writer.run_status == "queued"
    assert writer.outcome_artifact_digest is not None
    writer_outcome = cast("dict[str, object]", artifacts.get_json(writer.outcome_artifact_digest))
    assert writer_outcome["status"] == "succeeded"
    assert writer_outcome["context_artifact"] is not None
    assert writer_outcome["proposal_artifact"] is not None
    assert writer_outcome["provider_artifact"] is not None
    assert writer_outcome["effect_artifact"] is not None
    checks = cast("list[object]", writer_outcome["checks"])
    assert len(checks) == 1
    first_check = cast("dict[str, object]", checks[0])
    stdout_artifact = cast("dict[str, object]", first_check["stdout_artifact"])
    assert artifacts.get_bytes(cast("str", stdout_artifact["digest"])) == b"write-check\n"

    verify = worker.run_once()
    assert verify.status == "node-succeeded"
    assert verify.run_status == "succeeded"
    assert verify.outcome_artifact_digest is not None
    verify_outcome = cast("dict[str, object]", artifacts.get_json(verify.outcome_artifact_digest))
    assert verify_outcome["status"] == "succeeded"
    assert verify_outcome["context_artifact"] is None
    assert verify_outcome["proposal_artifact"] is None
    assert verify_outcome["provider_artifact"] is None
    assert verify_outcome["effect_artifact"] is None
    assert worker.run_once().status == "idle"

    assert len(provider.calls) == 1
    assert provider.calls[0].context.files[0].path == "src/value.py"
    assert str(repository) not in repr(provider.calls[0].context)
    assert tuple(command.check_id for command in acceptance.commands) == (
        "write-check",
        "verify-check",
    )
    first_success = next(
        event
        for event in events.read_stream("alpha:run:run-1")
        if event.event_type == "alpha.node.succeeded"
    )
    assert acceptance.specs[1].base_commit == first_success.payload["head_commit"]
    assert acceptance.specs[1].base_commit != base_commit
    assert runtime.inspect_run("run-1").retained_worktree
    restarted = AlphaRuntimeApiService(
        EventStore(events.path),
        repository,
        isolation_root=isolation,
    )
    assert restarted.replay_run("run-1").run.status == "succeeded"


def test_worker_lease_covers_provider_and_each_acceptance_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, events, artifacts, repository, _, base_commit = _runtime(tmp_path)
    _submit(
        runtime,
        repository,
        base_commit,
        writer_only=True,
        writer_check_ids=("write-check-1", "write-check-2"),
    )
    claimed_at = datetime.now(UTC)
    monkeypatch.setattr("blackcell.bootstrap.alpha_worker.utc_now", lambda: claimed_at)
    lifecycle = GitWorktreeLifecycle()
    acceptance = RecordingAcceptance(lifecycle)
    worker = AlphaRuntimeWorker(
        runtime=runtime,
        artifacts=artifacts,
        provider=ReplacingProvider(),
        change_executor=TextChangeExecutor(lifecycle),
        acceptance=acceptance,
        worktrees=lifecycle,
        policy=AlphaWorkerPolicy("worker-1", lease_grace_seconds=17),
    )

    result = worker.run_once()

    assert result.status == "node-succeeded"
    claimed = next(
        event
        for event in events.read_stream("alpha:run:run-1")
        if event.event_type == "alpha.node.claimed"
    )
    assert datetime.fromisoformat(cast("str", claimed.payload["expires_at"])) == (
        claimed_at + timedelta(seconds=30 * 3 + 17)
    )
    assert tuple(command.check_id for command in acceptance.commands) == (
        "write-check-1",
        "write-check-2",
    )


def test_worker_records_content_free_provider_failure_and_retains_checkout(
    tmp_path: Path,
) -> None:
    runtime, events, artifacts, repository, _, base_commit = _runtime(tmp_path)
    _submit(runtime, repository, base_commit, writer_only=True)
    lifecycle = GitWorktreeLifecycle()
    worker = AlphaRuntimeWorker(
        runtime=runtime,
        artifacts=artifacts,
        provider=FailingProvider(),
        change_executor=TextChangeExecutor(lifecycle),
        acceptance=RecordingAcceptance(lifecycle),
        worktrees=lifecycle,
        policy=AlphaWorkerPolicy("worker-1"),
    )

    result = worker.run_once()

    assert result.status == "node-failed"
    assert result.run_status == "failed"
    assert result.failure_code == "invalid-alpha-change-gateway-result"
    assert result.outcome_artifact_digest is not None
    outcome = cast("dict[str, object]", artifacts.get_json(result.outcome_artifact_digest))
    assert outcome["status"] == "failed"
    assert outcome["failure_code"] == result.failure_code
    assert outcome["context_artifact"] is not None
    assert outcome["proposal_artifact"] is None
    failed = next(
        event
        for event in events.read_stream("alpha:run:run-1")
        if event.event_type == "alpha.node.failed"
    )
    assert failed.payload["result_digest"] == result.outcome_artifact_digest
    assert failed.payload["retained_worktree"] is True
    inspection = cast("Mapping[str, object]", failed.payload["inspection"])
    assert Path(str(inspection["worktree_path"])).is_dir()
    serialized = repr(tuple(event.payload for event in events.read_stream("alpha:run:run-1")))
    assert "sensitive-provider-detail" not in serialized
    assert str(repository) not in str(result.failure_code)


def test_worker_records_context_and_dispatch_before_provider_call(tmp_path: Path) -> None:
    runtime, events, artifacts, repository, _, base_commit = _runtime(tmp_path)
    _submit(runtime, repository, base_commit, writer_only=True)
    lifecycle = GitWorktreeLifecycle()
    provider = DispatchObservingProvider(events)
    worker = AlphaRuntimeWorker(
        runtime=runtime,
        artifacts=artifacts,
        provider=provider,
        change_executor=TextChangeExecutor(lifecycle),
        acceptance=RecordingAcceptance(lifecycle),
        worktrees=lifecycle,
        policy=AlphaWorkerPolicy("worker-1"),
    )

    result = worker.run_once()

    assert result.status == "node-succeeded"
    assert provider.dispatch_event_id is not None
    assert len(provider.calls) == 1
    call = provider.calls[0]
    dispatch = next(
        event
        for event in events.read_stream("alpha:run:run-1")
        if event.event_id == provider.dispatch_event_id
    )
    prepared = events.read_stream("alpha:run:run-1")[2]
    assert dispatch.causation_id == prepared.event_id
    assert call.causation_id == dispatch.event_id
    context_digest = cast("str", dispatch.payload["context_artifact_digest"])
    context = cast("dict[str, object]", artifacts.get_json(context_digest))
    assert json_digest(context) == call.context.digest


def test_worker_persists_failed_check_streams_and_committed_recovery_head(
    tmp_path: Path,
) -> None:
    runtime, events, artifacts, repository, _, base_commit = _runtime(tmp_path)
    _submit(runtime, repository, base_commit, writer_only=True)
    lifecycle = GitWorktreeLifecycle()
    worker = AlphaRuntimeWorker(
        runtime=runtime,
        artifacts=artifacts,
        provider=ReplacingProvider(),
        change_executor=TextChangeExecutor(lifecycle),
        acceptance=RecordingAcceptance(lifecycle, failing_check_id="write-check"),
        worktrees=lifecycle,
        policy=AlphaWorkerPolicy("worker-1"),
    )

    result = worker.run_once()

    assert result.status == "node-failed"
    assert result.failure_code == "alpha-acceptance-check-failed"
    assert result.outcome_artifact_digest is not None
    outcome = cast("dict[str, object]", artifacts.get_json(result.outcome_artifact_digest))
    assert outcome["status"] == "failed"
    assert outcome["head_commit"] != base_commit
    checks = cast("list[object]", outcome["checks"])
    assert len(checks) == 1
    check = cast("dict[str, object]", checks[0])
    assert check["passed"] is False
    failed = next(
        event
        for event in events.read_stream("alpha:run:run-1")
        if event.event_type == "alpha.node.failed"
    )
    inspection = cast("Mapping[str, object]", failed.payload["inspection"])
    worktree = Path(cast("str", inspection["worktree_path"]))
    assert worktree.is_dir()
    assert (worktree / "src" / "value.py").read_text() == "VALUE = 2\n"
    assert _git_text(worktree, "status", "--porcelain") == ""


def test_worker_acknowledges_cancellation_from_acceptance_callback(tmp_path: Path) -> None:
    runtime, events, artifacts, repository, _, base_commit = _runtime(tmp_path)
    _submit(runtime, repository, base_commit, writer_only=True)
    lifecycle = GitWorktreeLifecycle()
    worker = AlphaRuntimeWorker(
        runtime=runtime,
        artifacts=artifacts,
        provider=ReplacingProvider(),
        change_executor=TextChangeExecutor(lifecycle),
        acceptance=CancelingAcceptance(runtime),
        worktrees=lifecycle,
        policy=AlphaWorkerPolicy("worker-1"),
    )

    result = worker.run_once()

    assert result.status == "node-canceled"
    assert result.run_status == "canceled"
    assert result.outcome_artifact_digest is not None
    outcome = cast("dict[str, object]", artifacts.get_json(result.outcome_artifact_digest))
    assert outcome["status"] == "canceled"
    assert outcome["failure_code"] is None
    assert outcome["effect_artifact"] is not None
    run = runtime.inspect_run("run-1")
    assert run.cancellation_requested
    assert run.retained_worktree
    canceled = next(
        event
        for event in events.read_stream("alpha:run:run-1")
        if event.event_type == "alpha.node.canceled"
    )
    assert canceled.payload["result_digest"] == result.outcome_artifact_digest
    event_types = tuple(event.event_type for event in events.read_stream("alpha:run:run-1"))
    assert event_types[-3:] == (
        "alpha.run.cancel-requested",
        "alpha.node.canceled",
        "alpha.run.canceled",
    )


def test_worker_converts_success_race_to_durable_canceled_outcome(tmp_path: Path) -> None:
    runtime, events, artifacts, repository, _, base_commit = _runtime(tmp_path)
    _submit(runtime, repository, base_commit, writer_only=True)
    lifecycle = GitWorktreeLifecycle()
    racing_runtime = cast("AlphaRuntimeWorkerPort", CancelBeforeSuccessRuntime(runtime))
    worker = AlphaRuntimeWorker(
        runtime=racing_runtime,
        artifacts=artifacts,
        provider=ReplacingProvider(),
        change_executor=TextChangeExecutor(lifecycle),
        acceptance=RecordingAcceptance(lifecycle),
        worktrees=lifecycle,
        policy=AlphaWorkerPolicy("worker-1"),
    )

    result = worker.run_once()

    assert result.status == "node-canceled"
    assert result.run_status == "canceled"
    assert result.outcome_artifact_digest is not None
    outcome = cast("dict[str, object]", artifacts.get_json(result.outcome_artifact_digest))
    assert outcome["status"] == "canceled"
    assert outcome["head_commit"] != base_commit
    assert len(cast("list[object]", outcome["checks"])) == 1
    canceled = next(
        event
        for event in events.read_stream("alpha:run:run-1")
        if event.event_type == "alpha.node.canceled"
    )
    assert canceled.payload["result_digest"] == result.outcome_artifact_digest


def _runtime(
    tmp_path: Path,
) -> tuple[AlphaRuntimeApiService, EventStore, ArtifactStore, Path, Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "BlackCell Test")
    _git(repository, "config", "user.email", "blackcell@example.invalid")
    (repository / "src").mkdir()
    (repository / "src" / "value.py").write_text("VALUE = 1\n")
    _git(repository, "add", "src/value.py")
    _git(repository, "commit", "-m", "initial")
    base_commit = _git_text(repository, "rev-parse", "HEAD")
    events = EventStore(tmp_path / "data" / "state.sqlite3")
    artifacts = ArtifactStore(
        tmp_path / "data" / "artifacts",
        database_path=events.path,
        max_total_bytes=16 * 1024 * 1024,
    )
    isolation = (tmp_path / "isolation").resolve()
    runtime = AlphaRuntimeApiService(
        events,
        repository.resolve(),
        isolation_root=isolation,
        artifacts=artifacts,
    )
    return runtime, events, artifacts, repository.resolve(), isolation, base_commit


def _submit(
    runtime: AlphaRuntimeApiService,
    repository: Path,
    base_commit: str,
    *,
    writer_only: bool = False,
    writer_check_ids: tuple[str, ...] = ("write-check",),
) -> None:
    runtime.register_project(
        AlphaProjectRequest(
            schema_version="alpha-project-request/v1",
            project_id="project-1",
            root=str(repository),
            configuration_provider="kernform",
            configuration_version="0.1.0",
            configuration_digest=_CONFIGURATION_DIGEST,
            idempotency_key="project-1",
        ),
        principal_id="operator",
    )
    runtime.accept_intent(
        AlphaIntentRequest(
            schema_version="alpha-intent-request/v1",
            intent_id="intent-1",
            project_id="project-1",
            objective="Apply and verify one bounded alpha change.",
            constraints=("Only change the admitted file.",),
            assumptions=(),
            unresolved_questions=(),
            idempotency_key="intent-1",
        ),
        principal_id="operator",
    )
    writer_budget = AlphaNodeBudget(1_000, 1_000, 30, 1_000, 1)
    check_budget = AlphaNodeBudget(0, 0, 30, 0, 0)
    writer = AlphaPlanNode(
        node_id="write",
        objective="Update the bounded value.",
        depends_on=(),
        budget=writer_budget,
        effects=("repository-read", "repository-write", "process"),
        allowed_paths=("src/value.py",),
        checks=tuple(
            AlphaAcceptanceCheck(check_id, ("python", "-m", "compileall", "src"))
            for check_id in writer_check_ids
        ),
    )
    verify = AlphaPlanNode(
        node_id="verify",
        objective="Verify the committed value.",
        depends_on=("write",),
        budget=check_budget,
        effects=("repository-read", "process"),
        allowed_paths=(),
        checks=(AlphaAcceptanceCheck("verify-check", ("python", "-m", "compileall", "src")),),
    )
    runtime.accept_plan(
        AlphaPlanRequest(
            schema_version="alpha-plan-request/v1",
            plan_id="plan-1",
            project_id="project-1",
            intent_id="intent-1",
            base_commit=base_commit,
            allowed_effects=("repository-read", "repository-write", "process"),
            nodes=(writer,) if writer_only else (writer, verify),
            idempotency_key="plan-1",
        ),
        principal_id="operator",
    )
    runtime.submit_run(
        AlphaRunRequest(
            schema_version="alpha-run-request/v1",
            run_id="run-1",
            project_id="project-1",
            intent_id="intent-1",
            plan_id="plan-1",
            idempotency_key="run-1",
        ),
        principal_id="operator",
    )


def _git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
    )


def _git_text(cwd: Path, *arguments: str) -> str:
    return _git(cwd, *arguments).stdout.decode("utf-8").strip()
