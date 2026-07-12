import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from blackcell.adapters.persistence.sqlite import SQLiteExecutionJournal
from blackcell.features.execute_affordance import (
    AdapterOutcome,
    AffordanceArgument,
    AffordanceArgumentSpec,
    AffordanceDefinition,
    AffordanceExecutionHandler,
    AffordanceInvocation,
    AuthorizationBindingConflict,
    ExecutionBinding,
    ExecutionClaim,
    ExecutionIdentityConflict,
    ExecutionInProgress,
    ExecutionJournalIntegrityError,
    ExecutionJournalSchemaError,
    ExecutionOperation,
    ExecutionPreparation,
    ExecutionRecoveryAuthorization,
    ExecutionRecoveryError,
    ExecutionResult,
    ExecutionStatus,
    IdempotencyKeyConflict,
    ManualAffordanceRecovery,
    SideEffectClass,
    StaleExecutionClaim,
    deserialize_execution_result,
    serialize_execution_result,
)
from blackcell.kernel import ArtifactStore

NOW = datetime(2026, 7, 11, 8, tzinfo=UTC)


def test_entry_lookup_by_invocation_reconstructs_verified_terminal_evidence(
    tmp_path: Path,
) -> None:
    journal = SQLiteExecutionJournal(tmp_path / "artifacts")
    preparation = _preparation()
    claim = journal.acquire(preparation, acquired_at=NOW)
    assert isinstance(claim, ExecutionClaim)
    result = journal.complete(claim, _result(preparation.binding), recorded_at=NOW)

    entry = journal.get_entry_by_invocation(preparation.invocation.invocation_id)

    assert entry is not None
    assert entry == journal.list_entries()[0]
    assert entry.binding == preparation.binding
    assert entry.current_result == result


def test_entry_lookup_by_invocation_returns_none_when_absent(tmp_path: Path) -> None:
    journal = SQLiteExecutionJournal(tmp_path / "artifacts")

    assert journal.get_entry_by_invocation("invocation:absent") is None


@pytest.mark.parametrize("invocation_id", ("", " "))
def test_entry_lookup_by_invocation_rejects_blank_identity(
    tmp_path: Path,
    invocation_id: str,
) -> None:
    journal = SQLiteExecutionJournal(tmp_path / "artifacts")

    with pytest.raises(ValueError, match="invocation_id must not be empty"):
        journal.get_entry_by_invocation(invocation_id)


def test_entry_lookup_by_invocation_rejects_corrupt_row(tmp_path: Path) -> None:
    journal = SQLiteExecutionJournal(tmp_path / "artifacts")
    preparation = _preparation()
    claim = journal.acquire(preparation, acquired_at=NOW)
    assert isinstance(claim, ExecutionClaim)
    with sqlite3.connect(journal.database_path) as connection:
        connection.execute(
            "update execution_journal set run_id = ' ' where invocation_id = ?",
            (preparation.invocation.invocation_id,),
        )

    with pytest.raises(ExecutionJournalIntegrityError, match="stored execution binding is invalid"):
        journal.get_entry_by_invocation(preparation.invocation.invocation_id)


def test_terminal_result_round_trips_as_the_canonical_artifact_across_restart(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    preparation = _preparation()
    binding = preparation.binding
    with SQLiteExecutionJournal(root) as journal:
        claim = journal.acquire(preparation, acquired_at=NOW)
        assert isinstance(claim, ExecutionClaim)
        stored = journal.complete(claim, _result(binding), recorded_at=NOW)
        assert journal.complete(claim, stored, recorded_at=NOW) == stored
        assert journal.get(binding.idempotency_key) == stored
        assert journal.get_by_invocation(binding.invocation_id) == stored
        assert journal.get_by_authorization(binding.authorization_decision_id) == stored
        assert len(journal) == 1

    with SQLiteExecutionJournal(root) as reopened:
        assert reopened.get_preparation(binding.execution_identity_digest) == preparation
        assert reopened.acquire(preparation, acquired_at=NOW + timedelta(minutes=1)) == stored
        entries = reopened.list_entries()

    assert len(entries) == 1
    assert entries[0].binding.run_id == "run:1"
    assert entries[0].current_result == stored
    artifact = ArtifactStore(root)
    assert artifact.get_text(stored.result_id) == serialize_execution_result(stored)
    assert (
        deserialize_execution_result(
            artifact.get_bytes(stored.result_id),
            expected_result_id=stored.result_id,
        )
        == stored
    )


def test_unknown_result_survives_restart_and_is_reconciled_with_history(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    preparation = _preparation()
    binding = preparation.binding
    with SQLiteExecutionJournal(root) as journal:
        claim = journal.acquire(preparation, acquired_at=NOW)
        assert isinstance(claim, ExecutionClaim)
        unknown = journal.complete(
            claim,
            _result(binding, status=ExecutionStatus.UNKNOWN),
            recorded_at=NOW,
        )

    with SQLiteExecutionJournal(root) as reopened:
        reconciliation = reopened.acquire(
            preparation,
            acquired_at=NOW + timedelta(minutes=1),
        )
        assert isinstance(reconciliation, ExecutionClaim)
        assert reconciliation.operation is ExecutionOperation.RECONCILE
        assert reconciliation.previous == unknown
        succeeded = reopened.complete(
            reconciliation,
            _result(binding, reconciled=True),
            recorded_at=NOW + timedelta(minutes=1),
        )

        assert tuple(item.status for item in reopened.list_results("key:1")) == (
            ExecutionStatus.UNKNOWN,
            ExecutionStatus.SUCCEEDED,
        )
        assert reopened.acquire(preparation, acquired_at=NOW + timedelta(minutes=2)) == succeeded


def test_prepared_execution_requires_explicit_recovery_and_fences_old_claim(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    preparation = _preparation()
    binding = preparation.binding
    journal = SQLiteExecutionJournal(root)
    original = journal.acquire(preparation, acquired_at=NOW)
    assert isinstance(original, ExecutionClaim)
    journal.close()

    with SQLiteExecutionJournal(root) as reopened:
        with pytest.raises(ExecutionInProgress):
            reopened.acquire(preparation, acquired_at=NOW + timedelta(minutes=1))

        recovery = reopened.recover(
            _recovery_authorization(original),
            recovered_at=NOW + timedelta(minutes=1),
        )
        recovered = recovery.claim
        assert recovery.preparation == preparation
        assert recovered.operation is ExecutionOperation.RECONCILE
        assert recovered.previous is None
        assert recovered.fencing_revision == original.fencing_revision + 1

        with pytest.raises(StaleExecutionClaim):
            reopened.complete(
                original,
                _result(binding),
                recorded_at=NOW + timedelta(minutes=1),
            )

        terminal = reopened.complete(
            recovered,
            _result(binding, reconciled=True),
            recorded_at=NOW + timedelta(minutes=1),
        )
        assert terminal.reconciled


@pytest.mark.parametrize(
    ("changes", "error"),
    (
        ({"run_id": "run:other"}, IdempotencyKeyConflict),
        ({"adapter_contract_version": "fixture/v2"}, IdempotencyKeyConflict),
        (
            {
                "idempotency_key": "key:other",
                "authorization_decision_id": "authorization:other",
            },
            ExecutionIdentityConflict,
        ),
        (
            {
                "idempotency_key": "key:other",
                "invocation_id": "invocation:other",
            },
            AuthorizationBindingConflict,
        ),
    ),
)
def test_identity_collisions_are_rejected_before_a_second_claim(
    tmp_path: Path,
    changes: dict[str, str],
    error: type[Exception],
) -> None:
    journal = SQLiteExecutionJournal(tmp_path / "artifacts")
    preparation = _preparation()
    candidate = _changed_preparation(preparation, changes)
    original = journal.acquire(preparation, acquired_at=NOW)
    assert isinstance(original, ExecutionClaim)

    with pytest.raises(error):
        journal.acquire(candidate, acquired_at=NOW)

    entry = journal.list_entries()[0]
    assert entry.active_claim == original
    assert len(journal) == 1


def test_concurrent_exact_acquire_grants_only_one_execute_claim(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    database_path = root / "kernel.sqlite3"
    journals = (
        SQLiteExecutionJournal(root, database_path=database_path),
        SQLiteExecutionJournal(root, database_path=database_path),
    )
    preparation = _preparation()
    barrier = Barrier(2)

    def acquire(index: int) -> object:
        barrier.wait()
        try:
            return journals[index].acquire(preparation, acquired_at=NOW)
        except ExecutionInProgress as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(acquire, range(2)))

    claims = tuple(item for item in outcomes if isinstance(item, ExecutionClaim))
    blocked = tuple(item for item in outcomes if isinstance(item, ExecutionInProgress))
    assert len(claims) == len(blocked) == 1
    assert claims[0].operation is ExecutionOperation.EXECUTE
    assert len(journals[0]) == 1


def test_completion_rejects_result_from_another_binding(tmp_path: Path) -> None:
    journal = SQLiteExecutionJournal(tmp_path / "artifacts")
    preparation = _preparation()
    binding = preparation.binding
    claim = journal.acquire(preparation, acquired_at=NOW)
    assert isinstance(claim, ExecutionClaim)
    forged = replace(
        _result(binding),
        invocation_id="invocation:forged",
    )

    with pytest.raises(ExecutionIdentityConflict, match="invocation_id"):
        journal.complete(claim, forged, recorded_at=NOW)

    assert journal.list_entries()[0].active_claim == claim


def test_newer_journal_schema_is_rejected_before_table_repair(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    journal = SQLiteExecutionJournal(root)
    database_path = journal.database_path
    journal.close()
    with sqlite3.connect(database_path) as connection:
        connection.execute("drop table execution_journal_results")
        connection.execute("drop table execution_journal")
        connection.execute(
            """
            insert into execution_journal_schema_migrations(version, applied_at)
            values (2, '2026-07-11T08:00:00+00:00')
            """
        )

    with pytest.raises(ExecutionJournalSchemaError, match="newer than supported"):
        SQLiteExecutionJournal(root)

    with sqlite3.connect(database_path) as connection:
        repaired = connection.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'execution_journal'"
        ).fetchone()
    assert repaired is None


def test_manual_restart_recovery_uses_exact_durable_preparation_and_never_reexecutes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    preparation = _preparation()
    adapter = _CrashAfterEffectAdapter(preparation)
    journal = SQLiteExecutionJournal(root)
    handler = AffordanceExecutionHandler(
        {"fixture": adapter},
        journal,
        clock=lambda: NOW,
    )
    authorization = _Authorization(
        preparation.authorization_decision_id,
        preparation.invocation.proposal_id,
        "allow",
        preparation.authorized_action_digest,
        True,
    )

    with pytest.raises(RuntimeError, match="lost completion"):
        handler.handle(
            preparation.invocation,
            preparation.definition,
            authorization,
            run_id=preparation.run_id,
        )
    with pytest.raises(ExecutionInProgress):
        handler.handle(
            preparation.invocation,
            preparation.definition,
            authorization,
            run_id=preparation.run_id,
        )
    assert adapter.execute_calls == 1
    active = journal.list_entries()[0].active_claim
    assert active is not None

    wrong = replace(
        _recovery_authorization(active),
        expected_claim_token="claim:not-current",
    )
    with pytest.raises(ExecutionRecoveryError, match="does not match"):
        journal.recover(wrong, recovered_at=NOW + timedelta(minutes=1))
    assert journal.list_entries()[0].active_claim == active

    journal.close()
    with SQLiteExecutionJournal(root) as reopened:
        result = ManualAffordanceRecovery(
            {"fixture": adapter},
            reopened,
            clock=lambda: NOW + timedelta(minutes=1),
        ).recover(_recovery_authorization(active))
        assert result.status is ExecutionStatus.SUCCEEDED
        assert result.reconciled
        assert reopened.get_preparation(result.execution_identity_digest) == preparation

        with pytest.raises(StaleExecutionClaim):
            reopened.complete(
                active,
                _result(preparation.binding),
                recorded_at=NOW + timedelta(minutes=1),
            )

    assert adapter.execute_calls == 1
    assert adapter.reconcile_calls == 1


def test_execution_journal_rejects_time_regression(tmp_path: Path) -> None:
    journal = SQLiteExecutionJournal(tmp_path / "artifacts")
    preparation = _preparation()
    claim = journal.acquire(preparation, acquired_at=NOW)
    assert isinstance(claim, ExecutionClaim)

    with pytest.raises(ValueError, match="cannot precede"):
        journal.acquire(preparation, acquired_at=NOW - timedelta(seconds=1))
    with pytest.raises(ValueError, match="cannot precede"):
        journal.complete(
            claim,
            _result(preparation.binding),
            recorded_at=NOW - timedelta(seconds=1),
        )
    premature = replace(
        _recovery_authorization(claim),
        authorized_at=NOW - timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="cannot precede"):
        journal.recover(premature, recovered_at=NOW + timedelta(minutes=1))


@dataclass(frozen=True, slots=True)
class _Authorization:
    decision_id: str
    proposal_id: str
    outcome: str
    authorized_action_digest: str
    authorized_read_only: bool


class _CrashAfterEffectAdapter:
    adapter_id = "fixture"
    contract_version = "fixture/v1"

    def __init__(self, expected: ExecutionPreparation) -> None:
        self.expected = expected
        self.execute_calls = 0
        self.reconcile_calls = 0

    def execute(self, invocation, definition):
        self.execute_calls += 1
        assert invocation == self.expected.invocation
        assert definition == self.expected.definition
        raise RuntimeError("lost completion after external effect")

    def reconcile(self, invocation, definition, previous):
        self.reconcile_calls += 1
        assert invocation == self.expected.invocation
        assert definition == self.expected.definition
        assert previous is None
        return AdapterOutcome(
            True,
            "sha256:reconciled",
            NOW + timedelta(minutes=1),
        )


def _preparation() -> ExecutionPreparation:
    invocation = AffordanceInvocation(
        invocation_id="invocation:1",
        proposal_id="proposal:1",
        affordance="inspect",
        arguments=(AffordanceArgument("path", "README.md"),),
        idempotency_key="key:1",
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
        run_id="run:1",
        invocation=invocation,
        definition=definition,
        authorization_decision_id="authorization:1",
        authorized_action_digest=invocation.action_digest,
        adapter_contract_version="fixture/v1",
    )


def _binding() -> ExecutionBinding:
    return _preparation().binding


def _changed_preparation(
    preparation: ExecutionPreparation,
    changes: dict[str, str],
) -> ExecutionPreparation:
    direct = {
        key: value
        for key, value in changes.items()
        if key in {"run_id", "adapter_contract_version"}
    }
    invocation_changes = {
        key: value for key, value in changes.items() if key in {"invocation_id", "idempotency_key"}
    }
    authorization = changes.get(
        "authorization_decision_id",
        preparation.authorization_decision_id,
    )
    return replace(
        preparation,
        **direct,
        invocation=replace(preparation.invocation, **invocation_changes),
        authorization_decision_id=authorization,
    )


def _recovery_authorization(claim: ExecutionClaim) -> ExecutionRecoveryAuthorization:
    return ExecutionRecoveryAuthorization(
        execution_identity_digest=claim.binding.execution_identity_digest,
        expected_claim_token=claim.claim_token,
        expected_fencing_revision=claim.fencing_revision,
        authorized_by="operator:test",
        reason="fixture worker stopped before recording an outcome",
        authorized_at=NOW + timedelta(minutes=1),
        original_worker_stopped=True,
    )


def _result(
    binding: ExecutionBinding,
    *,
    status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    reconciled: bool = False,
) -> ExecutionResult:
    unknown = status is ExecutionStatus.UNKNOWN
    return ExecutionResult(
        invocation_id=binding.invocation_id,
        proposal_id=binding.proposal_id,
        authorization_decision_id=binding.authorization_decision_id,
        affordance=binding.affordance,
        adapter_id=binding.adapter_id,
        idempotency_key=binding.idempotency_key,
        authorized_action_digest=binding.authorized_action_digest,
        execution_identity_digest=binding.execution_identity_digest,
        status=status,
        started_at=NOW,
        completed_at=NOW,
        output_digest=None if unknown else "sha256:output",
        observed_effects=(),
        error_code="outcome_unknown" if unknown else None,
        reconciled=reconciled,
    )
