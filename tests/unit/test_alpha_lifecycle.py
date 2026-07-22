from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest

from blackcell.adapters.execution.worktree import (
    GitWorktreeLifecycle,
    WorktreeExecutionSpec,
    WorktreeRemoval,
)
from blackcell.bootstrap.alpha_runtime import AlphaRuntimeApiService
from blackcell.interfaces.http import (
    AlphaAcceptanceCheck,
    AlphaCancelRunRequest,
    AlphaIntentRequest,
    AlphaNodeBudget,
    AlphaPlanNode,
    AlphaPlanRequest,
    AlphaProjectRequest,
    AlphaRunRequest,
    RuntimeApiError,
    RuntimeApiFailureCode,
)
from blackcell.kernel import EventStore, utc_now
from blackcell.kernel._json import bytes_digest
from blackcell.orchestration.alpha_lifecycle import (
    AlphaLifecycleError,
    alpha_provider_request_id,
    fold_alpha_run_lifecycle,
)

_CONFIGURATION_DIGEST = "sha256:" + ("a" * 64)
_CONTEXT_DIGEST = "sha256:" + ("c" * 64)


class SimulatedCleanupCrash(BaseException):
    pass


class CrashDuringRemovalWorktrees:
    def __init__(self, delegate: GitWorktreeLifecycle, *, perform_removal: bool) -> None:
        self.delegate = delegate
        self.perform_removal = perform_removal

    def remove_success(
        self,
        spec: WorktreeExecutionSpec,
        *,
        expected_head_commit: str | None = None,
    ) -> WorktreeRemoval:
        if self.perform_removal:
            self.delegate.remove_success(
                spec,
                expected_head_commit=expected_head_commit,
            )
        raise SimulatedCleanupCrash

    def __getattr__(self, name: str) -> object:
        return getattr(self.delegate, name)


def test_claim_prepares_exact_worktree_and_completion_is_fenced(tmp_path: Path) -> None:
    service, events, repository, isolation, base_commit = _runtime(tmp_path)
    _submit(service, repository, base_commit, "run-1")

    first = service.prepare_node(
        "run-1",
        "change",
        worker_id="worker-1",
        lease_expires_at=utc_now() + timedelta(minutes=5),
    )
    running = service.inspect_run("run-1")
    assert running.status == "running"
    assert running.active_node_id == "change"
    assert (running.attempt, running.fencing_token) == (1, 1)
    assert first.spec.base_commit == base_commit
    assert first.spec.allowed_paths == ("src",)
    assert first.spec.max_changed_paths == 2
    assert first.inspection.changed_paths == ()
    assert first.spec.worktree_path.is_dir()

    restarted = AlphaRuntimeApiService(
        EventStore(events.path),
        repository,
        isolation_root=isolation,
    )
    assert restarted.reconcile_startup(principal_id="startup") == (restarted.inspect_run("run-1"),)
    assert restarted.inspect_run("run-1").status == "queued"

    second = restarted.prepare_node(
        "run-1",
        "change",
        worker_id="worker-2",
        lease_expires_at=utc_now() + timedelta(minutes=5),
    )
    assert second.spec.lease.attempt == 2
    assert second.spec.lease.fencing_token == 2
    assert second.spec.worktree_path != first.spec.worktree_path
    with pytest.raises(RuntimeApiError) as stale:
        restarted.record_node_success(
            first.spec,
            result_digest=bytes_digest(b"stale"),
            principal_id="worker-1",
        )
    assert stale.value.code is RuntimeApiFailureCode.CONFLICT

    succeeded = restarted.record_node_success(
        second.spec,
        result_digest=bytes_digest(b"accepted"),
        principal_id="worker-2",
    )
    assert succeeded.status == "succeeded"
    assert succeeded.retained_worktree
    assert succeeded.active_node_id is None
    assert (succeeded.attempt, succeeded.fencing_token) == (2, 2)
    assert _run_event_types(events, "run-1") == (
        "alpha.run.queued",
        "alpha.node.claimed",
        "alpha.node.worktree-prepared",
        "alpha.node.requeued",
        "alpha.node.claimed",
        "alpha.node.worktree-prepared",
        "alpha.node.succeeded",
        "alpha.run.succeeded",
    )
    assert second.spec.worktree_path.is_dir()


def test_success_records_retained_head_and_dependent_writer_inherits_it(
    tmp_path: Path,
) -> None:
    service, events, repository, isolation, base_commit = _runtime(tmp_path)
    budget = AlphaNodeBudget(1_000, 1_000, 60, 1_000, 2)
    nodes = (
        AlphaPlanNode(
            node_id="write-first",
            objective="Apply the first bounded change.",
            depends_on=(),
            budget=budget,
            effects=("repository-read", "repository-write", "process"),
            allowed_paths=("src",),
            checks=(AlphaAcceptanceCheck("first-check", ("python", "-m", "compileall", "src")),),
        ),
        AlphaPlanNode(
            node_id="write-second",
            objective="Apply the dependent bounded change.",
            depends_on=("write-first",),
            budget=budget,
            effects=("repository-read", "repository-write", "process"),
            allowed_paths=("src",),
            checks=(AlphaAcceptanceCheck("second-check", ("python", "-m", "compileall", "src")),),
        ),
    )
    _submit(service, repository, base_commit, "run-1", nodes=nodes)
    lifecycle = GitWorktreeLifecycle()

    first = service.prepare_node(
        "run-1",
        "write-first",
        worker_id="worker-1",
        lease_expires_at=utc_now() + timedelta(minutes=5),
    )
    (first.spec.worktree_path / "src" / "value.py").write_text("VALUE = 2\n")
    with pytest.raises(RuntimeApiError) as dirty_success:
        service.record_node_success(
            first.spec,
            result_digest=bytes_digest(b"premature-result"),
            principal_id="worker-1",
        )
    assert dirty_success.value.code is RuntimeApiFailureCode.CONFLICT
    assert service.inspect_run("run-1").status == "running"
    first_commit = lifecycle.commit_changes(first.spec)
    queued = service.record_node_success(
        first.spec,
        result_digest=bytes_digest(b"first-result"),
        principal_id="worker-1",
    )
    assert queued.status == "queued"
    assert queued.retained_worktree
    assert first_commit.head_commit != base_commit
    maintenance = service.maintain_successful_worktrees(
        max_retained=0,
        principal_id="retention-worker",
    )
    assert maintenance.cleaned == 1
    assert not first.spec.worktree_path.exists()

    second = service.prepare_node(
        "run-1",
        "write-second",
        worker_id="worker-2",
        lease_expires_at=utc_now() + timedelta(minutes=5),
    )
    assert second.spec.base_commit == first_commit.head_commit
    assert (second.spec.worktree_path / "src" / "value.py").read_text() == "VALUE = 2\n"
    (second.spec.worktree_path / "src" / "next.py").write_text("NEXT = True\n")
    second_commit = lifecycle.commit_changes(second.spec)
    succeeded = service.record_node_success(
        second.spec,
        result_digest=bytes_digest(b"second-result"),
        principal_id="worker-2",
    )

    assert succeeded.status == "succeeded"
    assert succeeded.retained_worktree
    assert _git_text(second.spec.worktree_path, "rev-parse", "HEAD^") == first_commit.head_commit
    assert not first.spec.worktree_path.exists()
    assert second.spec.worktree_path.is_dir()
    success_events = tuple(
        event
        for event in events.read_stream("alpha:run:run-1")
        if event.event_type == "alpha.node.succeeded"
    )
    assert tuple(event.payload["head_commit"] for event in success_events) == (
        first_commit.head_commit,
        second_commit.head_commit,
    )
    restarted = AlphaRuntimeApiService(
        EventStore(events.path),
        repository,
        isolation_root=isolation,
    )
    assert restarted.inspect_run("run-1") == succeeded
    assert restarted.replay_run("run-1").run == succeeded


def test_running_cancellation_requires_exact_lease_and_retains_changes(
    tmp_path: Path,
) -> None:
    service, events, repository, _, base_commit = _runtime(tmp_path)
    _submit(service, repository, base_commit, "run-1")
    prepared = service.prepare_node(
        "run-1",
        "change",
        worker_id="worker-1",
        lease_expires_at=utc_now() + timedelta(minutes=5),
    )
    target = prepared.spec.worktree_path / "src" / "value.py"
    target.write_text("VALUE = 2\n")

    canceling = service.cancel_run(
        "run-1",
        AlphaCancelRunRequest(
            schema_version="alpha-cancel-run-request/v1",
            idempotency_key="cancel-run-1",
        ),
        principal_id="operator",
    )
    assert canceling.status == "canceling"
    assert canceling.cancellation_requested
    assert canceling.active_node_id == "change"

    stale_spec = replace(
        prepared.spec,
        lease=replace(
            prepared.spec.lease,
            fencing_token=prepared.spec.lease.fencing_token + 1,
        ),
    )
    with pytest.raises(RuntimeApiError) as stale:
        service.acknowledge_cancellation(stale_spec, principal_id="worker-1")
    assert stale.value.code is RuntimeApiFailureCode.CONFLICT

    canceled = service.acknowledge_cancellation(
        prepared.spec,
        result_digest="sha256:" + "a" * 64,
        principal_id="worker-1",
    )
    assert canceled.status == "canceled"
    assert canceled.retained_worktree
    assert canceled.active_node_id is None
    cancellation_event = next(
        event
        for event in events.read_stream("alpha:run:run-1")
        if event.event_type == "alpha.node.canceled"
    )
    assert cancellation_event.payload["result_digest"] == "sha256:" + "a" * 64
    assert target.read_text() == "VALUE = 2\n"
    assert prepared.spec.worktree_path.is_dir()
    assert _run_event_types(events, "run-1")[-3:] == (
        "alpha.run.cancel-requested",
        "alpha.node.canceled",
        "alpha.run.canceled",
    )


def test_startup_reconciliation_requeues_unchanged_and_retains_changed_work(
    tmp_path: Path,
) -> None:
    service, events, repository, isolation, base_commit = _runtime(tmp_path)
    _submit(service, repository, base_commit, "run-missing")
    missing = service.prepare_node(
        "run-missing",
        "change",
        worker_id="worker-missing",
        lease_expires_at=utc_now() + timedelta(minutes=5),
    )
    _git(repository, "worktree", "remove", str(missing.spec.worktree_path))

    service.submit_run(_run("run-changed"), principal_id="operator")
    changed = service.prepare_node(
        "run-changed",
        "change",
        worker_id="worker-changed",
        lease_expires_at=utc_now() + timedelta(minutes=5),
    )
    changed_target = changed.spec.worktree_path / "src" / "value.py"
    changed_target.write_text("VALUE = 9\n")

    restarted = AlphaRuntimeApiService(
        EventStore(events.path),
        repository,
        isolation_root=isolation,
    )
    reconciled = restarted.reconcile_startup(principal_id="startup")
    assert tuple(item.run_id for item in reconciled) == ("run-missing", "run-changed")
    by_id = {item.run_id: item for item in reconciled}
    assert by_id["run-missing"].status == "queued"
    assert not by_id["run-missing"].retained_worktree
    assert by_id["run-changed"].status == "reconciliation-required"
    assert by_id["run-changed"].retained_worktree
    assert changed_target.read_text() == "VALUE = 9\n"
    assert changed.spec.worktree_path.is_dir()

    for stale_spec in (missing.spec, changed.spec):
        with pytest.raises(RuntimeApiError) as stale:
            restarted.record_node_success(
                stale_spec,
                result_digest=bytes_digest(b"stale"),
                principal_id=stale_spec.lease.worker_id,
            )
        assert stale.value.code is RuntimeApiFailureCode.CONFLICT
    assert _run_event_types(events, "run-missing")[-1] == "alpha.node.requeued"
    assert _run_event_types(events, "run-changed")[-2:] == (
        "alpha.node.reconciliation-required",
        "alpha.run.reconciliation-required",
    )


def test_provider_dispatch_marker_is_durable_fenced_and_strict(tmp_path: Path) -> None:
    service, events, repository, _, base_commit = _runtime(tmp_path)
    _submit(service, repository, base_commit, "run-1")
    prepared = service.prepare_node(
        "run-1",
        "change",
        worker_id="worker-1",
        lease_expires_at=utc_now() + timedelta(minutes=5),
    )
    request_id = alpha_provider_request_id(prepared.spec.lease.digest)

    with pytest.raises(RuntimeApiError) as wrong_worker:
        service.record_provider_dispatch(
            prepared.spec,
            provider_request_id=request_id,
            context_digest=_CONTEXT_DIGEST,
            context_artifact_digest=_CONTEXT_DIGEST,
            principal_id="worker-2",
        )
    assert wrong_worker.value.code is RuntimeApiFailureCode.CONFLICT
    with pytest.raises(RuntimeApiError) as wrong_request:
        service.record_provider_dispatch(
            prepared.spec,
            provider_request_id="alpha-change-wrong",
            context_digest=_CONTEXT_DIGEST,
            context_artifact_digest=_CONTEXT_DIGEST,
            principal_id="worker-1",
        )
    assert wrong_request.value.code is RuntimeApiFailureCode.INVALID_REQUEST

    dispatch_id = service.record_provider_dispatch(
        prepared.spec,
        provider_request_id=request_id,
        context_digest=_CONTEXT_DIGEST,
        context_artifact_digest=_CONTEXT_DIGEST,
        principal_id="worker-1",
    )
    stream = events.read_stream("alpha:run:run-1")
    dispatch = stream[-1]
    assert dispatch.event_id == dispatch_id
    assert dispatch.event_type == "alpha.node.provider-dispatch-started"
    assert dispatch.causation_id == prepared.prepared_event_id
    assert dispatch.payload["provider_request_id"] == request_id
    assert dispatch.payload["context_digest"] == _CONTEXT_DIGEST
    state = fold_alpha_run_lifecycle("run-1", {"change": ()}, stream)
    assert state.active_lease is not None
    assert state.active_lease.provider_request_id == request_id
    assert state.active_lease.provider_context_digest == _CONTEXT_DIGEST
    assert state.active_lease.provider_dispatch_event_id == dispatch_id

    unprepared_dispatch = replace(
        dispatch,
        stream_sequence=3,
        causation_id=stream[1].event_id,
    )
    with pytest.raises(AlphaLifecycleError):
        fold_alpha_run_lifecycle("run-1", {"change": ()}, (*stream[:2], unprepared_dispatch))
    with pytest.raises(RuntimeApiError) as duplicate:
        service.record_provider_dispatch(
            prepared.spec,
            provider_request_id=request_id,
            context_digest=_CONTEXT_DIGEST,
            context_artifact_digest=_CONTEXT_DIGEST,
            principal_id="worker-1",
        )
    assert duplicate.value.code is RuntimeApiFailureCode.CONFLICT
    stale_spec = replace(
        prepared.spec,
        lease=replace(
            prepared.spec.lease,
            fencing_token=prepared.spec.lease.fencing_token + 1,
        ),
    )
    with pytest.raises(RuntimeApiError) as stale:
        service.record_provider_dispatch(
            stale_spec,
            provider_request_id=alpha_provider_request_id(stale_spec.lease.digest),
            context_digest=_CONTEXT_DIGEST,
            context_artifact_digest=_CONTEXT_DIGEST,
            principal_id="worker-1",
        )
    assert stale.value.code is RuntimeApiFailureCode.CONFLICT

    readonly_root = tmp_path / "readonly-runtime"
    readonly_root.mkdir()
    readonly, _, readonly_repository, _, readonly_base = _runtime(readonly_root)
    readonly_budget = AlphaNodeBudget(0, 0, 60, 0, 0)
    _submit(
        readonly,
        readonly_repository,
        readonly_base,
        "run-readonly",
        nodes=(
            AlphaPlanNode(
                node_id="check",
                objective="Run one read-only check.",
                depends_on=(),
                budget=readonly_budget,
                effects=("repository-read", "process"),
                allowed_paths=(),
                checks=(AlphaAcceptanceCheck("compile", ("python", "-m", "compileall", "src")),),
            ),
        ),
    )
    readonly_prepared = readonly.prepare_node(
        "run-readonly",
        "check",
        worker_id="worker-readonly",
        lease_expires_at=utc_now() + timedelta(minutes=5),
    )
    with pytest.raises(RuntimeApiError) as readonly_dispatch:
        readonly.record_provider_dispatch(
            readonly_prepared.spec,
            provider_request_id=alpha_provider_request_id(readonly_prepared.spec.lease.digest),
            context_digest=_CONTEXT_DIGEST,
            context_artifact_digest=_CONTEXT_DIGEST,
            principal_id="worker-readonly",
        )
    assert readonly_dispatch.value.code is RuntimeApiFailureCode.CONFLICT


def test_startup_reconciliation_never_requeues_ambiguous_provider_dispatch(
    tmp_path: Path,
) -> None:
    service, events, repository, isolation, base_commit = _runtime(tmp_path)
    _submit(service, repository, base_commit, "run-missing")
    prepared_by_run = {
        "run-missing": service.prepare_node(
            "run-missing",
            "change",
            worker_id="worker-missing",
            lease_expires_at=utc_now() + timedelta(minutes=5),
        )
    }
    for run_id in ("run-unchanged", "run-changed", "run-canceled"):
        service.submit_run(_run(run_id), principal_id="operator")
        prepared_by_run[run_id] = service.prepare_node(
            run_id,
            "change",
            worker_id=f"worker-{run_id.removeprefix('run-')}",
            lease_expires_at=utc_now() + timedelta(minutes=5),
        )
    for prepared in prepared_by_run.values():
        service.record_provider_dispatch(
            prepared.spec,
            provider_request_id=alpha_provider_request_id(prepared.spec.lease.digest),
            context_digest=_CONTEXT_DIGEST,
            context_artifact_digest=_CONTEXT_DIGEST,
            principal_id=prepared.spec.lease.worker_id,
        )

    _git(
        repository,
        "worktree",
        "remove",
        str(prepared_by_run["run-missing"].spec.worktree_path),
    )
    changed_target = prepared_by_run["run-changed"].spec.worktree_path / "src" / "value.py"
    changed_target.write_text("VALUE = 9\n")
    service.cancel_run(
        "run-canceled",
        AlphaCancelRunRequest(
            schema_version="alpha-cancel-run-request/v1",
            idempotency_key="cancel-ambiguous-run",
        ),
        principal_id="operator",
    )

    restarted = AlphaRuntimeApiService(
        EventStore(events.path),
        repository,
        isolation_root=isolation,
    )
    reconciled = restarted.reconcile_startup(principal_id="startup")
    by_id = {item.run_id: item for item in reconciled}
    assert set(by_id) == set(prepared_by_run)
    for run_id in ("run-missing", "run-unchanged", "run-changed"):
        assert by_id[run_id].status == "reconciliation-required"
        assert "alpha.node.requeued" not in _run_event_types(events, run_id)
        reconciliation = next(
            event
            for event in events.read_stream(f"alpha:run:{run_id}")
            if event.event_type == "alpha.node.reconciliation-required"
        )
        assert reconciliation.payload["failure_code"] == "alpha-provider-dispatch-ambiguous"
    assert not by_id["run-missing"].retained_worktree
    assert by_id["run-unchanged"].retained_worktree
    assert by_id["run-changed"].retained_worktree
    assert changed_target.read_text() == "VALUE = 9\n"
    assert by_id["run-canceled"].status == "canceled"
    assert "alpha.node.requeued" not in _run_event_types(events, "run-canceled")


def test_successful_worktree_retention_cleans_oldest_and_preserves_branches(
    tmp_path: Path,
) -> None:
    service, events, repository, _, base_commit = _runtime(tmp_path)
    _submit(service, repository, base_commit, "run-oldest")
    prepared_by_run = {
        "run-oldest": service.prepare_node(
            "run-oldest",
            "change",
            worker_id="worker-oldest",
            lease_expires_at=utc_now() + timedelta(minutes=5),
        )
    }
    for run_id in ("run-middle", "run-newest"):
        service.submit_run(_run(run_id), principal_id="operator")
        prepared_by_run[run_id] = service.prepare_node(
            run_id,
            "change",
            worker_id=f"worker-{run_id.removeprefix('run-')}",
            lease_expires_at=utc_now() + timedelta(minutes=5),
        )
    for run_id, prepared in prepared_by_run.items():
        service.record_node_success(
            prepared.spec,
            result_digest=bytes_digest(run_id.encode()),
            principal_id=prepared.spec.lease.worker_id,
        )

    report = service.maintain_successful_worktrees(
        max_retained=1,
        principal_id="retention-worker",
    )

    assert report.pending_recovered == 0
    assert (report.cleanup_requested, report.cleaned, report.failed) == (2, 2, 0)
    assert report.retained == 1
    assert report.quota_satisfied
    for run_id in ("run-oldest", "run-middle"):
        prepared = prepared_by_run[run_id]
        assert not prepared.spec.worktree_path.exists()
        assert not service.inspect_run(run_id).retained_worktree
        assert service.replay_run(run_id).run == service.inspect_run(run_id)
        assert _git_text(repository, "rev-parse", prepared.spec.branch_name) == base_commit
        assert _run_event_types(events, run_id)[-2:] == (
            "alpha.node.worktree-cleanup-requested",
            "alpha.node.worktree-cleaned",
        )
    newest = prepared_by_run["run-newest"]
    assert newest.spec.worktree_path.is_dir()
    assert service.inspect_run("run-newest").retained_worktree
    repeated = service.maintain_successful_worktrees(
        max_retained=1,
        principal_id="retention-worker",
    )
    assert (repeated.cleanup_requested, repeated.cleaned, repeated.failed) == (0, 0, 0)
    assert repeated.retained == 1


def test_successful_worktree_cleanup_recovers_request_before_effect(tmp_path: Path) -> None:
    _assert_successful_worktree_cleanup_recovery(tmp_path, effect_happened=False)


def test_successful_worktree_cleanup_recovers_effect_before_completion(
    tmp_path: Path,
) -> None:
    _assert_successful_worktree_cleanup_recovery(tmp_path, effect_happened=True)


def _assert_successful_worktree_cleanup_recovery(
    tmp_path: Path,
    *,
    effect_happened: bool,
) -> None:
    service, events, repository, isolation, base_commit = _runtime(tmp_path)
    _submit(service, repository, base_commit, "run-1")
    prepared = service.prepare_node(
        "run-1",
        "change",
        worker_id="worker-1",
        lease_expires_at=utc_now() + timedelta(minutes=5),
    )
    service.record_node_success(
        prepared.spec,
        result_digest=bytes_digest(b"success"),
        principal_id="worker-1",
    )
    crashing = AlphaRuntimeApiService(
        EventStore(events.path),
        repository,
        isolation_root=isolation,
        worktrees=cast(
            "GitWorktreeLifecycle",
            CrashDuringRemovalWorktrees(
                GitWorktreeLifecycle(),
                perform_removal=effect_happened,
            ),
        ),
    )

    with pytest.raises(SimulatedCleanupCrash):
        crashing.maintain_successful_worktrees(
            max_retained=0,
            principal_id="retention-worker",
        )
    assert prepared.spec.worktree_path.exists() is not effect_happened
    assert _run_event_types(events, "run-1")[-1] == ("alpha.node.worktree-cleanup-requested")

    restarted = AlphaRuntimeApiService(
        EventStore(events.path),
        repository,
        isolation_root=isolation,
    )
    report = restarted.maintain_successful_worktrees(
        max_retained=0,
        principal_id="retention-worker",
    )
    assert (report.pending_recovered, report.cleanup_requested) == (1, 0)
    assert (report.cleaned, report.failed, report.retained) == (1, 0, 0)
    assert report.quota_satisfied
    assert not restarted.inspect_run("run-1").retained_worktree
    assert _git_text(repository, "rev-parse", prepared.spec.branch_name) == base_commit
    assert _run_event_types(events, "run-1")[-2:] == (
        "alpha.node.worktree-cleanup-requested",
        "alpha.node.worktree-cleaned",
    )


def test_successful_worktree_cleanup_failure_is_durable_and_not_retried(
    tmp_path: Path,
) -> None:
    service, events, repository, _, base_commit = _runtime(tmp_path)
    _submit(service, repository, base_commit, "run-1")
    prepared = service.prepare_node(
        "run-1",
        "change",
        worker_id="worker-1",
        lease_expires_at=utc_now() + timedelta(minutes=5),
    )
    service.record_node_success(
        prepared.spec,
        result_digest=bytes_digest(b"success"),
        principal_id="worker-1",
    )
    dirty = prepared.spec.worktree_path / "src" / "late-change.py"
    dirty.write_text("LATE = True\n")

    report = service.maintain_successful_worktrees(
        max_retained=0,
        principal_id="retention-worker",
    )

    assert (report.cleanup_requested, report.cleaned, report.failed) == (1, 0, 1)
    assert report.retained == 1
    assert not report.quota_satisfied
    failed = events.read_stream("alpha:run:run-1")[-1]
    assert failed.event_type == "alpha.node.worktree-cleanup-failed"
    assert failed.payload["failure_code"] == "worktree-dirty"
    assert failed.payload["retained_worktree"] is True
    assert service.inspect_run("run-1").status == "succeeded"
    assert service.inspect_run("run-1").retained_worktree
    event_count = len(events.read_stream("alpha:run:run-1"))

    repeated = service.maintain_successful_worktrees(
        max_retained=0,
        principal_id="retention-worker",
    )
    assert (repeated.cleanup_requested, repeated.cleaned, repeated.failed) == (0, 0, 0)
    assert not repeated.quota_satisfied
    assert len(events.read_stream("alpha:run:run-1")) == event_count
    assert dirty.read_text() == "LATE = True\n"


def _runtime(
    tmp_path: Path,
) -> tuple[AlphaRuntimeApiService, EventStore, Path, Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "blackcell@example.invalid")
    _git(repository, "config", "user.name", "BlackCell Test")
    (repository / "src").mkdir()
    (repository / "src" / "value.py").write_text("VALUE = 1\n")
    _git(repository, "add", "src/value.py")
    _git(repository, "commit", "-q", "-m", "base")
    base_commit = _git_text(repository, "rev-parse", "HEAD")
    events = EventStore(tmp_path / "data" / "state.sqlite3")
    isolation = (tmp_path / "isolation").resolve()
    service = AlphaRuntimeApiService(
        events,
        repository.resolve(),
        isolation_root=isolation,
    )
    return service, events, repository.resolve(), isolation, base_commit


def _submit(
    service: AlphaRuntimeApiService,
    repository: Path,
    base_commit: str,
    run_id: str,
    *,
    nodes: tuple[AlphaPlanNode, ...] | None = None,
) -> None:
    service.register_project(
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
    service.accept_intent(
        AlphaIntentRequest(
            schema_version="alpha-intent-request/v1",
            intent_id="intent-1",
            project_id="project-1",
            objective="Apply one bounded alpha change.",
            constraints=("Retain interrupted work.",),
            assumptions=(),
            unresolved_questions=(),
            idempotency_key="intent-1",
        ),
        principal_id="operator",
    )
    budget = AlphaNodeBudget(
        max_input_tokens=1_000,
        max_output_tokens=1_000,
        timeout_seconds=60,
        max_cost_microusd=1_000,
        max_changed_files=2,
    )
    service.accept_plan(
        AlphaPlanRequest(
            schema_version="alpha-plan-request/v1",
            plan_id="plan-1",
            project_id="project-1",
            intent_id="intent-1",
            base_commit=base_commit,
            allowed_effects=("repository-read", "repository-write", "process"),
            nodes=nodes
            or (
                AlphaPlanNode(
                    node_id="change",
                    objective="Apply a bounded text change.",
                    depends_on=(),
                    budget=budget,
                    effects=("repository-read", "repository-write", "process"),
                    allowed_paths=("src",),
                    checks=(
                        AlphaAcceptanceCheck(
                            check_id="compile",
                            argv=("python", "-m", "compileall", "src"),
                        ),
                    ),
                ),
            ),
            idempotency_key="plan-1",
        ),
        principal_id="operator",
    )
    service.submit_run(_run(run_id), principal_id="operator")


def _run(run_id: str) -> AlphaRunRequest:
    return AlphaRunRequest(
        schema_version="alpha-run-request/v1",
        run_id=run_id,
        project_id="project-1",
        intent_id="intent-1",
        plan_id="plan-1",
        idempotency_key=run_id,
    )


def _run_event_types(events: EventStore, run_id: str) -> tuple[str, ...]:
    return tuple(event.event_type for event in events.read_stream(f"alpha:run:{run_id}"))


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _git_text(cwd: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
