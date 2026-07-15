from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from blackcell.adapters.execution.local_process import (
    LOCAL_PROCESS_ADAPTER_ID,
    LocalProcessAdapter,
    LocalProcessAffordance,
    LocalProcessRegistry,
    ProcessRun,
)
from blackcell.adapters.persistence.sqlite import SQLiteExecutionJournal
from blackcell.features.execute_affordance import (
    AffordanceDefinition,
    AffordanceExecutionHandler,
    AffordanceInvocation,
    ExecutionStatus,
    SideEffectClass,
    UncertainExecutionError,
)
from blackcell.kernel import ArtifactStore


def test_real_process_adapter_is_idempotent_across_journal_restart(tmp_path: Path) -> None:
    executable = _script(
        tmp_path,
        "random_output",
        "import os; os.write(1, os.urandom(32))",
    )
    configuration = _configuration(tmp_path, executable)
    registry = LocalProcessRegistry((configuration,))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    invocation = _invocation()
    authorization = _Authorization(invocation)

    with SQLiteExecutionJournal(tmp_path / "artifacts") as journal:
        handler = AffordanceExecutionHandler(
            {LOCAL_PROCESS_ADAPTER_ID: LocalProcessAdapter(registry, artifacts)},
            journal,
        )
        first = handler.handle(
            invocation,
            configuration.definition,
            authorization,
            run_id="run:local-process",
        )
        repeated = handler.handle(
            invocation,
            configuration.definition,
            authorization,
            run_id="run:local-process",
        )

    with SQLiteExecutionJournal(tmp_path / "artifacts") as reopened:
        after_restart = AffordanceExecutionHandler(
            {LOCAL_PROCESS_ADAPTER_ID: LocalProcessAdapter(registry, artifacts)},
            reopened,
        ).handle(
            invocation,
            configuration.definition,
            authorization,
            run_id="run:local-process",
        )
        results = reopened.list_results(invocation.idempotency_key)

    assert first.status is ExecutionStatus.SUCCEEDED
    assert repeated == after_restart == first
    assert results == (first,)


def test_unknown_attempt_reconciles_through_same_audited_read_only_declaration(
    tmp_path: Path,
) -> None:
    executable = _script(tmp_path, "recovery", "import os; os.write(1, b'recovered')")
    configuration = _configuration(tmp_path, executable)
    registry = LocalProcessRegistry((configuration,))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    invocation = _invocation()
    authorization = _Authorization(invocation)

    with SQLiteExecutionJournal(tmp_path / "artifacts") as journal:
        uncertain_handler = AffordanceExecutionHandler(
            {
                LOCAL_PROCESS_ADAPTER_ID: LocalProcessAdapter(
                    registry,
                    artifacts,
                    runner=_UncertainRunner(),
                )
            },
            journal,
        )
        unknown = uncertain_handler.handle(
            invocation,
            configuration.definition,
            authorization,
            run_id="run:local-process",
        )
        recovered = AffordanceExecutionHandler(
            {LOCAL_PROCESS_ADAPTER_ID: LocalProcessAdapter(registry, artifacts)},
            journal,
        ).handle(
            invocation,
            configuration.definition,
            authorization,
            run_id="run:local-process",
        )

        history = journal.list_results(invocation.idempotency_key)

    assert unknown.status is ExecutionStatus.UNKNOWN
    assert recovered.status is ExecutionStatus.SUCCEEDED
    assert recovered.reconciled
    assert tuple(item.status for item in history) == (
        ExecutionStatus.UNKNOWN,
        ExecutionStatus.SUCCEEDED,
    )


@dataclass(frozen=True, slots=True)
class _Authorization:
    decision_id: str
    proposal_id: str
    outcome: str
    authorized_action_digest: str
    authorized_read_only: bool

    def __init__(self, invocation: AffordanceInvocation) -> None:
        object.__setattr__(self, "decision_id", "authorization:local-process")
        object.__setattr__(self, "proposal_id", invocation.proposal_id)
        object.__setattr__(self, "outcome", "allow")
        object.__setattr__(self, "authorized_action_digest", invocation.action_digest)
        object.__setattr__(self, "authorized_read_only", True)


class _UncertainRunner:
    def run(self, *_args: object, **_kwargs: object) -> ProcessRun:
        raise UncertainExecutionError("simulated uncertain worker termination")


def _configuration(root: Path, executable: _TrustedProgram) -> LocalProcessAffordance:
    return LocalProcessAffordance(
        definition=AffordanceDefinition(
            "probe",
            LOCAL_PROCESS_ADAPTER_ID,
            SideEffectClass.READ_ONLY,
            1.0,
        ),
        executable=str(executable.binary.resolve()),
        fixed_argv=("-I", "-S", "-c", f"exec({executable.body!r})"),
        bindings=(),
        working_directory=str(root.resolve()),
        allowed_path_roots=(str(root.resolve()),),
        termination_grace_seconds=0.1,
        drain_grace_seconds=0.1,
    )


def _invocation() -> AffordanceInvocation:
    return AffordanceInvocation(
        "invocation:local-process",
        "proposal:local-process",
        "probe",
        (),
        "idempotency:local-process",
        datetime.now(UTC) - timedelta(seconds=1),
    )


@dataclass(frozen=True, slots=True)
class _TrustedProgram:
    binary: Path
    body: str


def _script(root: Path, name: str, body: str) -> _TrustedProgram:
    del root, name
    return _TrustedProgram(Path(sys.executable).resolve(), body)
