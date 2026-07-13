from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Self
from uuid import uuid4

from blackcell.features.execute_affordance.errors import (
    AuthorizationBindingConflict,
    ExecutionIdentityConflict,
    ExecutionInProgress,
    ExecutionJournalIntegrityError,
    ExecutionJournalSchemaError,
    ExecutionRecoveryError,
    IdempotencyKeyConflict,
    StaleExecutionClaim,
)
from blackcell.features.execute_affordance.models import (
    EXECUTION_PREPARATION_MEDIA_TYPE,
    EXECUTION_RESULT_MEDIA_TYPE,
    ExecutionBinding,
    ExecutionClaim,
    ExecutionJournalEntry,
    ExecutionJournalStatus,
    ExecutionOperation,
    ExecutionPreparation,
    ExecutionRecovery,
    ExecutionRecoveryAuthorization,
    ExecutionResult,
    deserialize_execution_preparation,
    deserialize_execution_result,
    serialize_execution_preparation,
    serialize_execution_result,
)
from blackcell.kernel import ArtifactIntegrityError, ArtifactNotFoundError, ArtifactStore
from blackcell.kernel.database import connect

_JOURNAL_SCHEMA_VERSION = 1
_MIGRATION_TABLE = "execution_journal_schema_migrations"
_MIGRATION_SCHEMA = f"""
create table if not exists {_MIGRATION_TABLE} (
    version integer primary key,
    applied_at text not null
)
"""

_JOURNAL_SCHEMA = """
create table if not exists execution_journal (
    journal_position integer primary key autoincrement,
    schema_version text not null check(length(schema_version) > 0),
    execution_identity_digest text not null unique check(length(execution_identity_digest) > 0),
    run_id text not null check(length(run_id) > 0),
    invocation_id text not null unique check(length(invocation_id) > 0),
    proposal_id text not null check(length(proposal_id) > 0),
    authorization_decision_id text not null unique check(length(authorization_decision_id) > 0),
    affordance text not null check(length(affordance) > 0),
    adapter_id text not null check(length(adapter_id) > 0),
    idempotency_key text not null unique check(length(idempotency_key) > 0),
    authorized_action_digest text not null check(length(authorized_action_digest) > 0),
    adapter_contract_version text not null check(length(adapter_contract_version) > 0),
    invocation_digest text not null check(length(invocation_digest) > 0),
    definition_digest text not null check(length(definition_digest) > 0),
    preparation_id text not null check(length(preparation_id) > 0),
    status text not null check(status in ('prepared', 'unknown', 'succeeded', 'failed')),
    current_result_id text,
    fencing_revision integer not null check(fencing_revision >= 1),
    active_claim_token text,
    active_operation text check(
        active_operation is null or active_operation in ('execute', 'reconcile')
    ),
    claim_acquired_at text,
    created_at text not null,
    updated_at text not null,
    foreign key(current_result_id) references kernel_artifacts(digest),
    foreign key(preparation_id) references kernel_artifacts(digest),
    check((status = 'prepared') = (current_result_id is null)),
    check(
        (active_claim_token is null and active_operation is null and claim_acquired_at is null)
        or
        (active_claim_token is not null and active_operation is not null
         and claim_acquired_at is not null)
    ),
    check(status != 'prepared' or active_claim_token is not null),
    check(active_operation != 'execute' or status = 'prepared'),
    check(active_operation != 'reconcile' or status in ('prepared', 'unknown'))
);

create index if not exists idx_execution_journal_status
    on execution_journal(status, journal_position);
create index if not exists idx_execution_journal_run
    on execution_journal(run_id, journal_position);

create table if not exists execution_journal_results (
    transition_position integer primary key autoincrement,
    journal_position integer not null,
    fencing_revision integer not null check(fencing_revision >= 1),
    operation text not null check(operation in ('execute', 'reconcile')),
    result_id text not null,
    status text not null check(status in ('unknown', 'succeeded', 'failed')),
    reconciled integer not null check(reconciled in (0, 1)),
    recorded_at text not null,
    unique(journal_position, fencing_revision),
    foreign key(journal_position) references execution_journal(journal_position),
    foreign key(result_id) references kernel_artifacts(digest)
);

create index if not exists idx_execution_journal_results_entry
    on execution_journal_results(journal_position, transition_position);

create table if not exists execution_journal_recoveries (
    recovery_position integer primary key autoincrement,
    journal_position integer not null,
    recovery_authorization_id text not null unique,
    execution_identity_digest text not null,
    expected_claim_token text not null,
    expected_fencing_revision integer not null check(expected_fencing_revision >= 1),
    replacement_claim_token text not null,
    replacement_fencing_revision integer not null check(replacement_fencing_revision >= 1),
    authorized_by text not null,
    reason text not null,
    authorized_at text not null,
    recovered_at text not null,
    original_worker_stopped integer not null check(original_worker_stopped = 1),
    foreign key(journal_position) references execution_journal(journal_position)
);
"""

_BINDING_COLUMNS = """
schema_version, execution_identity_digest, run_id, invocation_id, proposal_id,
authorization_decision_id, affordance, adapter_id, idempotency_key,
authorized_action_digest, adapter_contract_version, invocation_digest,
definition_digest, preparation_id
""".strip()
_BINDING_PLACEHOLDERS = ", ".join("?" for _ in range(14))


class SQLiteExecutionJournal:
    """Durable execution claims and canonical result artifacts in the kernel store."""

    def __init__(
        self,
        root: Path | str,
        *,
        database_path: Path | str | None = None,
        artifact_max_total_bytes: int | None = None,
    ) -> None:
        self.root = Path(root)
        self._artifacts = ArtifactStore(
            self.root,
            database_path=database_path,
            max_total_bytes=artifact_max_total_bytes,
        )
        self.database_path = self._artifacts.database_path
        self._closed = False
        self._initialize_schema()

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._closed = True

    def acquire(
        self,
        preparation: ExecutionPreparation,
        *,
        acquired_at: datetime,
    ) -> ExecutionClaim | ExecutionResult:
        self._require_open()
        _require_aware(acquired_at, "acquired_at")
        stored_preparation = self._put_preparation(preparation)
        binding = stored_preparation.binding
        token = _claim_token()
        with connect(self.database_path) as connection:
            connection.execute("begin immediate")
            try:
                row = self._binding_row(connection, binding)
                if row is None:
                    cursor = connection.execute(
                        f"""
                        insert into execution_journal(
                            {_BINDING_COLUMNS}, status, current_result_id,
                            fencing_revision, active_claim_token, active_operation,
                            claim_acquired_at, created_at, updated_at
                        ) values ({_BINDING_PLACEHOLDERS},
                                  'prepared', null, 1, ?, 'execute', ?, ?, ?)
                        """,
                        (
                            *_binding_values(binding),
                            token,
                            acquired_at.isoformat(),
                            acquired_at.isoformat(),
                            acquired_at.isoformat(),
                        ),
                    )
                    position = cursor.lastrowid
                    if position is None:  # pragma: no cover - SQLite invariant
                        raise ExecutionJournalIntegrityError(
                            "SQLite did not assign an execution journal position"
                        )
                    claim = ExecutionClaim(
                        int(position),
                        binding,
                        1,
                        token,
                        ExecutionOperation.EXECUTE,
                        acquired_at,
                    )
                    connection.commit()
                    return claim

                stored_binding = _binding_from_row(row)
                self._validate_binding(stored_binding, binding)
                if self._load_preparation(str(row["preparation_id"])) != stored_preparation:
                    raise ExecutionJournalIntegrityError(
                        "stored execution preparation does not match the exact retry"
                    )
                _require_not_before(
                    acquired_at,
                    datetime.fromisoformat(str(row["updated_at"])),
                    "acquired_at",
                )
                status = ExecutionJournalStatus(str(row["status"]))
                current = self._current_result(row)
                if status in {
                    ExecutionJournalStatus.SUCCEEDED,
                    ExecutionJournalStatus.FAILED,
                }:
                    if current is None:  # pragma: no cover - SQL check and decoder guard
                        raise ExecutionJournalIntegrityError(
                            "terminal execution has no current result"
                        )
                    connection.commit()
                    return current
                if row["active_claim_token"] is not None:
                    raise ExecutionInProgress(
                        f"execution {binding.execution_identity_digest!r} has an active "
                        f"{row['active_operation']} claim"
                    )
                if status is not ExecutionJournalStatus.UNKNOWN or current is None:
                    raise ExecutionJournalIntegrityError(
                        "non-terminal execution is neither actively prepared nor unknown"
                    )
                revision = int(row["fencing_revision"]) + 1
                connection.execute(
                    """
                    update execution_journal
                    set fencing_revision = ?, active_claim_token = ?,
                        active_operation = 'reconcile', claim_acquired_at = ?, updated_at = ?
                    where journal_position = ?
                    """,
                    (
                        revision,
                        token,
                        acquired_at.isoformat(),
                        acquired_at.isoformat(),
                        int(row["journal_position"]),
                    ),
                )
                claim = ExecutionClaim(
                    int(row["journal_position"]),
                    binding,
                    revision,
                    token,
                    ExecutionOperation.RECONCILE,
                    acquired_at,
                    current,
                )
                connection.commit()
                return claim
            except Exception:
                connection.rollback()
                raise

    def recover(
        self,
        authorization: ExecutionRecoveryAuthorization,
        *,
        recovered_at: datetime,
    ) -> ExecutionRecovery:
        self._require_open()
        _require_aware(recovered_at, "recovered_at")
        token = _claim_token()
        with connect(self.database_path) as connection:
            connection.execute("begin immediate")
            try:
                row = connection.execute(
                    """
                    select * from execution_journal
                    where execution_identity_digest = ?
                    """,
                    (authorization.execution_identity_digest,),
                ).fetchone()
                if row is None:
                    raise ExecutionRecoveryError("no prepared execution exists to recover")
                stored_binding = _binding_from_row(row)
                status = ExecutionJournalStatus(str(row["status"]))
                if status in {
                    ExecutionJournalStatus.SUCCEEDED,
                    ExecutionJournalStatus.FAILED,
                }:
                    raise ExecutionRecoveryError("a terminal execution cannot be recovered")
                if row["active_claim_token"] is None:
                    raise ExecutionRecoveryError(
                        "manual recovery requires an exact active execution claim"
                    )
                if (
                    row["active_claim_token"] != authorization.expected_claim_token
                    or int(row["fencing_revision"]) != authorization.expected_fencing_revision
                ):
                    raise ExecutionRecoveryError(
                        "manual recovery authorization does not match the active claim"
                    )
                claim_acquired_at = datetime.fromisoformat(str(row["claim_acquired_at"]))
                updated_at = datetime.fromisoformat(str(row["updated_at"]))
                _require_not_before(
                    authorization.authorized_at,
                    claim_acquired_at,
                    "authorized_at",
                )
                _require_not_before(recovered_at, updated_at, "recovered_at")
                _require_not_before(
                    recovered_at,
                    authorization.authorized_at,
                    "recovered_at",
                )
                preparation = self._load_preparation(str(row["preparation_id"]))
                if preparation.binding != stored_binding:
                    raise ExecutionJournalIntegrityError(
                        "prepared execution artifact does not match its journal binding"
                    )
                previous = self._current_result(row)
                if status is ExecutionJournalStatus.UNKNOWN and previous is None:
                    raise ExecutionJournalIntegrityError("unknown execution has no result artifact")
                if status is ExecutionJournalStatus.PREPARED and previous is not None:
                    raise ExecutionJournalIntegrityError(
                        "prepared execution unexpectedly has a result"
                    )
                revision = int(row["fencing_revision"]) + 1
                connection.execute(
                    """
                    insert into execution_journal_recoveries(
                        journal_position, recovery_authorization_id,
                        execution_identity_digest, expected_claim_token,
                        expected_fencing_revision, replacement_claim_token,
                        replacement_fencing_revision, authorized_by, reason,
                        authorized_at, recovered_at, original_worker_stopped
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        int(row["journal_position"]),
                        authorization.authorization_id,
                        authorization.execution_identity_digest,
                        authorization.expected_claim_token,
                        authorization.expected_fencing_revision,
                        token,
                        revision,
                        authorization.authorized_by,
                        authorization.reason,
                        authorization.authorized_at.isoformat(),
                        recovered_at.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    update execution_journal
                    set fencing_revision = ?, active_claim_token = ?,
                        active_operation = 'reconcile', claim_acquired_at = ?, updated_at = ?
                    where journal_position = ?
                    """,
                    (
                        revision,
                        token,
                        recovered_at.isoformat(),
                        recovered_at.isoformat(),
                        int(row["journal_position"]),
                    ),
                )
                claim = ExecutionClaim(
                    int(row["journal_position"]),
                    stored_binding,
                    revision,
                    token,
                    ExecutionOperation.RECONCILE,
                    recovered_at,
                    previous,
                )
                connection.commit()
                return ExecutionRecovery(preparation, claim, authorization)
            except Exception:
                connection.rollback()
                raise

    def complete(
        self,
        claim: ExecutionClaim,
        result: ExecutionResult,
        *,
        recorded_at: datetime,
    ) -> ExecutionResult:
        self._require_open()
        _require_aware(recorded_at, "recorded_at")
        _validate_result_binding(result, claim.binding)
        expected_reconciled = claim.operation is ExecutionOperation.RECONCILE
        if result.reconciled is not expected_reconciled:
            raise ExecutionIdentityConflict(
                "execution result reconciliation marker does not match its claim"
            )
        data = serialize_execution_result(result).encode("utf-8")
        try:
            artifact = self._artifacts.put_bytes(
                data,
                media_type=EXECUTION_RESULT_MEDIA_TYPE,
                encoding="utf-8",
            )
        except ArtifactIntegrityError as error:
            raise ExecutionJournalIntegrityError(
                f"execution result artifact {result.result_id!r} is corrupt"
            ) from error
        if artifact.digest != result.result_id:  # pragma: no cover - content-address invariant
            raise ExecutionJournalIntegrityError(
                "execution result artifact has a different content identity"
            )

        with connect(self.database_path) as connection:
            connection.execute("begin immediate")
            try:
                row = connection.execute(
                    "select * from execution_journal where journal_position = ?",
                    (claim.journal_position,),
                ).fetchone()
                if row is None:
                    raise StaleExecutionClaim("execution journal entry no longer exists")
                self._validate_binding(_binding_from_row(row), claim.binding)
                _require_not_before(
                    recorded_at,
                    datetime.fromisoformat(str(row["updated_at"])),
                    "recorded_at",
                )
                _require_not_before(recorded_at, claim.acquired_at, "recorded_at")
                _require_not_before(recorded_at, result.completed_at, "recorded_at")
                prior = connection.execute(
                    """
                    select result_id from execution_journal_results
                    where journal_position = ? and fencing_revision = ?
                    """,
                    (claim.journal_position, claim.fencing_revision),
                ).fetchone()
                if prior is not None:
                    if str(prior["result_id"]) != result.result_id:
                        raise ExecutionIdentityConflict(
                            "execution claim completion was retried with a different result"
                        )
                    connection.commit()
                    return self._load_result(result.result_id)
                if (
                    int(row["fencing_revision"]) != claim.fencing_revision
                    or row["active_claim_token"] != claim.claim_token
                    or row["active_operation"] != claim.operation.value
                ):
                    raise StaleExecutionClaim(
                        "execution claim was superseded by another fencing revision"
                    )
                connection.execute(
                    """
                    insert into execution_journal_results(
                        journal_position, fencing_revision, operation, result_id,
                        status, reconciled, recorded_at
                    ) values (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        claim.journal_position,
                        claim.fencing_revision,
                        claim.operation.value,
                        result.result_id,
                        result.status.value,
                        int(result.reconciled),
                        recorded_at.isoformat(),
                    ),
                )
                changed = connection.execute(
                    """
                    update execution_journal
                    set status = ?, current_result_id = ?, active_claim_token = null,
                        active_operation = null, claim_acquired_at = null, updated_at = ?
                    where journal_position = ? and fencing_revision = ?
                      and active_claim_token = ?
                    """,
                    (
                        result.status.value,
                        result.result_id,
                        recorded_at.isoformat(),
                        claim.journal_position,
                        claim.fencing_revision,
                        claim.claim_token,
                    ),
                ).rowcount
                if changed != 1:  # pragma: no cover - guarded in the same write transaction
                    raise StaleExecutionClaim("execution claim was fenced during completion")
                connection.commit()
                return self._load_result(result.result_id)
            except Exception:
                connection.rollback()
                raise

    def get(self, idempotency_key: str) -> ExecutionResult | None:
        return self._get_by("idempotency_key", idempotency_key)

    def get_by_authorization(self, decision_id: str) -> ExecutionResult | None:
        return self._get_by("authorization_decision_id", decision_id)

    def get_by_invocation(self, invocation_id: str) -> ExecutionResult | None:
        return self._get_by("invocation_id", invocation_id)

    def get_entry_by_invocation(
        self,
        invocation_id: str,
    ) -> ExecutionJournalEntry | None:
        self._require_open()
        if not invocation_id.strip():
            raise ValueError("invocation_id must not be empty")
        with connect(self.database_path) as connection:
            row = connection.execute(
                "select * from execution_journal where invocation_id = ?",
                (invocation_id,),
            ).fetchone()
        return None if row is None else self._entry_from_row(row)

    def get_preparation(
        self,
        execution_identity_digest: str,
    ) -> ExecutionPreparation | None:
        self._require_open()
        if not execution_identity_digest.strip():
            raise ValueError("execution_identity_digest must not be empty")
        with connect(self.database_path) as connection:
            row = connection.execute(
                """
                select preparation_id from execution_journal
                where execution_identity_digest = ?
                """,
                (execution_identity_digest,),
            ).fetchone()
        return None if row is None else self._load_preparation(str(row["preparation_id"]))

    def list_entries(
        self,
        *,
        after_position: int = 0,
        limit: int | None = None,
        status: ExecutionJournalStatus | None = None,
    ) -> tuple[ExecutionJournalEntry, ...]:
        self._require_open()
        _validate_cursor(after_position, limit)
        query = "select * from execution_journal where journal_position > ?"
        parameters: tuple[object, ...] = (after_position,)
        if status is not None:
            query += " and status = ?"
            parameters += (status.value,)
        query += " order by journal_position"
        if limit is not None:
            query += " limit ?"
            parameters += (limit,)
        with connect(self.database_path) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(self._entry_from_row(row) for row in rows)

    def list_results(self, idempotency_key: str) -> tuple[ExecutionResult, ...]:
        self._require_open()
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        with connect(self.database_path) as connection:
            rows = connection.execute(
                """
                select history.result_id
                from execution_journal_results as history
                join execution_journal as journal
                  on journal.journal_position = history.journal_position
                where journal.idempotency_key = ?
                order by history.transition_position
                """,
                (idempotency_key,),
            ).fetchall()
        return tuple(self._load_result(str(row["result_id"])) for row in rows)

    def __len__(self) -> int:
        self._require_open()
        with connect(self.database_path) as connection:
            row = connection.execute("select count(*) as count from execution_journal").fetchone()
        if row is None:  # pragma: no cover - SQLite aggregate invariant
            raise ExecutionJournalIntegrityError("SQLite did not return a journal count")
        return int(row["count"])

    def _get_by(self, column: str, value: str) -> ExecutionResult | None:
        self._require_open()
        if not value.strip():
            raise ValueError(f"{column} must not be empty")
        with connect(self.database_path) as connection:
            row = connection.execute(
                f"select * from execution_journal where {column} = ?", (value,)
            ).fetchone()
        return None if row is None else self._current_result(row)

    def _entry_from_row(self, row: sqlite3.Row) -> ExecutionJournalEntry:
        binding = _binding_from_row(row)
        current = self._current_result(row)
        active = None
        if row["active_claim_token"] is not None:
            active = ExecutionClaim(
                int(row["journal_position"]),
                binding,
                int(row["fencing_revision"]),
                str(row["active_claim_token"]),
                ExecutionOperation(str(row["active_operation"])),
                datetime.fromisoformat(str(row["claim_acquired_at"])),
                current,
            )
        try:
            return ExecutionJournalEntry(
                journal_position=int(row["journal_position"]),
                binding=binding,
                status=ExecutionJournalStatus(str(row["status"])),
                current_result=current,
                active_claim=active,
                created_at=datetime.fromisoformat(str(row["created_at"])),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
        except (TypeError, ValueError) as error:
            raise ExecutionJournalIntegrityError(
                f"execution journal entry {row['journal_position']} is invalid"
            ) from error

    def _binding_row(
        self,
        connection: sqlite3.Connection,
        binding: ExecutionBinding,
    ) -> sqlite3.Row | None:
        rows = connection.execute(
            """
            select * from execution_journal
            where idempotency_key = ? or invocation_id = ? or authorization_decision_id = ?
            order by journal_position
            """,
            (
                binding.idempotency_key,
                binding.invocation_id,
                binding.authorization_decision_id,
            ),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise ExecutionIdentityConflict(
                "idempotency, invocation, and authorization identities belong to "
                "different execution journal entries",
                fields=(
                    "idempotency_key",
                    "invocation_id",
                    "authorization_decision_id",
                ),
            )
        return rows[0]

    @staticmethod
    def _validate_binding(stored: ExecutionBinding, candidate: ExecutionBinding) -> None:
        fields = tuple(
            field
            for field in (
                "run_id",
                "invocation_id",
                "proposal_id",
                "authorization_decision_id",
                "affordance",
                "adapter_id",
                "idempotency_key",
                "authorized_action_digest",
                "adapter_contract_version",
                "invocation_digest",
                "definition_digest",
                "preparation_id",
                "execution_identity_digest",
            )
            if getattr(stored, field) != getattr(candidate, field)
        )
        if not fields:
            return
        message = "execution identity collision; mismatched fields: " + ", ".join(fields)
        if stored.idempotency_key == candidate.idempotency_key:
            raise IdempotencyKeyConflict(message, fields=fields)
        if stored.authorization_decision_id == candidate.authorization_decision_id:
            raise AuthorizationBindingConflict(message, fields=fields)
        raise ExecutionIdentityConflict(message, fields=fields)

    def _current_result(self, row: sqlite3.Row) -> ExecutionResult | None:
        value = row["current_result_id"]
        if value is None:
            return None
        result = self._load_result(str(value))
        if result.status.value != str(row["status"]):
            raise ExecutionJournalIntegrityError(
                f"execution journal status does not match result {result.result_id!r}"
            )
        _validate_result_binding(result, _binding_from_row(row))
        return result

    def _put_preparation(
        self,
        preparation: ExecutionPreparation,
    ) -> ExecutionPreparation:
        data = serialize_execution_preparation(preparation).encode("utf-8")
        try:
            artifact = self._artifacts.put_bytes(
                data,
                media_type=EXECUTION_PREPARATION_MEDIA_TYPE,
                encoding="utf-8",
            )
        except ArtifactIntegrityError as error:
            raise ExecutionJournalIntegrityError(
                f"execution preparation artifact {preparation.preparation_id!r} is corrupt"
            ) from error
        if artifact.digest != preparation.preparation_id:
            raise ExecutionJournalIntegrityError(
                "execution preparation artifact has a different content identity"
            )
        return self._load_preparation(preparation.preparation_id)

    def _load_preparation(self, preparation_id: str) -> ExecutionPreparation:
        try:
            reference = self._artifacts.stat(preparation_id)
            if (
                reference.media_type != EXECUTION_PREPARATION_MEDIA_TYPE
                or reference.encoding != "utf-8"
            ):
                raise ExecutionJournalIntegrityError(
                    f"execution preparation artifact {preparation_id!r} has incompatible metadata"
                )
            data = self._artifacts.get_bytes(reference, verify=True)
            preparation = deserialize_execution_preparation(
                data,
                expected_preparation_id=preparation_id,
            )
        except ExecutionJournalIntegrityError:
            raise
        except (ArtifactIntegrityError, ArtifactNotFoundError, TypeError, ValueError) as error:
            raise ExecutionJournalIntegrityError(
                f"execution preparation artifact {preparation_id!r} is missing or invalid"
            ) from error
        return preparation

    def _load_result(self, result_id: str) -> ExecutionResult:
        try:
            reference = self._artifacts.stat(result_id)
            if reference.media_type != EXECUTION_RESULT_MEDIA_TYPE or reference.encoding != "utf-8":
                raise ExecutionJournalIntegrityError(
                    f"execution result artifact {result_id!r} has incompatible metadata"
                )
            data = self._artifacts.get_bytes(reference, verify=True)
            result = deserialize_execution_result(data, expected_result_id=result_id)
        except ExecutionJournalIntegrityError:
            raise
        except (ArtifactIntegrityError, ArtifactNotFoundError, TypeError, ValueError) as error:
            raise ExecutionJournalIntegrityError(
                f"execution result artifact {result_id!r} is missing or invalid"
            ) from error
        return result

    def _initialize_schema(self) -> None:
        with connect(self.database_path) as connection:
            migration_exists = connection.execute(
                "select 1 from sqlite_master where type = 'table' and name = ?",
                (_MIGRATION_TABLE,),
            ).fetchone()
            if migration_exists is not None:
                current = _schema_version(connection)
                if current > _JOURNAL_SCHEMA_VERSION:
                    raise ExecutionJournalSchemaError(
                        f"execution journal schema {current} is newer than supported "
                        f"schema {_JOURNAL_SCHEMA_VERSION}"
                    )
            connection.execute("begin immediate")
            try:
                connection.execute(_MIGRATION_SCHEMA)
                current = _schema_version(connection)
                if current > _JOURNAL_SCHEMA_VERSION:
                    raise ExecutionJournalSchemaError(
                        f"execution journal schema {current} is newer than supported "
                        f"schema {_JOURNAL_SCHEMA_VERSION}"
                    )
                if current < 1:
                    _apply_schema(connection)
                    connection.execute(
                        f"""
                        insert into {_MIGRATION_TABLE}(version, applied_at)
                        values (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                        """
                    )
                else:
                    _apply_schema(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("SQLiteExecutionJournal is closed")


def _binding_values(binding: ExecutionBinding) -> tuple[str, ...]:
    return (
        binding.schema_version,
        binding.execution_identity_digest,
        binding.run_id,
        binding.invocation_id,
        binding.proposal_id,
        binding.authorization_decision_id,
        binding.affordance,
        binding.adapter_id,
        binding.idempotency_key,
        binding.authorized_action_digest,
        binding.adapter_contract_version,
        binding.invocation_digest,
        binding.definition_digest,
        binding.preparation_id,
    )


def _binding_from_row(row: sqlite3.Row) -> ExecutionBinding:
    try:
        binding = ExecutionBinding(
            run_id=str(row["run_id"]),
            invocation_id=str(row["invocation_id"]),
            proposal_id=str(row["proposal_id"]),
            authorization_decision_id=str(row["authorization_decision_id"]),
            affordance=str(row["affordance"]),
            adapter_id=str(row["adapter_id"]),
            idempotency_key=str(row["idempotency_key"]),
            authorized_action_digest=str(row["authorized_action_digest"]),
            adapter_contract_version=str(row["adapter_contract_version"]),
            invocation_digest=str(row["invocation_digest"]),
            definition_digest=str(row["definition_digest"]),
            preparation_id=str(row["preparation_id"]),
            schema_version=str(row["schema_version"]),
        )
    except (TypeError, ValueError) as error:
        raise ExecutionJournalIntegrityError("stored execution binding is invalid") from error
    if binding.execution_identity_digest != str(row["execution_identity_digest"]):
        raise ExecutionJournalIntegrityError("stored execution identity does not match its binding")
    return binding


def _validate_result_binding(result: ExecutionResult, binding: ExecutionBinding) -> None:
    fields = tuple(
        field
        for field in (
            "invocation_id",
            "proposal_id",
            "authorization_decision_id",
            "affordance",
            "adapter_id",
            "idempotency_key",
            "authorized_action_digest",
            "execution_identity_digest",
        )
        if getattr(result, field) != getattr(binding, field)
    )
    if fields:
        raise ExecutionIdentityConflict(
            "execution result does not match its claim binding: " + ", ".join(fields),
            fields=fields,
        )


def _schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(f"select max(version) as version from {_MIGRATION_TABLE}").fetchone()
    return 0 if row is None or row["version"] is None else int(row["version"])


def _apply_schema(connection: sqlite3.Connection) -> None:
    for statement in _JOURNAL_SCHEMA.split(";"):
        if sql := statement.strip():
            connection.execute(sql)


def _claim_token() -> str:
    return f"claim:{uuid4()}"


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _require_not_before(value: datetime, boundary: datetime, label: str) -> None:
    _require_aware(boundary, "stored timestamp")
    if value < boundary:
        raise ValueError(f"{label} cannot precede durable execution state")


def _validate_cursor(after_position: int, limit: int | None) -> None:
    if after_position < 0:
        raise ValueError("after_position must be non-negative")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
