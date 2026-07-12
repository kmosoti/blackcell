from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self
from uuid import uuid4

from blackcell.features.request_decision import (
    DECISION_ATTEMPT_MEDIA_TYPE,
    DECISION_FAILURE_MEDIA_TYPE,
    DECISION_REQUEST_MEDIA_TYPE,
    DECISION_RESPONSE_MEDIA_TYPE,
    DECISION_ROUTE_MEDIA_TYPE,
    DECISION_USAGE_MEDIA_TYPE,
    DecisionAttempt,
    DecisionAttemptClaim,
    DecisionAttemptInProgress,
    DecisionAttemptRecord,
    DecisionFailure,
    DecisionFailureRecord,
    DecisionIdentityConflict,
    DecisionJournalError,
    DecisionPreparation,
    DecisionRequestRecord,
    DecisionResponse,
    DecisionRoute,
    DecisionSuccessRecord,
    DecisionTerminalRecord,
    DecisionUsage,
    RequestDecision,
    decode_decision_attempt,
    decode_decision_failure,
    decode_decision_request,
    decode_decision_response,
    decode_decision_route,
    decode_decision_usage,
    encode_decision_attempt,
    encode_decision_failure,
    encode_decision_request,
    encode_decision_response,
    encode_decision_route,
    encode_decision_usage,
)
from blackcell.kernel import ArtifactIntegrityError, ArtifactNotFoundError, ArtifactStore
from blackcell.kernel.database import connect

_SCHEMA_VERSION = 1
_ROW_SCHEMA_VERSION = "decision-attempt-journal/v1"
_MIGRATION_TABLE = "decision_attempt_journal_schema_migrations"
_ACQUIRED_FENCING_REVISION = 1
_INVOKING_FENCING_REVISION = 2

_MIGRATION_SCHEMA = f"""
create table if not exists {_MIGRATION_TABLE} (
    version integer primary key,
    applied_at text not null
)
"""

_JOURNAL_SCHEMA = """
create table if not exists decision_attempt_journal (
    journal_position integer primary key autoincrement,
    schema_version text not null check(length(schema_version) > 0),
    request_id text not null unique check(length(request_id) > 0),
    request_digest text not null unique check(length(request_digest) > 0),
    run_id text not null check(length(run_id) > 0),
    node_id text not null check(length(node_id) > 0),
    request_artifact_digest text not null,
    status text not null check(status in ('registered', 'prepared', 'succeeded', 'failed')),
    route_id text,
    route_artifact_digest text,
    attempt_id text unique,
    attempt_artifact_digest text,
    fencing_revision integer not null default 0 check(fencing_revision >= 0),
    active_claim_token text,
    claim_acquired_at text,
    response_artifact_digest text unique,
    failure_artifact_digest text unique,
    usage_artifact_digest text unique,
    registered_at text not null,
    prepared_at text,
    updated_at text not null,
    foreign key(request_artifact_digest) references kernel_artifacts(digest),
    foreign key(route_artifact_digest) references kernel_artifacts(digest),
    foreign key(attempt_artifact_digest) references kernel_artifacts(digest),
    foreign key(response_artifact_digest) references kernel_artifacts(digest),
    foreign key(failure_artifact_digest) references kernel_artifacts(digest),
    foreign key(usage_artifact_digest) references kernel_artifacts(digest),
    check(
        (route_id is null and route_artifact_digest is null and prepared_at is null)
        or
        (route_id is not null and route_artifact_digest is not null and prepared_at is not null)
    ),
    check(
        (attempt_id is null and attempt_artifact_digest is null
         and fencing_revision = 0 and claim_acquired_at is null)
        or
        (attempt_id is not null and attempt_artifact_digest is not null
         and fencing_revision >= 1 and claim_acquired_at is not null)
    ),
    check(active_claim_token is null or (status = 'prepared' and attempt_id is not null)),
    check(
        status != 'registered'
        or
        (route_id is null and attempt_id is null and active_claim_token is null
         and response_artifact_digest is null and failure_artifact_digest is null
         and usage_artifact_digest is null)
    ),
    check(
        status != 'prepared'
        or
        (route_id is not null and response_artifact_digest is null
         and failure_artifact_digest is null and usage_artifact_digest is null
         and (attempt_id is null or active_claim_token is not null))
    ),
    check(
        status != 'succeeded'
        or
        (route_id is not null and attempt_id is not null and active_claim_token is null
         and response_artifact_digest is not null and failure_artifact_digest is null
         and usage_artifact_digest is not null)
    ),
    check(
        status != 'failed'
        or
        (active_claim_token is null and response_artifact_digest is null
         and failure_artifact_digest is not null
         and (usage_artifact_digest is null or attempt_id is not null))
    )
);

create index if not exists idx_decision_attempt_journal_status
    on decision_attempt_journal(status, journal_position);
create index if not exists idx_decision_attempt_journal_run
    on decision_attempt_journal(run_id, journal_position);
"""


class SQLiteDecisionAttemptJournal:
    """Artifact-first, single-attempt model-call journal.

    An acquired claim is intentionally permanent until its exact holder commits a
    terminal result. A process interruption therefore leaves an active uncertain
    attempt that fails closed. Lease expiry, reclaim, and automatic reinvocation
    are outside this version's contract.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        database_path: Path | str | None = None,
    ) -> None:
        self.root = Path(root)
        self._artifacts = ArtifactStore(self.root, database_path=database_path)
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

    def register(
        self,
        request: RequestDecision,
        *,
        registered_at: datetime,
    ) -> DecisionRequestRecord:
        self._require_open()
        registered_at = _timestamp(registered_at, "registered_at")
        _require_not_before(registered_at, request.requested_at, "registered_at")
        request_digest = self._put_artifact(
            encode_decision_request(request),
            media_type=DECISION_REQUEST_MEDIA_TYPE,
            expected_digest=request.request_digest,
            label="decision request",
        )
        candidate = DecisionRequestRecord(request, request_digest, registered_at)
        with connect(self.database_path) as connection:
            connection.execute("begin immediate")
            try:
                row = self._request_identity_row(
                    connection,
                    request_id=request.request_id,
                    request_digest=request.request_digest,
                )
                if row is not None:
                    stored = self._request_record(row)
                    if stored.request != request:
                        raise DecisionIdentityConflict(
                            "decision request identity was reused with different content"
                        )
                    connection.commit()
                    return stored
                connection.execute(
                    """
                    insert into decision_attempt_journal(
                        schema_version, request_id, request_digest, run_id, node_id,
                        request_artifact_digest, status, registered_at, updated_at
                    ) values (?, ?, ?, ?, ?, ?, 'registered', ?, ?)
                    """,
                    (
                        _ROW_SCHEMA_VERSION,
                        request.request_id,
                        request.request_digest,
                        request.run_id,
                        request.node_id,
                        request_digest,
                        registered_at.isoformat(),
                        registered_at.isoformat(),
                    ),
                )
                connection.commit()
                return candidate
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise DecisionIdentityConflict(
                    "decision request identity collided with durable journal state"
                ) from error
            except Exception:
                connection.rollback()
                raise

    def record_route(
        self,
        request: DecisionRequestRecord,
        route: DecisionRoute,
        *,
        recorded_at: datetime,
    ) -> DecisionPreparation | DecisionTerminalRecord:
        self._require_open()
        recorded_at = _timestamp(recorded_at, "recorded_at")
        _require_not_before(route.selected_at, request.registered_at, "route.selected_at")
        candidate = DecisionPreparation(request, route, route.route_id, recorded_at)
        route_digest = self._put_artifact(
            encode_decision_route(route),
            media_type=DECISION_ROUTE_MEDIA_TYPE,
            expected_digest=route.route_id,
            label="decision route",
        )
        with connect(self.database_path) as connection:
            connection.execute("begin immediate")
            try:
                row = self._exact_request_row(connection, request)
                if _is_terminal(row):
                    terminal = self._terminal_record(row)
                    connection.commit()
                    return terminal
                stored_request = self._request_record(row)
                if row["route_id"] is not None:
                    stored = self._preparation(row, stored_request)
                    if stored.route != route:
                        raise DecisionIdentityConflict(
                            "decision request was already prepared with a different route"
                        )
                    connection.commit()
                    return stored
                if str(row["status"]) != "registered":
                    raise DecisionJournalError("decision request is not routable")
                _require_not_before(recorded_at, _updated_at(row), "recorded_at")
                connection.execute(
                    """
                    update decision_attempt_journal
                    set status = 'prepared', route_id = ?, route_artifact_digest = ?,
                        prepared_at = ?, updated_at = ?
                    where journal_position = ? and status = 'registered'
                    """,
                    (
                        route.route_id,
                        route_digest,
                        recorded_at.isoformat(),
                        recorded_at.isoformat(),
                        int(row["journal_position"]),
                    ),
                )
                connection.commit()
                return candidate
            except Exception:
                connection.rollback()
                raise

    def reject(
        self,
        request: DecisionRequestRecord,
        failure: DecisionFailure,
        *,
        recorded_at: datetime,
    ) -> DecisionFailureRecord:
        self._require_open()
        recorded_at = _timestamp(recorded_at, "recorded_at")
        if failure.route_id is not None or failure.attempt_id is not None:
            raise DecisionIdentityConflict("a pre-route rejection cannot bind a route or attempt")
        failure_digest = self._put_artifact(
            encode_decision_failure(failure),
            media_type=DECISION_FAILURE_MEDIA_TYPE,
            expected_digest=failure.failure_id,
            label="decision failure",
        )
        candidate = DecisionFailureRecord(request, failure, failure_digest)
        _require_not_before(failure.failed_at, request.registered_at, "failed_at")
        with connect(self.database_path) as connection:
            connection.execute("begin immediate")
            try:
                row = self._exact_request_row(connection, request)
                if _is_terminal(row):
                    terminal = self._terminal_record(row)
                    if isinstance(terminal, DecisionFailureRecord) and terminal == candidate:
                        connection.commit()
                        return terminal
                    if isinstance(terminal, DecisionFailureRecord):
                        raise DecisionIdentityConflict(
                            "decision request already has a different terminal failure"
                        )
                    raise DecisionIdentityConflict(
                        "a successful decision cannot be replaced by a route rejection"
                    )
                if str(row["status"]) != "registered":
                    raise DecisionIdentityConflict(
                        "a prepared decision cannot be replaced by a pre-route rejection"
                    )
                _validate_failure_time(failure, recorded_at)
                _require_not_before(recorded_at, _updated_at(row), "recorded_at")
                self._write_failure(
                    connection,
                    row,
                    failure_digest=failure_digest,
                    usage_digest=None,
                    recorded_at=recorded_at,
                    claim=None,
                )
                connection.commit()
                return candidate
            except Exception:
                connection.rollback()
                raise

    def acquire(
        self,
        preparation: DecisionPreparation,
        *,
        acquired_at: datetime,
    ) -> DecisionAttemptClaim | DecisionTerminalRecord:
        self._require_open()
        acquired_at = _timestamp(acquired_at, "acquired_at")
        with connect(self.database_path) as connection:
            row = self._exact_request_row(connection, preparation.request_record)
            if _is_terminal(row):
                return self._terminal_record(row)
            self._validate_preparation(row, preparation)
            if row["active_claim_token"] is not None:
                raise DecisionAttemptInProgress(
                    f"decision request {preparation.request_record.request.request_id!r} "
                    "has an active or uncertain attempt"
                )
            if row["attempt_id"] is not None:
                raise DecisionJournalError(
                    "decision attempt has no terminal result and cannot be automatically reclaimed"
                )
            _require_not_before(acquired_at, _updated_at(row), "acquired_at")

        request = preparation.request_record.request
        attempt = DecisionAttempt(
            request.request_id,
            request.request_digest,
            preparation.route.route_id,
            1,
            acquired_at,
        )
        attempt_digest = self._put_artifact(
            encode_decision_attempt(attempt),
            media_type=DECISION_ATTEMPT_MEDIA_TYPE,
            expected_digest=attempt.attempt_id,
            label="decision attempt",
        )
        token = f"claim:{uuid4()}"
        claim = DecisionAttemptClaim(
            DecisionAttemptRecord(attempt, attempt_digest),
            _ACQUIRED_FENCING_REVISION,
            token,
        )
        with connect(self.database_path) as connection:
            connection.execute("begin immediate")
            try:
                row = self._exact_request_row(connection, preparation.request_record)
                if _is_terminal(row):
                    terminal = self._terminal_record(row)
                    connection.commit()
                    return terminal
                self._validate_preparation(row, preparation)
                if row["active_claim_token"] is not None or row["attempt_id"] is not None:
                    raise DecisionAttemptInProgress(
                        f"decision request {request.request_id!r} has an active or "
                        "uncertain attempt"
                    )
                changed = connection.execute(
                    """
                    update decision_attempt_journal
                    set attempt_id = ?, attempt_artifact_digest = ?, fencing_revision = ?,
                        active_claim_token = ?, claim_acquired_at = ?, updated_at = ?
                    where journal_position = ? and status = 'prepared'
                      and attempt_id is null and active_claim_token is null
                    """,
                    (
                        attempt.attempt_id,
                        attempt_digest,
                        _ACQUIRED_FENCING_REVISION,
                        token,
                        acquired_at.isoformat(),
                        acquired_at.isoformat(),
                        int(row["journal_position"]),
                    ),
                ).rowcount
                if changed != 1:
                    raise DecisionAttemptInProgress(
                        f"decision request {request.request_id!r} was claimed concurrently"
                    )
                connection.commit()
                return claim
            except Exception:
                connection.rollback()
                raise

    def begin_invoke(
        self,
        preparation: DecisionPreparation,
        claim: DecisionAttemptClaim,
        *,
        invoked_at: datetime,
    ) -> DecisionAttemptClaim | DecisionTerminalRecord:
        """Consume an acquired fence exactly once before crossing into live inference."""

        self._require_open()
        invoked_at = _timestamp(invoked_at, "invoked_at")
        with connect(self.database_path) as connection:
            connection.execute("begin immediate")
            try:
                row = self._exact_request_row(connection, preparation.request_record)
                if _is_terminal(row):
                    terminal = self._terminal_record(row)
                    connection.commit()
                    return terminal
                self._validate_active_claim(row, preparation, claim)
                if claim.fencing_revision != _ACQUIRED_FENCING_REVISION:
                    raise DecisionJournalError("decision claim was already admitted for invocation")
                _require_not_before(invoked_at, _updated_at(row), "invoked_at")
                changed = connection.execute(
                    """
                    update decision_attempt_journal
                    set fencing_revision = ?, updated_at = ?
                    where journal_position = ? and status = 'prepared'
                      and active_claim_token = ? and fencing_revision = ?
                    """,
                    (
                        _INVOKING_FENCING_REVISION,
                        invoked_at.isoformat(),
                        int(row["journal_position"]),
                        claim.claim_token,
                        _ACQUIRED_FENCING_REVISION,
                    ),
                ).rowcount
                if changed != 1:
                    raise DecisionJournalError(
                        "decision invocation admission was fenced by another caller"
                    )
                connection.commit()
                return replace(
                    claim,
                    fencing_revision=_INVOKING_FENCING_REVISION,
                    invoked_at=invoked_at,
                )
            except Exception:
                connection.rollback()
                raise

    def succeed(
        self,
        preparation: DecisionPreparation,
        claim: DecisionAttemptClaim,
        response: DecisionResponse,
        usage: DecisionUsage,
        *,
        recorded_at: datetime,
    ) -> DecisionSuccessRecord:
        self._require_open()
        recorded_at = _timestamp(recorded_at, "recorded_at")
        response_digest = self._put_artifact(
            encode_decision_response(response),
            media_type=DECISION_RESPONSE_MEDIA_TYPE,
            expected_digest=response.response_id,
            label="decision response",
        )
        usage_digest = self._put_artifact(
            encode_decision_usage(usage),
            media_type=DECISION_USAGE_MEDIA_TYPE,
            expected_digest=usage.usage_id,
            label="decision usage",
        )
        candidate = DecisionSuccessRecord(
            preparation,
            claim.attempt_record,
            response,
            response_digest,
            usage,
            usage_digest,
        )
        with connect(self.database_path) as connection:
            connection.execute("begin immediate")
            try:
                row = self._exact_request_row(connection, preparation.request_record)
                if _is_terminal(row):
                    terminal = self._terminal_record(row)
                    if isinstance(terminal, DecisionSuccessRecord) and terminal == candidate:
                        connection.commit()
                        return terminal
                    raise DecisionIdentityConflict(
                        "decision attempt already has a different terminal record"
                    )
                self._validate_invocation_claim(row, preparation, claim)
                _require_not_before(recorded_at, _updated_at(row), "recorded_at")
                _require_not_before(recorded_at, response.completed_at, "recorded_at")
                if claim.invoked_at is None:  # pragma: no cover - validation guard
                    raise DecisionJournalError("decision invocation time is missing")
                if response.completed_at < claim.invoked_at:
                    raise DecisionIdentityConflict(
                        "decision response completion precedes invocation admission"
                    )
                changed = connection.execute(
                    """
                    update decision_attempt_journal
                    set status = 'succeeded', active_claim_token = null,
                        response_artifact_digest = ?, usage_artifact_digest = ?, updated_at = ?
                    where journal_position = ? and status = 'prepared'
                      and active_claim_token = ? and fencing_revision = ?
                    """,
                    (
                        response_digest,
                        usage_digest,
                        recorded_at.isoformat(),
                        int(row["journal_position"]),
                        claim.claim_token,
                        claim.fencing_revision,
                    ),
                ).rowcount
                if changed != 1:
                    raise DecisionJournalError("decision completion was fenced by another claim")
                connection.commit()
                return candidate
            except Exception:
                connection.rollback()
                raise

    def fail(
        self,
        request: DecisionRequestRecord,
        failure: DecisionFailure,
        *,
        preparation: DecisionPreparation | None,
        claim: DecisionAttemptClaim | None,
        usage: DecisionUsage | None,
        recorded_at: datetime,
    ) -> DecisionFailureRecord:
        self._require_open()
        recorded_at = _timestamp(recorded_at, "recorded_at")
        failure_digest = self._put_artifact(
            encode_decision_failure(failure),
            media_type=DECISION_FAILURE_MEDIA_TYPE,
            expected_digest=failure.failure_id,
            label="decision failure",
        )
        usage_digest = None
        if usage is not None:
            usage_digest = self._put_artifact(
                encode_decision_usage(usage),
                media_type=DECISION_USAGE_MEDIA_TYPE,
                expected_digest=usage.usage_id,
                label="decision usage",
            )
        attempt_record = None if claim is None else claim.attempt_record
        candidate = DecisionFailureRecord(
            request,
            failure,
            failure_digest,
            preparation,
            attempt_record,
            usage,
            usage_digest,
        )
        _require_not_before(failure.failed_at, request.registered_at, "failed_at")
        if preparation is not None:
            _require_not_before(failure.failed_at, preparation.prepared_at, "failed_at")
        if claim is not None:
            _require_not_before(
                failure.failed_at,
                claim.attempt_record.attempt.started_at,
                "failed_at",
            )
            if claim.invoked_at is None:
                raise DecisionJournalError("decision invocation time is missing")
            _require_not_before(failure.failed_at, claim.invoked_at, "failed_at")
        with connect(self.database_path) as connection:
            connection.execute("begin immediate")
            try:
                row = self._exact_request_row(connection, request)
                if _is_terminal(row):
                    terminal = self._terminal_record(row)
                    if isinstance(terminal, DecisionFailureRecord) and terminal == candidate:
                        connection.commit()
                        return terminal
                    if isinstance(terminal, DecisionFailureRecord):
                        raise DecisionIdentityConflict(
                            "decision attempt already has a different terminal failure"
                        )
                    raise DecisionIdentityConflict(
                        "a successful decision cannot be replaced by a failure"
                    )
                _validate_failure_time(failure, recorded_at)
                _require_not_before(recorded_at, _updated_at(row), "recorded_at")
                if preparation is None:
                    if claim is not None or usage is not None or str(row["status"]) != "registered":
                        raise DecisionIdentityConflict(
                            "unrouted failure does not match durable decision state"
                        )
                else:
                    self._validate_preparation(row, preparation)
                    if claim is None:
                        if usage is not None or row["attempt_id"] is not None:
                            raise DecisionIdentityConflict(
                                "unattempted failure does not match durable decision state"
                            )
                    else:
                        self._validate_invocation_claim(row, preparation, claim)
                self._write_failure(
                    connection,
                    row,
                    failure_digest=failure_digest,
                    usage_digest=usage_digest,
                    recorded_at=recorded_at,
                    claim=claim,
                )
                connection.commit()
                return candidate
            except Exception:
                connection.rollback()
                raise

    def _write_failure(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        failure_digest: str,
        usage_digest: str | None,
        recorded_at: datetime,
        claim: DecisionAttemptClaim | None,
    ) -> None:
        if claim is None:
            predicate = "active_claim_token is null"
            parameters: tuple[object, ...] = ()
        else:
            predicate = "active_claim_token = ? and fencing_revision = ?"
            parameters = (claim.claim_token, claim.fencing_revision)
        changed = connection.execute(
            f"""
            update decision_attempt_journal
            set status = 'failed', active_claim_token = null,
                failure_artifact_digest = ?, usage_artifact_digest = ?, updated_at = ?
            where journal_position = ? and status in ('registered', 'prepared')
              and {predicate}
            """,
            (
                failure_digest,
                usage_digest,
                recorded_at.isoformat(),
                int(row["journal_position"]),
                *parameters,
            ),
        ).rowcount
        if changed != 1:
            raise DecisionJournalError("decision failure completion was fenced")

    def _request_identity_row(
        self,
        connection: sqlite3.Connection,
        *,
        request_id: str,
        request_digest: str,
    ) -> sqlite3.Row | None:
        rows = connection.execute(
            """
            select * from decision_attempt_journal
            where request_id = ? or request_digest = ?
            order by journal_position
            """,
            (request_id, request_digest),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise DecisionJournalError("decision request identities span multiple journal rows")
        row = rows[0]
        if str(row["request_id"]) != request_id or str(row["request_digest"]) != request_digest:
            raise DecisionIdentityConflict(
                "decision request id or digest belongs to different durable content"
            )
        return row

    def _exact_request_row(
        self,
        connection: sqlite3.Connection,
        request_record: DecisionRequestRecord,
    ) -> sqlite3.Row:
        request = request_record.request
        row = self._request_identity_row(
            connection,
            request_id=request.request_id,
            request_digest=request.request_digest,
        )
        if row is None:
            raise DecisionJournalError("decision request is not registered")
        stored = self._request_record(row)
        if stored != request_record:
            raise DecisionIdentityConflict(
                "decision request record does not match durable registration"
            )
        return row

    def _request_record(self, row: sqlite3.Row) -> DecisionRequestRecord:
        self._validate_row(row)
        digest = str(row["request_artifact_digest"])
        request = self._load_request(digest)
        if (
            request.request_id != str(row["request_id"])
            or request.request_digest != str(row["request_digest"])
            or request.run_id != str(row["run_id"])
            or request.node_id != str(row["node_id"])
        ):
            raise DecisionJournalError("stored decision request row does not match its artifact")
        try:
            return DecisionRequestRecord(
                request,
                digest,
                _stored_datetime(row, "registered_at"),
            )
        except (TypeError, ValueError) as error:
            raise DecisionJournalError("stored decision request record is invalid") from error

    def _preparation(
        self,
        row: sqlite3.Row,
        request: DecisionRequestRecord | None = None,
    ) -> DecisionPreparation:
        request_record = request or self._request_record(row)
        if row["route_id"] is None or row["route_artifact_digest"] is None:
            raise DecisionJournalError("stored decision has no prepared route")
        route_id = str(row["route_id"])
        route_digest = str(row["route_artifact_digest"])
        if route_id != route_digest:
            raise DecisionJournalError("stored decision route identity is inconsistent")
        route = self._load_route(route_digest)
        try:
            return DecisionPreparation(
                request_record,
                route,
                route_digest,
                _stored_datetime(row, "prepared_at"),
            )
        except (TypeError, ValueError) as error:
            raise DecisionJournalError("stored decision preparation is invalid") from error

    def _attempt_record(self, row: sqlite3.Row) -> DecisionAttemptRecord:
        if row["attempt_id"] is None or row["attempt_artifact_digest"] is None:
            raise DecisionJournalError("stored decision has no attempt")
        attempt_id = str(row["attempt_id"])
        attempt_digest = str(row["attempt_artifact_digest"])
        if attempt_id != attempt_digest:
            raise DecisionJournalError("stored decision attempt identity is inconsistent")
        attempt = self._load_attempt(attempt_digest)
        try:
            return DecisionAttemptRecord(attempt, attempt_digest)
        except (TypeError, ValueError) as error:
            raise DecisionJournalError("stored decision attempt record is invalid") from error

    def _terminal_record(self, row: sqlite3.Row) -> DecisionTerminalRecord:
        status = str(row["status"])
        request = self._request_record(row)
        if status == "succeeded":
            preparation = self._preparation(row, request)
            attempt = self._attempt_record(row)
            response_digest = _required_row_text(row, "response_artifact_digest")
            usage_digest = _required_row_text(row, "usage_artifact_digest")
            response = self._load_response(response_digest, request.request)
            usage = self._load_usage(usage_digest)
            try:
                return DecisionSuccessRecord(
                    preparation,
                    attempt,
                    response,
                    response_digest,
                    usage,
                    usage_digest,
                )
            except (TypeError, ValueError) as error:
                raise DecisionJournalError("stored decision success record is invalid") from error
        if status == "failed":
            failure_digest = _required_row_text(row, "failure_artifact_digest")
            failure = self._load_failure(failure_digest)
            preparation = None if row["route_id"] is None else self._preparation(row, request)
            attempt = None if row["attempt_id"] is None else self._attempt_record(row)
            usage_digest = (
                None if row["usage_artifact_digest"] is None else str(row["usage_artifact_digest"])
            )
            usage = None if usage_digest is None else self._load_usage(usage_digest)
            try:
                return DecisionFailureRecord(
                    request,
                    failure,
                    failure_digest,
                    preparation,
                    attempt,
                    usage,
                    usage_digest,
                )
            except (TypeError, ValueError) as error:
                raise DecisionJournalError("stored decision failure record is invalid") from error
        raise DecisionJournalError("decision journal row is not terminal")

    def _validate_preparation(
        self,
        row: sqlite3.Row,
        preparation: DecisionPreparation,
    ) -> None:
        stored = self._preparation(row)
        if stored != preparation:
            raise DecisionIdentityConflict(
                "decision preparation does not match durable request and route"
            )
        if str(row["status"]) != "prepared":
            raise DecisionJournalError("decision request is not prepared")

    def _validate_active_claim(
        self,
        row: sqlite3.Row,
        preparation: DecisionPreparation,
        claim: DecisionAttemptClaim,
    ) -> None:
        self._validate_preparation(row, preparation)
        stored_attempt = self._attempt_record(row)
        if stored_attempt != claim.attempt_record:
            raise DecisionIdentityConflict("decision claim belongs to a different attempt")
        if (
            row["active_claim_token"] != claim.claim_token
            or int(row["fencing_revision"]) != claim.fencing_revision
        ):
            raise DecisionJournalError("decision claim is stale or fenced")
        if _stored_datetime(row, "claim_acquired_at") != claim.attempt_record.attempt.started_at:
            raise DecisionJournalError("stored decision claim time is inconsistent")

    def _validate_invocation_claim(
        self,
        row: sqlite3.Row,
        preparation: DecisionPreparation,
        claim: DecisionAttemptClaim,
    ) -> None:
        self._validate_active_claim(row, preparation, claim)
        if claim.fencing_revision != _INVOKING_FENCING_REVISION:
            raise DecisionJournalError("decision claim was not admitted for invocation")
        if claim.invoked_at is None:
            raise DecisionJournalError("decision invocation time is missing")
        if _updated_at(row) != claim.invoked_at:
            raise DecisionJournalError("decision invocation time is inconsistent")

    def _validate_row(self, row: sqlite3.Row) -> None:
        if str(row["schema_version"]) != _ROW_SCHEMA_VERSION:
            raise DecisionJournalError(
                f"unsupported decision journal row schema {row['schema_version']!r}"
            )
        status = str(row["status"])
        if status not in {"registered", "prepared", "succeeded", "failed"}:
            raise DecisionJournalError(f"stored decision status {status!r} is invalid")

    def _put_artifact(
        self,
        data: bytes,
        *,
        media_type: str,
        expected_digest: str,
        label: str,
    ) -> str:
        try:
            reference = self._artifacts.put_bytes(
                data,
                media_type=media_type,
                encoding="utf-8",
            )
        except (ArtifactIntegrityError, ArtifactNotFoundError, TypeError, ValueError) as error:
            raise DecisionJournalError(f"{label} artifact could not be persisted") from error
        if reference.digest != expected_digest:
            raise DecisionJournalError(f"{label} artifact identity does not match typed content")
        if reference.media_type != media_type or reference.encoding != "utf-8":
            raise DecisionJournalError(f"{label} artifact has incompatible metadata")
        return reference.digest

    def _load_artifact(self, digest: str, *, media_type: str, label: str) -> bytes:
        try:
            reference = self._artifacts.stat(digest)
            if reference.media_type != media_type or reference.encoding != "utf-8":
                raise DecisionJournalError(f"{label} artifact has incompatible metadata")
            return self._artifacts.get_bytes(reference, verify=True)
        except DecisionJournalError:
            raise
        except (ArtifactIntegrityError, ArtifactNotFoundError, TypeError, ValueError) as error:
            raise DecisionJournalError(f"{label} artifact is missing or invalid") from error

    def _load_request(self, digest: str) -> RequestDecision:
        data = self._load_artifact(
            digest,
            media_type=DECISION_REQUEST_MEDIA_TYPE,
            label="decision request",
        )
        try:
            return decode_decision_request(data, expected_request_digest=digest)
        except (TypeError, ValueError) as error:
            raise DecisionJournalError("decision request artifact is malformed") from error

    def _load_route(self, digest: str) -> DecisionRoute:
        data = self._load_artifact(
            digest,
            media_type=DECISION_ROUTE_MEDIA_TYPE,
            label="decision route",
        )
        try:
            return decode_decision_route(data, expected_route_id=digest)
        except (TypeError, ValueError) as error:
            raise DecisionJournalError("decision route artifact is malformed") from error

    def _load_attempt(self, digest: str) -> DecisionAttempt:
        data = self._load_artifact(
            digest,
            media_type=DECISION_ATTEMPT_MEDIA_TYPE,
            label="decision attempt",
        )
        try:
            return decode_decision_attempt(data, expected_attempt_id=digest)
        except (TypeError, ValueError) as error:
            raise DecisionJournalError("decision attempt artifact is malformed") from error

    def _load_response(self, digest: str, request: RequestDecision) -> DecisionResponse:
        data = self._load_artifact(
            digest,
            media_type=DECISION_RESPONSE_MEDIA_TYPE,
            label="decision response",
        )
        try:
            return decode_decision_response(
                data,
                expected_response_id=digest,
                request=request,
            )
        except (TypeError, ValueError) as error:
            raise DecisionJournalError("decision response artifact is malformed") from error

    def _load_failure(self, digest: str) -> DecisionFailure:
        data = self._load_artifact(
            digest,
            media_type=DECISION_FAILURE_MEDIA_TYPE,
            label="decision failure",
        )
        try:
            return decode_decision_failure(data, expected_failure_id=digest)
        except (TypeError, ValueError) as error:
            raise DecisionJournalError("decision failure artifact is malformed") from error

    def _load_usage(self, digest: str) -> DecisionUsage:
        data = self._load_artifact(
            digest,
            media_type=DECISION_USAGE_MEDIA_TYPE,
            label="decision usage",
        )
        try:
            return decode_decision_usage(data, expected_usage_id=digest)
        except (TypeError, ValueError) as error:
            raise DecisionJournalError("decision usage artifact is malformed") from error

    def _initialize_schema(self) -> None:
        with connect(self.database_path) as connection:
            migration_exists = connection.execute(
                "select 1 from sqlite_master where type = 'table' and name = ?",
                (_MIGRATION_TABLE,),
            ).fetchone()
            if migration_exists is not None:
                current = _schema_version(connection)
                if current > _SCHEMA_VERSION:
                    raise DecisionJournalError(
                        f"decision attempt journal schema {current} is newer than supported "
                        f"schema {_SCHEMA_VERSION}"
                    )
            connection.execute("begin immediate")
            try:
                connection.execute(_MIGRATION_SCHEMA)
                current = _schema_version(connection)
                if current > _SCHEMA_VERSION:
                    raise DecisionJournalError(
                        f"decision attempt journal schema {current} is newer than supported "
                        f"schema {_SCHEMA_VERSION}"
                    )
                _apply_schema(connection)
                if current < 1:
                    connection.execute(
                        f"""
                        insert into {_MIGRATION_TABLE}(version, applied_at)
                        values (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                        """
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("SQLiteDecisionAttemptJournal is closed")


def _is_terminal(row: sqlite3.Row) -> bool:
    return str(row["status"]) in {"succeeded", "failed"}


def _required_row_text(row: sqlite3.Row, field: str) -> str:
    value = row[field]
    if not isinstance(value, str) or not value.strip():
        raise DecisionJournalError(f"stored decision row requires {field}")
    return value


def _stored_datetime(row: sqlite3.Row, field: str) -> datetime:
    value = row[field]
    if not isinstance(value, str):
        raise DecisionJournalError(f"stored decision row requires {field}")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise DecisionJournalError(f"stored decision {field} is invalid") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise DecisionJournalError(f"stored decision {field} must be timezone-aware")
    return result.astimezone(UTC)


def _updated_at(row: sqlite3.Row) -> datetime:
    return _stored_datetime(row, "updated_at")


def _timestamp(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _require_not_before(value: datetime, boundary: datetime, label: str) -> None:
    if value < boundary:
        raise ValueError(f"{label} cannot precede durable decision state")


def _validate_failure_time(failure: DecisionFailure, recorded_at: datetime) -> None:
    _require_not_before(recorded_at, failure.failed_at, "recorded_at")


def _schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(f"select max(version) as version from {_MIGRATION_TABLE}").fetchone()
    return 0 if row is None or row["version"] is None else int(row["version"])


def _apply_schema(connection: sqlite3.Connection) -> None:
    for statement in _JOURNAL_SCHEMA.split(";"):
        if sql := statement.strip():
            connection.execute(sql)


__all__ = ["SQLiteDecisionAttemptJournal"]
