from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable, Mapping, Set
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from blackcell.features.execute_affordance import (
    AdapterOutcome,
    AffordanceDefinition,
    AffordanceInvocation,
    ExecutionResult,
)
from blackcell.features.observe_outcome import (
    ObserveOutcome,
    OutcomeClaim,
    OutcomeEvidencePointer,
    OutcomeObservation,
    OutcomeObservationStatus,
)
from blackcell.gateway import AdapterResult, ModelCapability, ModelRequest
from blackcell.kernel import ArtifactStore, JsonValue, new_event_id
from blackcell.operator.status import RepositoryStatusPort, RepositoryStatusSnapshot

REPOSITORY_STATUS_ADAPTER_ID = "repository-status"
REPOSITORY_STATUS_CONTRACT_VERSION = "repository-status/v1"
REPOSITORY_OUTCOME_OBSERVER_ID = "repository-status-observer"
REPOSITORY_OUTCOME_CONTRACT_VERSION = "repository-status-observer/v1"
REPOSITORY_MODEL_ADAPTER_ID = "repository-recorded"

_STATUS_COMMAND = ("git", "status", "--porcelain=v1", "--untracked-files=all")
_SUPPORTED_FACTS = frozenset({("repository", "git.valid"), ("repository", "git.clean")})

RunCommand = Callable[..., subprocess.CompletedProcess[str]]
Clock = Callable[[], datetime]


class RepositoryStatusError(RuntimeError):
    """A bounded repository status read failed without exposing command output."""


class RepositoryStatusReader:
    """Run one fixed, no-shell Git status command through explicit byte/time bounds."""

    def __init__(
        self,
        repo_root: Path | str,
        *,
        timeout_seconds: float = 10.0,
        max_output_bytes: int = 1_048_576,
        runner: RunCommand = subprocess.run,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or timeout_seconds <= 0
        ):
            raise ValueError("repository status timeout must be positive")
        if (
            isinstance(max_output_bytes, bool)
            or not isinstance(max_output_bytes, int)
            or max_output_bytes < 1
        ):
            raise ValueError("repository status byte boundary must be positive")
        self.repo_root = Path(repo_root).resolve()
        self._timeout_seconds = float(timeout_seconds)
        self._max_output_bytes = max_output_bytes
        self._runner = runner
        self._clock = clock

    def read(self) -> RepositoryStatusSnapshot:
        try:
            completed = self._runner(
                list(_STATUS_COMMAND),
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("repository status exceeded its deadline") from error
        except OSError as error:
            raise RepositoryStatusError("repository status process could not start") from error
        stdout = _bounded_text(completed.stdout, self._max_output_bytes, "stdout")
        stderr = _bounded_text(completed.stderr, self._max_output_bytes, "stderr")
        encoded = stdout.encode("utf-8")
        diagnostic = stderr.encode("utf-8")
        digest = "sha256:" + hashlib.sha256(encoded + b"\0" + diagnostic).hexdigest()
        valid = completed.returncode == 0
        entries = stdout.splitlines() if valid else []
        return RepositoryStatusSnapshot(
            valid=valid,
            clean=valid and not entries,
            entry_count=len(entries),
            output_digest=digest,
            observed_at=_aware(self._clock()),
        )


class RepositoryStatusExecutionAdapter:
    adapter_id = REPOSITORY_STATUS_ADAPTER_ID
    contract_version = REPOSITORY_STATUS_CONTRACT_VERSION

    def __init__(self, reader: RepositoryStatusPort, artifacts: ArtifactStore) -> None:
        self._reader = reader
        self._artifacts = artifacts

    def execute(
        self,
        invocation: AffordanceInvocation,
        definition: AffordanceDefinition,
    ) -> AdapterOutcome:
        _validate_invocation(invocation, definition)
        snapshot = self._reader.read()
        reference = self._artifacts.put_json(
            snapshot.manifest(schema_version=self.contract_version)
        )
        return AdapterOutcome(
            success=snapshot.valid,
            output_digest=reference.digest,
            completed_at=snapshot.observed_at,
            error_code=None if snapshot.valid else "repository-status-invalid",
        )

    def reconcile(
        self,
        invocation: AffordanceInvocation,
        definition: AffordanceDefinition,
        previous: ExecutionResult | None,
    ) -> AdapterOutcome:
        del previous
        return self.execute(invocation, definition)


class RepositoryStatusOutcomeObserver:
    observer_id = REPOSITORY_OUTCOME_OBSERVER_ID
    contract_version = REPOSITORY_OUTCOME_CONTRACT_VERSION

    def __init__(self, reader: RepositoryStatusPort, artifacts: ArtifactStore) -> None:
        self._reader = reader
        self._artifacts = artifacts

    def observe(self, command: ObserveOutcome) -> OutcomeObservation:
        unsupported = tuple(
            target.key for target in command.targets if target.key not in _SUPPORTED_FACTS
        )
        if unsupported:
            raise LookupError(f"repository outcome target is not supported: {unsupported!r}")
        snapshot = self._reader.read()
        reference = self._artifacts.put_json(
            snapshot.manifest(schema_version=REPOSITORY_STATUS_CONTRACT_VERSION)
        )
        claims = tuple(
            OutcomeClaim(
                claim_id=new_event_id(),
                subject=target.subject,
                predicate=target.predicate,
                value=snapshot.value_for(target.subject, target.predicate),
            )
            for target in command.targets
        )
        return OutcomeObservation(
            observation_id=new_event_id(),
            binding=command.binding,
            evaluation_spec_id=command.evaluation_spec_id,
            domain=command.domain,
            stream_id=command.stream_id,
            observer_id=self.observer_id,
            observer_contract_version=self.contract_version,
            status=OutcomeObservationStatus.OBSERVED,
            observed_at=snapshot.observed_at,
            claims=claims,
            evidence=(
                OutcomeEvidencePointer(artifact_id=reference.digest, digest=reference.digest),
            ),
        )


class RepositoryRecordedModelAdapter:
    """Derive the fixed baseline proposal from the exact admitted ModelRequest."""

    adapter_id = REPOSITORY_MODEL_ADAPTER_ID
    capabilities: Set[ModelCapability] = frozenset({ModelCapability.REASON})
    local = True
    deterministic = True

    def invoke(self, request: ModelRequest, *, model_id: str) -> AdapterResult:
        del model_id
        frame_id = _required_text(request.input, "context_frame_id")
        affordances = request.input.get("affordances")
        if not isinstance(affordances, tuple) or not affordances:
            raise ValueError("repository model request requires an affordance")
        first = affordances[0]
        if not isinstance(first, Mapping):
            raise TypeError("repository model affordance must be an object")
        affordance = _required_text(cast("Mapping[str, Any]", first), "name")
        evidence = request.input.get("evidence_event_ids")
        if not isinstance(evidence, tuple) or any(not isinstance(item, str) for item in evidence):
            raise TypeError("repository model evidence ids must be text")
        proposal_id = "proposal:" + hashlib.sha256(request.request_id.encode()).hexdigest()[:24]
        output: dict[str, JsonValue] = {
            "proposal_id": proposal_id,
            "context_frame_id": frame_id,
            "affordance": affordance,
            "arguments": (),
            "rationale": "Run the declared read-only repository inspection.",
            "evidence_event_ids": cast("tuple[JsonValue, ...]", evidence),
        }
        return AdapterResult(
            output=output,
            input_tokens=request.estimated_input_tokens,
            output_tokens=1,
            latency_ms=0,
            cost_microusd=0,
            deterministic=True,
        )


def _validate_invocation(
    invocation: AffordanceInvocation,
    definition: AffordanceDefinition,
) -> None:
    if invocation.affordance != "inspect_repository" or definition.name != invocation.affordance:
        raise ValueError("repository status adapter only supports inspect_repository")
    if invocation.arguments or definition.arguments:
        raise ValueError("inspect_repository does not accept arguments")


def _bounded_text(value: object, maximum: int, stream: str) -> str:
    if not isinstance(value, str):
        raise RepositoryStatusError(f"repository status {stream} is not text")
    if len(value.encode("utf-8")) > maximum:
        raise RepositoryStatusError(f"repository status {stream} exceeds its byte boundary")
    return value


def _required_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"repository model input field {field!r} must be non-empty text")
    return value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("repository adapter clock must return a timezone-aware timestamp")
    return value.astimezone(UTC)


def _validated_git_directory(repo_root: Path) -> Path:
    """Resolve the Git metadata directory for a repository or linked worktree."""

    if not repo_root.is_dir():
        raise ValueError(f"repository root does not exist or is not a directory: {repo_root}")
    marker = repo_root / ".git"
    if marker.is_dir():
        return marker
    if marker.is_file():
        try:
            declaration = marker.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ValueError(f"cannot read Git worktree marker: {marker}") from error
        prefix = "gitdir:"
        if declaration.casefold().startswith(prefix):
            candidate = Path(declaration[len(prefix) :].strip())
            resolved = (
                candidate.resolve()
                if candidate.is_absolute()
                else (repo_root / candidate).resolve()
            )
            if resolved.is_dir():
                return resolved
    raise ValueError(f"repository root is not a Git worktree: {repo_root}")


__all__ = [
    "REPOSITORY_MODEL_ADAPTER_ID",
    "REPOSITORY_OUTCOME_CONTRACT_VERSION",
    "REPOSITORY_OUTCOME_OBSERVER_ID",
    "REPOSITORY_STATUS_ADAPTER_ID",
    "REPOSITORY_STATUS_CONTRACT_VERSION",
    "RepositoryRecordedModelAdapter",
    "RepositoryStatusError",
    "RepositoryStatusExecutionAdapter",
    "RepositoryStatusOutcomeObserver",
    "RepositoryStatusReader",
    "validated_git_directory",
]


validated_git_directory = _validated_git_directory
