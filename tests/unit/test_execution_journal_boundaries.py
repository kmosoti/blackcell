import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from blackcell.adapters.persistence.sqlite import SQLiteExecutionJournal
from blackcell.features.execute_affordance import (
    AdapterOutcome,
    AffordanceArgument,
    AffordanceArgumentSpec,
    AffordanceDefinition,
    AffordanceInvocation,
    ExecutionClaim,
    ExecutionIdentityConflict,
    ExecutionJournalIntegrityError,
    ExecutionJournalStatus,
    ExecutionOperation,
    ExecutionPreparation,
    ExecutionRecoveryAuthorization,
    ExecutionRecoveryError,
    ExecutionResult,
    ExecutionStatus,
    ManualAffordanceRecovery,
    SideEffectClass,
    StaleExecutionClaim,
    UncertainExecutionError,
)

NOW = datetime(2026, 7, 11, 8, tzinfo=UTC)


def test_prepared_claim_reconstructs_after_restart_and_failed_result_is_terminal(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    preparation = _preparation("restart")
    with SQLiteExecutionJournal(root) as journal:
        claim = journal.acquire(preparation, acquired_at=NOW)
        assert isinstance(claim, ExecutionClaim)

    with SQLiteExecutionJournal(root) as reopened:
        entry = reopened.list_entries()[0]
        assert entry.status is ExecutionJournalStatus.PREPARED
        assert entry.active_claim == claim
        assert reopened.get(preparation.invocation.idempotency_key) is None

        failed = reopened.complete(
            claim,
            _result(preparation, status=ExecutionStatus.FAILED),
            recorded_at=NOW + timedelta(minutes=1),
        )

    with SQLiteExecutionJournal(root) as final:
        assert final.get(preparation.invocation.idempotency_key) == failed
        assert final.get_by_invocation(preparation.invocation.invocation_id) == failed
        assert final.get_by_authorization(preparation.authorization_decision_id) == failed
        assert (
            final.acquire(
                preparation,
                acquired_at=NOW + timedelta(minutes=2),
            )
            == failed
        )
        assert (
            final.list_entries(
                status=ExecutionJournalStatus.FAILED,
                limit=1,
            )[0].current_result
            == failed
        )
        assert final.list_entries(after_position=1) == ()


def test_mixed_identity_collision_across_entries_is_rejected_atomically(
    tmp_path: Path,
) -> None:
    journal = SQLiteExecutionJournal(tmp_path / "artifacts")
    first = _preparation("first")
    second = _preparation("second")
    first_claim = journal.acquire(first, acquired_at=NOW)
    second_claim = journal.acquire(second, acquired_at=NOW)
    assert isinstance(first_claim, ExecutionClaim)
    assert isinstance(second_claim, ExecutionClaim)
    mixed = _preparation(
        "mixed",
        invocation_id=second.invocation.invocation_id,
        idempotency_key=first.invocation.idempotency_key,
    )

    with pytest.raises(ExecutionIdentityConflict, match="different execution journal entries"):
        journal.acquire(mixed, acquired_at=NOW)

    entries = journal.list_entries()
    assert len(entries) == 2
    assert tuple(entry.active_claim for entry in entries) == (first_claim, second_claim)


def test_unknown_outcome_can_be_reconciled_more_than_once_with_monotonic_history(
    tmp_path: Path,
) -> None:
    journal = SQLiteExecutionJournal(tmp_path / "artifacts")
    preparation = _preparation("uncertain")
    first = journal.acquire(preparation, acquired_at=NOW)
    assert isinstance(first, ExecutionClaim)
    first_unknown = journal.complete(
        first,
        _result(preparation, status=ExecutionStatus.UNKNOWN),
        recorded_at=NOW,
    )

    second = journal.acquire(preparation, acquired_at=NOW + timedelta(minutes=1))
    assert isinstance(second, ExecutionClaim)
    assert second.operation is ExecutionOperation.RECONCILE
    assert second.fencing_revision == 2
    assert second.previous == first_unknown
    second_unknown = journal.complete(
        second,
        _result(
            preparation,
            status=ExecutionStatus.UNKNOWN,
            reconciled=True,
            completed_at=NOW + timedelta(minutes=1),
        ),
        recorded_at=NOW + timedelta(minutes=1),
    )

    third = journal.acquire(preparation, acquired_at=NOW + timedelta(minutes=2))
    assert isinstance(third, ExecutionClaim)
    assert third.operation is ExecutionOperation.RECONCILE
    assert third.fencing_revision == 3
    assert third.previous == second_unknown
    terminal = journal.complete(
        third,
        _result(
            preparation,
            reconciled=True,
            completed_at=NOW + timedelta(minutes=2),
        ),
        recorded_at=NOW + timedelta(minutes=2),
    )

    assert tuple(
        (result.status, result.reconciled)
        for result in journal.list_results(preparation.invocation.idempotency_key)
    ) == (
        (ExecutionStatus.UNKNOWN, False),
        (ExecutionStatus.UNKNOWN, True),
        (ExecutionStatus.SUCCEEDED, True),
    )
    assert journal.list_entries()[0].current_result == terminal


def test_repeated_manual_recovery_is_audited_and_each_revision_fences_older_claims(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    journal = SQLiteExecutionJournal(root)
    preparation = _preparation("fenced")
    original = journal.acquire(preparation, acquired_at=NOW)
    assert isinstance(original, ExecutionClaim)

    second = journal.recover(
        _recovery_authorization(original, NOW + timedelta(minutes=1)),
        recovered_at=NOW + timedelta(minutes=1),
    ).claim
    third = journal.recover(
        _recovery_authorization(second, NOW + timedelta(minutes=2)),
        recovered_at=NOW + timedelta(minutes=2),
    ).claim
    assert (second.fencing_revision, third.fencing_revision) == (2, 3)

    terminal = journal.complete(
        third,
        _result(
            preparation,
            reconciled=True,
            completed_at=NOW + timedelta(minutes=2),
        ),
        recorded_at=NOW + timedelta(minutes=2),
    )
    for stale in (original, second):
        with pytest.raises(StaleExecutionClaim, match="superseded"):
            journal.complete(
                stale,
                _result(
                    preparation,
                    reconciled=stale.operation is ExecutionOperation.RECONCILE,
                    completed_at=NOW + timedelta(minutes=2),
                ),
                recorded_at=NOW + timedelta(minutes=2),
            )
    with pytest.raises(ExecutionRecoveryError, match="terminal"):
        journal.recover(
            _recovery_authorization(third, NOW + timedelta(minutes=3)),
            recovered_at=NOW + timedelta(minutes=3),
        )

    with closing(sqlite3.connect(journal.database_path)) as connection, connection:
        recoveries = connection.execute(
            """
            select expected_fencing_revision, replacement_fencing_revision,
                   original_worker_stopped
            from execution_journal_recoveries
            order by recovery_position
            """
        ).fetchall()
    assert recoveries == [(1, 2, 1), (2, 3, 1)]
    assert journal.get(preparation.invocation.idempotency_key) == terminal


def test_recovery_rejects_missing_execution_and_unknown_without_active_claim(
    tmp_path: Path,
) -> None:
    journal = SQLiteExecutionJournal(tmp_path / "artifacts")
    missing = ExecutionRecoveryAuthorization(
        execution_identity_digest="sha256:missing",
        expected_claim_token="claim:missing",
        expected_fencing_revision=1,
        authorized_by="operator:test",
        reason="the fixture worker is confirmed stopped",
        authorized_at=NOW,
        original_worker_stopped=True,
    )
    with pytest.raises(ExecutionRecoveryError, match="no prepared execution"):
        journal.recover(missing, recovered_at=NOW)

    preparation = _preparation("inactive")
    claim = journal.acquire(preparation, acquired_at=NOW)
    assert isinstance(claim, ExecutionClaim)
    journal.complete(
        claim,
        _result(preparation, status=ExecutionStatus.UNKNOWN),
        recorded_at=NOW,
    )
    with pytest.raises(ExecutionRecoveryError, match="exact active execution claim"):
        journal.recover(
            _recovery_authorization(claim, NOW + timedelta(minutes=1)),
            recovered_at=NOW + timedelta(minutes=1),
        )


def test_uncertain_manual_reconciliation_persists_unknown_for_another_attempt(
    tmp_path: Path,
) -> None:
    journal = SQLiteExecutionJournal(tmp_path / "artifacts")
    preparation = _preparation("manual-unknown")
    original = journal.acquire(preparation, acquired_at=NOW)
    assert isinstance(original, ExecutionClaim)
    moments = iter(
        (
            NOW + timedelta(minutes=1),
            NOW + timedelta(minutes=2),
            NOW + timedelta(minutes=3),
        )
    )
    adapter = _UncertainReconciler()

    result = ManualAffordanceRecovery(
        {"fixture": adapter},
        journal,
        clock=lambda: next(moments),
    ).recover(_recovery_authorization(original, NOW + timedelta(minutes=1)))

    assert result.status is ExecutionStatus.UNKNOWN
    assert result.reconciled
    assert adapter.previous is None
    retry = journal.acquire(preparation, acquired_at=NOW + timedelta(minutes=4))
    assert isinstance(retry, ExecutionClaim)
    assert retry.operation is ExecutionOperation.RECONCILE
    assert retry.fencing_revision == 3
    assert retry.previous == result


@pytest.mark.parametrize(
    ("adapter_id", "contract_version", "error", "message"),
    (
        (None, None, LookupError, "not registered"),
        ("other", "fixture/v1", ValueError, "identity"),
        ("fixture", "fixture/v2", ValueError, "contract version"),
    ),
)
def test_recovery_configuration_failure_leaves_a_fenced_recoverable_claim(
    tmp_path: Path,
    adapter_id: str | None,
    contract_version: str | None,
    error: type[Exception],
    message: str,
) -> None:
    journal = SQLiteExecutionJournal(tmp_path / "artifacts")
    preparation = _preparation("configuration")
    original = journal.acquire(preparation, acquired_at=NOW)
    assert isinstance(original, ExecutionClaim)
    registry = (
        {}
        if adapter_id is None or contract_version is None
        else {
            "fixture": _Reconciler(
                adapter_id=adapter_id,
                contract_version=contract_version,
            )
        }
    )

    with pytest.raises(error, match=message):
        ManualAffordanceRecovery(
            registry,
            journal,
            clock=lambda: NOW + timedelta(minutes=1),
        ).recover(_recovery_authorization(original, NOW + timedelta(minutes=1)))

    active = journal.list_entries()[0].active_claim
    assert active is not None
    assert active.operation is ExecutionOperation.RECONCILE
    assert active.fencing_revision == original.fencing_revision + 1


def test_naive_recovery_clock_is_rejected_before_the_claim_is_fenced(tmp_path: Path) -> None:
    journal = SQLiteExecutionJournal(tmp_path / "artifacts")
    preparation = _preparation("naive-clock")
    original = journal.acquire(preparation, acquired_at=NOW)
    assert isinstance(original, ExecutionClaim)

    with pytest.raises(ValueError, match="timezone-aware"):
        ManualAffordanceRecovery(
            {"fixture": _Reconciler()},
            journal,
            clock=lambda: datetime(2026, 7, 11, 9),
        ).recover(_recovery_authorization(original, NOW + timedelta(minutes=1)))

    assert journal.list_entries()[0].active_claim == original


def test_retrying_a_completed_claim_with_a_different_result_is_rejected(
    tmp_path: Path,
) -> None:
    journal = SQLiteExecutionJournal(tmp_path / "artifacts")
    preparation = _preparation("completion-collision")
    claim = journal.acquire(preparation, acquired_at=NOW)
    assert isinstance(claim, ExecutionClaim)
    succeeded = journal.complete(
        claim,
        _result(preparation),
        recorded_at=NOW,
    )

    with pytest.raises(ExecutionIdentityConflict, match="different result"):
        journal.complete(
            claim,
            _result(
                preparation,
                status=ExecutionStatus.FAILED,
                completed_at=NOW + timedelta(minutes=1),
            ),
            recorded_at=NOW + timedelta(minutes=1),
        )

    assert journal.get(preparation.invocation.idempotency_key) == succeeded


@pytest.mark.parametrize(
    ("column", "value", "message"),
    (
        ("run_id", " ", "stored execution binding is invalid"),
        (
            "execution_identity_digest",
            "sha256:forged",
            "stored execution identity does not match",
        ),
    ),
)
def test_invalid_persisted_binding_is_detected_on_restart(
    tmp_path: Path,
    column: str,
    value: str,
    message: str,
) -> None:
    root = tmp_path / "artifacts"
    journal = SQLiteExecutionJournal(root)
    preparation = _preparation("corrupt-binding")
    claim = journal.acquire(preparation, acquired_at=NOW)
    assert isinstance(claim, ExecutionClaim)
    database_path = journal.database_path
    journal.close()
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute(f"update execution_journal set {column} = ?", (value,))

    with (
        SQLiteExecutionJournal(root) as reopened,
        pytest.raises(ExecutionJournalIntegrityError, match=message),
    ):
        reopened.list_entries()


def test_corrupt_terminal_status_and_artifact_metadata_are_detected(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    journal = SQLiteExecutionJournal(root)
    preparation = _preparation("corrupt-result")
    claim = journal.acquire(preparation, acquired_at=NOW)
    assert isinstance(claim, ExecutionClaim)
    terminal = journal.complete(claim, _result(preparation), recorded_at=NOW)
    with closing(sqlite3.connect(journal.database_path)) as connection, connection:
        connection.execute(
            "update execution_journal set status = 'failed' where journal_position = ?",
            (claim.journal_position,),
        )
    with pytest.raises(ExecutionJournalIntegrityError, match="status does not match"):
        journal.get(preparation.invocation.idempotency_key)

    metadata_root = tmp_path / "metadata-artifacts"
    metadata_journal = SQLiteExecutionJournal(metadata_root)
    metadata_preparation = _preparation("corrupt-metadata")
    metadata_claim = metadata_journal.acquire(metadata_preparation, acquired_at=NOW)
    assert isinstance(metadata_claim, ExecutionClaim)
    with (
        closing(sqlite3.connect(metadata_journal.database_path)) as connection,
        connection,
    ):
        connection.execute(
            "update kernel_artifacts set media_type = 'text/plain' where digest = ?",
            (metadata_preparation.preparation_id,),
        )
    with pytest.raises(ExecutionJournalIntegrityError, match="incompatible metadata"):
        metadata_journal.get_preparation(metadata_preparation.binding.execution_identity_digest)
    assert terminal.status is ExecutionStatus.SUCCEEDED


class _Reconciler:
    def __init__(
        self,
        *,
        adapter_id: str = "fixture",
        contract_version: str = "fixture/v1",
    ) -> None:
        self.adapter_id = adapter_id
        self.contract_version = contract_version

    def execute(self, invocation, definition):
        raise AssertionError("manual recovery must not execute an affordance")

    def reconcile(self, invocation, definition, previous):
        return AdapterOutcome(True, "sha256:reconciled", NOW + timedelta(minutes=2))


class _UncertainReconciler(_Reconciler):
    def __init__(self) -> None:
        super().__init__()
        self.previous: ExecutionResult | None = None

    def reconcile(self, invocation, definition, previous):
        self.previous = previous
        raise UncertainExecutionError("the external outcome cannot be determined")


def _preparation(
    label: str,
    *,
    invocation_id: str | None = None,
    idempotency_key: str | None = None,
) -> ExecutionPreparation:
    invocation = AffordanceInvocation(
        invocation_id=invocation_id or f"invocation:{label}",
        proposal_id=f"proposal:{label}",
        affordance="inspect",
        arguments=(AffordanceArgument("path", f"{label}.txt"),),
        idempotency_key=idempotency_key or f"key:{label}",
        requested_at=NOW,
    )
    definition = AffordanceDefinition(
        name="inspect",
        adapter_id="fixture",
        side_effect_class=SideEffectClass.READ_ONLY,
        timeout_seconds=10.0,
        arguments=(AffordanceArgumentSpec("path"),),
    )
    return ExecutionPreparation(
        run_id=f"run:{label}",
        invocation=invocation,
        definition=definition,
        authorization_decision_id=f"authorization:{label}",
        authorized_action_digest=invocation.action_digest,
        adapter_contract_version="fixture/v1",
    )


def _recovery_authorization(
    claim: ExecutionClaim,
    authorized_at: datetime,
) -> ExecutionRecoveryAuthorization:
    return ExecutionRecoveryAuthorization(
        execution_identity_digest=claim.binding.execution_identity_digest,
        expected_claim_token=claim.claim_token,
        expected_fencing_revision=claim.fencing_revision,
        authorized_by="operator:test",
        reason="the fixture worker is confirmed stopped",
        authorized_at=authorized_at,
        original_worker_stopped=True,
    )


def _result(
    preparation: ExecutionPreparation,
    *,
    status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    reconciled: bool = False,
    completed_at: datetime = NOW,
) -> ExecutionResult:
    unknown = status is ExecutionStatus.UNKNOWN
    return ExecutionResult(
        invocation_id=preparation.invocation.invocation_id,
        proposal_id=preparation.invocation.proposal_id,
        authorization_decision_id=preparation.authorization_decision_id,
        affordance=preparation.invocation.affordance,
        adapter_id=preparation.definition.adapter_id,
        idempotency_key=preparation.invocation.idempotency_key,
        authorized_action_digest=preparation.authorized_action_digest,
        execution_identity_digest=preparation.binding.execution_identity_digest,
        status=status,
        started_at=preparation.invocation.requested_at,
        completed_at=completed_at,
        output_digest=None if unknown else f"sha256:{status.value}",
        observed_effects=(),
        error_code="outcome_unknown" if unknown else None,
        reconciled=reconciled,
    )
