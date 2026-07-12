from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from blackcell.adapters.persistence.sqlite import SQLiteDecisionAttemptJournal
from blackcell.features.request_decision import (
    DecisionAdapterResult,
    DecisionAffordance,
    DecisionArgument,
    DecisionArgumentSpec,
    DecisionAttemptClaim,
    DecisionAttemptInProgress,
    DecisionBudget,
    DecisionCapability,
    DecisionClassification,
    DecisionFailure,
    DecisionFailureKind,
    DecisionFailureRecord,
    DecisionIdentityConflict,
    DecisionJournalError,
    DecisionLocality,
    DecisionPreparation,
    DecisionProposal,
    DecisionRequirements,
    DecisionResponse,
    DecisionRoute,
    DecisionSuccessRecord,
    DecisionUsage,
    RequestDecision,
    RequestDecisionHandler,
)
from blackcell.kernel import ArtifactStore, JsonValue

NOW = datetime(2026, 7, 12, 14, tzinfo=UTC)


class Gateway:
    def __init__(
        self,
        *,
        crash: bool = False,
        completed_at: datetime | None = None,
        selected_at: datetime | None = None,
    ) -> None:
        self.crash = crash
        self.completed_at = completed_at or NOW + timedelta(seconds=2)
        self.selected_at = selected_at or NOW
        self.route_calls = 0
        self.invoke_calls = 0

    def route(self, request: RequestDecision) -> DecisionRoute:
        self.route_calls += 1
        return _route(selected_at=self.selected_at)

    def invoke(
        self,
        request: RequestDecision,
        route: DecisionRoute,
    ) -> DecisionAdapterResult:
        self.invoke_calls += 1
        if self.crash:
            raise RuntimeError("simulated process interruption")
        return DecisionAdapterResult(
            _output(),
            input_tokens=8,
            output_tokens=4,
            latency_ms=10,
            cost_microusd=2,
            deterministic=True,
            completed_at=self.completed_at,
        )


def test_success_is_artifact_first_restart_safe_and_terminal_reentry_is_typed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    request = _request()
    route = _route()
    with SQLiteDecisionAttemptJournal(root) as journal:
        registered = journal.register(request, registered_at=NOW)
        preparation = journal.record_route(
            registered,
            route,
            recorded_at=NOW + timedelta(seconds=1),
        )
        assert isinstance(preparation, DecisionPreparation)
        claim = journal.acquire(preparation, acquired_at=NOW + timedelta(seconds=2))
        assert isinstance(claim, DecisionAttemptClaim)
        admitted = journal.begin_invoke(
            preparation,
            claim,
            invoked_at=NOW + timedelta(seconds=2),
        )
        assert isinstance(admitted, DecisionAttemptClaim)
        claim = admitted
        response = _response(preparation, claim, completed_at=NOW + timedelta(seconds=3))
        usage = _usage(request, claim)
        terminal = journal.succeed(
            preparation,
            claim,
            response,
            usage,
            recorded_at=NOW + timedelta(seconds=4),
        )
        database = journal.database_path

    assert isinstance(terminal, DecisionSuccessRecord)
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("select * from decision_attempt_journal").fetchone()
    assert row is not None
    assert row["status"] == "succeeded"
    artifact_fields = (
        "request_artifact_digest",
        "route_artifact_digest",
        "attempt_artifact_digest",
        "response_artifact_digest",
        "usage_artifact_digest",
    )
    artifacts = ArtifactStore(root, database_path=database)
    assert all(artifacts.verify(str(row[field])) for field in artifact_fields)

    with SQLiteDecisionAttemptJournal(root) as reopened:
        exact = reopened.register(request, registered_at=NOW + timedelta(minutes=1))
        assert exact == registered
        assert (
            reopened.record_route(
                exact,
                route,
                recorded_at=NOW + timedelta(minutes=1),
            )
            == terminal
        )
        assert reopened.acquire(preparation, acquired_at=NOW + timedelta(minutes=1)) == terminal
        assert (
            reopened.succeed(
                preparation,
                claim,
                response,
                usage,
                recorded_at=NOW + timedelta(minutes=1),
            )
            == terminal
        )


def test_real_handler_retry_returns_terminal_without_reinvoking_gateway(tmp_path: Path) -> None:
    gateway = Gateway()
    journal = SQLiteDecisionAttemptJournal(tmp_path / "artifacts")
    moments = iter(
        (
            NOW,
            NOW,
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=4),
            NOW + timedelta(minutes=1),
        )
    )
    handler = RequestDecisionHandler(gateway, journal, clock=lambda: next(moments))
    preparation = handler.prepare(_request())
    assert isinstance(preparation, DecisionPreparation)

    first = handler.handle(preparation)
    second = handler.handle(preparation)

    assert isinstance(first, DecisionSuccessRecord)
    assert second == first
    assert gateway.invoke_calls == 1


def test_handler_classifies_completion_before_invocation_admission(tmp_path: Path) -> None:
    gateway = Gateway(completed_at=NOW + timedelta(seconds=2))
    journal = SQLiteDecisionAttemptJournal(tmp_path / "artifacts")
    moments = iter(
        (
            NOW,
            NOW,
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=3),
            NOW + timedelta(seconds=4),
        )
    )
    handler = RequestDecisionHandler(gateway, journal, clock=lambda: next(moments))
    preparation = handler.prepare(_request())
    assert isinstance(preparation, DecisionPreparation)

    result = handler.handle(preparation)

    assert isinstance(result, DecisionFailureRecord)
    assert result.failure.kind is DecisionFailureKind.INTEGRITY
    assert result.failure.code == "decision_completion_precedes_invocation"
    assert result.failure.failed_at == NOW + timedelta(seconds=3)
    assert gateway.invoke_calls == 1


def test_real_staged_claim_reuse_returns_terminal_before_gateway(tmp_path: Path) -> None:
    gateway = Gateway(
        completed_at=NOW + timedelta(seconds=3),
        selected_at=NOW + timedelta(seconds=3),
    )
    journal = SQLiteDecisionAttemptJournal(tmp_path / "artifacts")
    handler = RequestDecisionHandler(
        gateway,
        journal,
        clock=lambda: NOW + timedelta(seconds=3),
    )
    preparation = handler.prepare(_request())
    assert isinstance(preparation, DecisionPreparation)
    claim = handler.acquire(preparation)
    assert isinstance(claim, DecisionAttemptClaim)

    first = handler.invoke(preparation, claim)
    second = handler.invoke(preparation, claim)

    assert isinstance(first, DecisionSuccessRecord)
    assert second == first
    assert gateway.invoke_calls == 1


def test_concurrent_real_invoke_crosses_gateway_once(tmp_path: Path) -> None:
    gateway = Gateway(
        completed_at=NOW + timedelta(seconds=3),
        selected_at=NOW + timedelta(seconds=3),
    )
    journal = SQLiteDecisionAttemptJournal(tmp_path / "artifacts")
    handler = RequestDecisionHandler(
        gateway,
        journal,
        clock=lambda: NOW + timedelta(seconds=3),
    )
    preparation = handler.prepare(_request())
    assert isinstance(preparation, DecisionPreparation)
    claim = handler.acquire(preparation)
    assert isinstance(claim, DecisionAttemptClaim)

    def invoke() -> object:
        try:
            return handler.invoke(preparation, claim)
        except Exception as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: invoke(), range(2)))

    assert gateway.invoke_calls == 1
    assert any(isinstance(item, DecisionSuccessRecord) for item in results)
    assert all(isinstance(item, DecisionSuccessRecord | DecisionJournalError) for item in results)


def test_process_interruption_leaves_an_active_uncertain_attempt_that_never_reinvokes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    gateway = Gateway(crash=True)
    first_journal = SQLiteDecisionAttemptJournal(root)
    moments = iter((NOW, NOW, NOW, NOW, NOW + timedelta(seconds=1)))
    handler = RequestDecisionHandler(gateway, first_journal, clock=lambda: next(moments))
    preparation = handler.prepare(_request())
    assert isinstance(preparation, DecisionPreparation)

    with pytest.raises(RuntimeError, match="process interruption"):
        handler.handle(preparation)
    first_journal.close()

    with SQLiteDecisionAttemptJournal(root) as restarted:
        with pytest.raises(DecisionAttemptInProgress, match="active or uncertain"):
            restarted.acquire(preparation, acquired_at=NOW + timedelta(minutes=1))
        assert not hasattr(restarted, "recover")
    assert gateway.invoke_calls == 1


def test_request_and_route_identities_are_immutable(tmp_path: Path) -> None:
    journal = SQLiteDecisionAttemptJournal(tmp_path / "artifacts")
    request = _request()
    registered = journal.register(request, registered_at=NOW)

    with pytest.raises(DecisionIdentityConflict, match="different durable content"):
        journal.register(
            replace(request, objective="a different objective"),
            registered_at=NOW,
        )

    preparation = journal.record_route(registered, _route(), recorded_at=NOW)
    assert isinstance(preparation, DecisionPreparation)
    assert (
        journal.record_route(
            registered,
            _route(),
            recorded_at=NOW + timedelta(seconds=1),
        )
        == preparation
    )
    with pytest.raises(DecisionIdentityConflict, match="different route"):
        journal.record_route(
            registered,
            replace(_route(), model_id="reason-v2"),
            recorded_at=NOW,
        )

    with closing(sqlite3.connect(journal.database_path)) as connection:
        count = connection.execute("select count(*) from decision_attempt_journal").fetchone()[0]
        stored_route = connection.execute(
            "select route_id from decision_attempt_journal"
        ).fetchone()[0]
    assert count == 1
    assert stored_route == preparation.route.route_id


def test_concurrent_acquire_commits_one_claim_and_fails_the_other_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    journal = SQLiteDecisionAttemptJournal(root)
    registered = journal.register(_request(), registered_at=NOW)
    preparation = journal.record_route(registered, _route(), recorded_at=NOW)
    assert isinstance(preparation, DecisionPreparation)
    journal.close()

    def acquire() -> object:
        with SQLiteDecisionAttemptJournal(root) as contender:
            try:
                return contender.acquire(
                    preparation,
                    acquired_at=NOW + timedelta(seconds=1),
                )
            except Exception as error:
                return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: acquire(), range(2)))

    assert sum(isinstance(item, DecisionAttemptClaim) for item in results) == 1
    assert sum(isinstance(item, DecisionAttemptInProgress) for item in results) == 1
    with closing(sqlite3.connect(root / "kernel.sqlite3")) as connection:
        row = connection.execute(
            "select fencing_revision, active_claim_token from decision_attempt_journal"
        ).fetchone()
    assert row is not None and row[0] == 1 and str(row[1]).startswith("claim:")


@pytest.mark.parametrize("mismatch", ("token", "revision", "attempt"))
def test_begin_invoke_rejects_forged_claim_without_consuming_the_fence(
    tmp_path: Path,
    mismatch: str,
) -> None:
    journal = SQLiteDecisionAttemptJournal(tmp_path / "artifacts")
    registered = journal.register(_request(), registered_at=NOW)
    preparation = journal.record_route(registered, _route(), recorded_at=NOW)
    assert isinstance(preparation, DecisionPreparation)
    claim = journal.acquire(preparation, acquired_at=NOW + timedelta(seconds=1))
    assert isinstance(claim, DecisionAttemptClaim)
    if mismatch == "token":
        forged = replace(claim, claim_token="claim:forged")
        expected_error = DecisionJournalError
    elif mismatch == "revision":
        forged = replace(claim, fencing_revision=2)
        expected_error = DecisionJournalError
    else:
        attempt = replace(claim.attempt_record.attempt, attempt_number=2)
        forged = replace(
            claim,
            attempt_record=replace(
                claim.attempt_record,
                attempt=attempt,
                attempt_artifact_digest=attempt.attempt_id,
            ),
        )
        expected_error = DecisionIdentityConflict

    with pytest.raises(expected_error):
        journal.begin_invoke(
            preparation,
            forged,
            invoked_at=NOW + timedelta(seconds=2),
        )

    admitted = journal.begin_invoke(
        preparation,
        claim,
        invoked_at=NOW + timedelta(seconds=2),
    )
    assert isinstance(admitted, DecisionAttemptClaim)
    assert admitted.fencing_revision == 2
    assert admitted.invoked_at == NOW + timedelta(seconds=2)


def test_concurrent_begin_invoke_admits_exactly_one_caller(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    journal = SQLiteDecisionAttemptJournal(root)
    registered = journal.register(_request(), registered_at=NOW)
    preparation = journal.record_route(registered, _route(), recorded_at=NOW)
    assert isinstance(preparation, DecisionPreparation)
    claim = journal.acquire(preparation, acquired_at=NOW + timedelta(seconds=1))
    assert isinstance(claim, DecisionAttemptClaim)
    journal.close()

    def begin() -> object:
        with SQLiteDecisionAttemptJournal(root) as contender:
            try:
                return contender.begin_invoke(
                    preparation,
                    claim,
                    invoked_at=NOW + timedelta(seconds=2),
                )
            except Exception as error:
                return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: begin(), range(2)))

    assert sum(isinstance(item, DecisionAttemptClaim) for item in results) == 1
    assert sum(isinstance(item, DecisionJournalError) for item in results) == 1
    with closing(sqlite3.connect(root / "kernel.sqlite3")) as connection:
        row = connection.execute(
            "select fencing_revision, active_claim_token from decision_attempt_journal"
        ).fetchone()
    assert row is not None and row[0] == 2 and str(row[1]).startswith("claim:")


def test_unadmitted_claim_cannot_finalize_an_inference(tmp_path: Path) -> None:
    journal = SQLiteDecisionAttemptJournal(tmp_path / "artifacts")
    registered = journal.register(_request(), registered_at=NOW)
    preparation = journal.record_route(registered, _route(), recorded_at=NOW)
    assert isinstance(preparation, DecisionPreparation)
    claim = journal.acquire(preparation, acquired_at=NOW + timedelta(seconds=1))
    assert isinstance(claim, DecisionAttemptClaim)
    response = _response(preparation, claim, completed_at=NOW + timedelta(seconds=2))

    with pytest.raises(DecisionJournalError, match="not admitted"):
        journal.succeed(
            preparation,
            claim,
            response,
            _usage(preparation.request_record.request, claim),
            recorded_at=NOW + timedelta(seconds=3),
        )


def test_response_and_failure_cannot_predate_invocation_admission(tmp_path: Path) -> None:
    journal = SQLiteDecisionAttemptJournal(tmp_path / "artifacts")
    registered = journal.register(_request(), registered_at=NOW)
    preparation = journal.record_route(registered, _route(), recorded_at=NOW)
    assert isinstance(preparation, DecisionPreparation)
    acquired = journal.acquire(preparation, acquired_at=NOW + timedelta(seconds=1))
    assert isinstance(acquired, DecisionAttemptClaim)
    claim = journal.begin_invoke(
        preparation,
        acquired,
        invoked_at=NOW + timedelta(seconds=3),
    )
    assert isinstance(claim, DecisionAttemptClaim)
    response = _response(preparation, claim, completed_at=NOW + timedelta(seconds=2))

    with pytest.raises(DecisionIdentityConflict, match="precedes invocation"):
        journal.succeed(
            preparation,
            claim,
            response,
            _usage(preparation.request_record.request, claim),
            recorded_at=NOW + timedelta(seconds=4),
        )

    failure = DecisionFailure(
        preparation.request_record.request.request_id,
        preparation.request_record.request.request_digest,
        DecisionFailureKind.ADAPTER,
        "retrograde_failure",
        False,
        NOW + timedelta(seconds=2),
        preparation.route.route_id,
        claim.attempt_record.attempt.attempt_id,
    )
    with pytest.raises(ValueError, match="failed_at cannot precede"):
        journal.fail(
            preparation.request_record,
            failure,
            preparation=preparation,
            claim=claim,
            usage=None,
            recorded_at=NOW + timedelta(seconds=4),
        )


def test_stale_claim_cannot_commit_and_does_not_clear_the_active_attempt(tmp_path: Path) -> None:
    journal, preparation, claim = _claimed_journal(tmp_path)
    response = _response(preparation, claim, completed_at=NOW + timedelta(seconds=2))
    usage = _usage(preparation.request_record.request, claim)
    stale = replace(claim, claim_token="claim:stale")

    with pytest.raises(DecisionJournalError, match="stale or fenced"):
        journal.succeed(
            preparation,
            stale,
            response,
            usage,
            recorded_at=NOW + timedelta(seconds=3),
        )

    with pytest.raises(DecisionAttemptInProgress):
        journal.acquire(preparation, acquired_at=NOW + timedelta(seconds=4))


def test_attempted_failure_and_usage_reconstruct_exactly_after_restart(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    journal, preparation, claim = _claimed_journal(tmp_path, root=root)
    request = preparation.request_record.request
    usage = _usage(request, claim)
    failure = DecisionFailure(
        request.request_id,
        request.request_digest,
        DecisionFailureKind.ADAPTER,
        "adapter_failed",
        False,
        NOW + timedelta(seconds=2),
        preparation.route.route_id,
        claim.attempt_record.attempt.attempt_id,
        "RuntimeError",
    )
    terminal = journal.fail(
        preparation.request_record,
        failure,
        preparation=preparation,
        claim=claim,
        usage=usage,
        recorded_at=NOW + timedelta(seconds=3),
    )
    journal.close()

    with SQLiteDecisionAttemptJournal(root) as reopened:
        assert reopened.acquire(preparation, acquired_at=NOW + timedelta(minutes=1)) == terminal
        assert (
            reopened.fail(
                preparation.request_record,
                failure,
                preparation=preparation,
                claim=claim,
                usage=usage,
                recorded_at=NOW + timedelta(minutes=1),
            )
            == terminal
        )


def test_pre_route_rejection_is_terminal_and_cannot_be_replaced(tmp_path: Path) -> None:
    journal = SQLiteDecisionAttemptJournal(tmp_path / "artifacts")
    request = _request()
    registered = journal.register(request, registered_at=NOW)
    failure = DecisionFailure(
        request.request_id,
        request.request_digest,
        DecisionFailureKind.ADMISSION,
        "no_route",
        False,
        NOW,
    )
    terminal = journal.reject(registered, failure, recorded_at=NOW)

    assert isinstance(terminal, DecisionFailureRecord)
    assert journal.reject(registered, failure, recorded_at=NOW) == terminal
    with pytest.raises(DecisionIdentityConflict, match="different terminal failure"):
        journal.reject(
            registered,
            replace(failure, code="different_rejection"),
            recorded_at=NOW,
        )
    assert journal.record_route(registered, _route(), recorded_at=NOW) == terminal


def test_terminal_success_cannot_be_rewritten_with_another_result_or_failure(
    tmp_path: Path,
) -> None:
    journal, preparation, claim = _claimed_journal(tmp_path)
    request = preparation.request_record.request
    response = _response(preparation, claim, completed_at=NOW + timedelta(seconds=2))
    usage = _usage(request, claim)
    terminal = journal.succeed(
        preparation,
        claim,
        response,
        usage,
        recorded_at=NOW + timedelta(seconds=3),
    )

    with pytest.raises(DecisionIdentityConflict, match="different terminal"):
        journal.succeed(
            preparation,
            claim,
            replace(response, completed_at=NOW + timedelta(seconds=4)),
            usage,
            recorded_at=NOW + timedelta(seconds=5),
        )
    failure = DecisionFailure(
        request.request_id,
        request.request_digest,
        DecisionFailureKind.ADAPTER,
        "late_failure",
        False,
        NOW + timedelta(seconds=4),
        preparation.route.route_id,
        claim.attempt_record.attempt.attempt_id,
    )
    with pytest.raises(DecisionIdentityConflict, match="successful decision"):
        journal.fail(
            preparation.request_record,
            failure,
            preparation=preparation,
            claim=claim,
            usage=None,
            recorded_at=NOW + timedelta(seconds=5),
        )
    assert journal.acquire(preparation, acquired_at=NOW + timedelta(minutes=1)) == terminal


def test_terminal_attempt_failure_rejects_different_failure_content(tmp_path: Path) -> None:
    journal, preparation, claim = _claimed_journal(tmp_path)
    request = preparation.request_record.request
    failure = DecisionFailure(
        request.request_id,
        request.request_digest,
        DecisionFailureKind.ADAPTER,
        "adapter_failed",
        False,
        NOW + timedelta(seconds=2),
        preparation.route.route_id,
        claim.attempt_record.attempt.attempt_id,
    )
    terminal = journal.fail(
        preparation.request_record,
        failure,
        preparation=preparation,
        claim=claim,
        usage=None,
        recorded_at=NOW + timedelta(seconds=3),
    )

    assert isinstance(terminal, DecisionFailureRecord)
    with pytest.raises(DecisionIdentityConflict, match="different terminal failure"):
        journal.fail(
            preparation.request_record,
            replace(failure, code="another_failure"),
            preparation=preparation,
            claim=claim,
            usage=None,
            recorded_at=NOW + timedelta(seconds=4),
        )


def test_timestamps_are_monotonic_before_durable_state_changes(tmp_path: Path) -> None:
    journal = SQLiteDecisionAttemptJournal(tmp_path / "artifacts")
    request = _request()
    with pytest.raises(ValueError, match="cannot precede"):
        journal.register(request, registered_at=NOW - timedelta(seconds=1))

    registered = journal.register(request, registered_at=NOW)
    with pytest.raises(ValueError, match="cannot precede"):
        journal.record_route(
            registered,
            _route(selected_at=NOW),
            recorded_at=NOW - timedelta(seconds=1),
        )


def test_future_schema_and_corrupt_artifact_metadata_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    journal = SQLiteDecisionAttemptJournal(root)
    request = _request()
    journal.register(request, registered_at=NOW)
    database = journal.database_path
    journal.close()

    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "update kernel_artifacts set media_type = 'text/plain' where digest = ?",
            (request.request_digest,),
        )
    with (
        SQLiteDecisionAttemptJournal(root) as reopened,
        pytest.raises(DecisionJournalError, match="incompatible metadata"),
    ):
        reopened.register(request, registered_at=NOW)

    future_root = tmp_path / "future"
    future = SQLiteDecisionAttemptJournal(future_root)
    future_database = future.database_path
    future.close()
    with closing(sqlite3.connect(future_database)) as connection, connection:
        connection.execute(
            "insert into decision_attempt_journal_schema_migrations(version, applied_at) "
            "values (99, '2026-07-12T14:00:00+00:00')"
        )
    with pytest.raises(DecisionJournalError, match="newer than supported"):
        SQLiteDecisionAttemptJournal(future_root)


def test_schema_initialization_is_idempotent_under_concurrent_open(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    # Kernel schema ownership is separate; exercise this adapter's migration race
    # after the shared kernel database exists.
    ArtifactStore(root)

    def initialize(_: int) -> Path:
        with SQLiteDecisionAttemptJournal(root) as journal:
            return journal.database_path

    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = tuple(pool.map(initialize, range(4)))

    assert len(set(paths)) == 1
    with closing(sqlite3.connect(paths[0])) as connection:
        versions = connection.execute(
            "select version from decision_attempt_journal_schema_migrations order by version"
        ).fetchall()
        table = connection.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'decision_attempt_journal'"
        ).fetchone()
    assert versions == [(1,)]
    assert table == (1,)


def _claimed_journal(
    tmp_path: Path,
    *,
    root: Path | None = None,
) -> tuple[SQLiteDecisionAttemptJournal, DecisionPreparation, DecisionAttemptClaim]:
    journal = SQLiteDecisionAttemptJournal(root or tmp_path / "artifacts")
    registered = journal.register(_request(), registered_at=NOW)
    preparation = journal.record_route(registered, _route(), recorded_at=NOW)
    assert isinstance(preparation, DecisionPreparation)
    claim = journal.acquire(preparation, acquired_at=NOW + timedelta(seconds=1))
    assert isinstance(claim, DecisionAttemptClaim)
    admitted = journal.begin_invoke(
        preparation,
        claim,
        invoked_at=NOW + timedelta(seconds=1),
    )
    assert isinstance(admitted, DecisionAttemptClaim)
    return journal, preparation, admitted


def _request() -> RequestDecision:
    return RequestDecision(
        DecisionRequirements(
            "decision:1",
            "node:planner",
            DecisionCapability.REASON,
            DecisionClassification.PRIVATE,
            DecisionLocality.LOCAL_ONLY,
            DecisionBudget(100, 20, 1_000, 100),
            12,
            True,
            NOW,
        ),
        "run:1",
        "run:1",
        "event:context-recorded",
        f"sha256:{'1' * 64}",
        "inspect project status",
        '{"status":"ready"}',
        ("event:1",),
        (DecisionAffordance("inspect", (DecisionArgumentSpec("path"),)),),
    )


def _route(*, selected_at: datetime = NOW) -> DecisionRoute:
    return DecisionRoute(
        "reason-local",
        "recorded",
        "reason-v1",
        DecisionCapability.REASON,
        True,
        True,
        selected_at,
    )


def _output() -> dict[str, JsonValue]:
    return {
        "proposal_id": "proposal:1",
        "context_frame_id": f"sha256:{'1' * 64}",
        "affordance": "inspect",
        "arguments": ({"name": "path", "value": "README.md"},),
        "rationale": "inspect the cited repository evidence",
        "evidence_event_ids": ("event:1",),
    }


def _proposal(request: RequestDecision) -> DecisionProposal:
    return DecisionProposal(
        "proposal:1",
        request.context_frame_id,
        "inspect",
        (DecisionArgument("path", "README.md"),),
        "inspect the cited repository evidence",
        ("event:1",),
    )


def _response(
    preparation: DecisionPreparation,
    claim: DecisionAttemptClaim,
    *,
    completed_at: datetime,
) -> DecisionResponse:
    request = preparation.request_record.request
    return DecisionResponse(
        request.request_id,
        request.request_digest,
        preparation.route.route_id,
        claim.attempt_record.attempt.attempt_id,
        _proposal(request),
        completed_at,
    )


def _usage(request: RequestDecision, claim: DecisionAttemptClaim) -> DecisionUsage:
    return DecisionUsage(
        request.request_id,
        claim.attempt_record.attempt.attempt_id,
        8,
        4,
        10,
        2,
        True,
    )
