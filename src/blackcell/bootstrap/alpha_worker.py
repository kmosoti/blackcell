"""Bootstrap coordinator joining alpha runtime, provider, effects, isolation, and artifacts."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol

from blackcell.adapters.execution.bubblewrap import BUBBLEWRAP_ACCEPTANCE_PROBE_TIMEOUT_SECONDS
from blackcell.adapters.execution.evidence import AlphaEvidenceCollector, AlphaEvidenceError
from blackcell.adapters.execution.text_changes import (
    TextChangeAdmission,
    TextChangeExecutionError,
    TextChangeExecutionResult,
    TextChangeExecutor,
    text_change_result_payload,
)
from blackcell.adapters.execution.worktree import (
    GIT_WORKTREE_COMMAND_TIMEOUT_SECONDS,
    MAX_GIT_WORKTREE_COMMIT_COMMANDS,
    MAX_GIT_WORKTREE_CREATE_COMMANDS,
    MAX_GIT_WORKTREE_INSPECT_COMMANDS,
    GitWorktreeLifecycle,
    WorktreeCommitEffect,
    WorktreeExecutionSpec,
    WorktreeLifecycleError,
)
from blackcell.adapters.models.alpha_change_provider import AlphaChangeProviderError
from blackcell.bootstrap.alpha_runtime import AlphaPreparedNode, AlphaReadyNode
from blackcell.gateway import (
    DataClassification,
    GatewayAdmissionError,
    GatewayBudget,
    LocalityPolicy,
)
from blackcell.interfaces.http import AlphaRunResponse, RuntimeApiError
from blackcell.kernel import ArtifactRef, JsonInput, KernelError, utc_now
from blackcell.kernel._json import canonical_json_bytes
from blackcell.orchestration.alpha_acceptance import (
    MAX_ALPHA_ACCEPTANCE_STREAM_BYTES,
    AlphaAcceptanceCommand,
    AlphaAcceptanceError,
    AlphaAcceptanceFailureCode,
    AlphaAcceptanceResult,
    alpha_acceptance_command_payload,
    alpha_acceptance_result_payload,
)
from blackcell.orchestration.alpha_artifacts import (
    ALPHA_ACCEPTANCE_COMMAND_MEDIA_TYPE,
    ALPHA_ACCEPTANCE_RESULT_MEDIA_TYPE,
    ALPHA_CONTEXT_MEDIA_TYPE,
    ALPHA_EFFECT_MEDIA_TYPE,
    ALPHA_NODE_OUTCOME_SCHEMA,
    ALPHA_OUTCOME_MEDIA_TYPE,
    ALPHA_PROPOSAL_MEDIA_TYPE,
    ALPHA_PROVIDER_MEDIA_TYPE,
    AlphaArtifactLink,
    AlphaCheckArtifacts,
    AlphaNodeOutcomeManifest,
    alpha_node_outcome_payload,
)
from blackcell.orchestration.alpha_changes import (
    AlphaChangeContractError,
    AlphaChangeProviderCall,
    AlphaChangeProviderResult,
    alpha_change_context_payload,
    alpha_change_proposal_payload,
    alpha_change_provider_result_payload,
)
from blackcell.orchestration.alpha_lifecycle import alpha_provider_request_id

_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
# Every node creates a worktree, takes the conservative dirty-commit path, and performs one
# terminal inspection. Writers additionally inspect during evidence collection and twice around
# admitted text effects. Each acceptance check performs a before/after inspection of its own.
MAX_ALPHA_WORKER_READ_BASE_WORKTREE_SECONDS = GIT_WORKTREE_COMMAND_TIMEOUT_SECONDS * (
    MAX_GIT_WORKTREE_CREATE_COMMANDS
    + MAX_GIT_WORKTREE_COMMIT_COMMANDS
    + MAX_GIT_WORKTREE_INSPECT_COMMANDS
)
MAX_ALPHA_WORKER_WRITE_BASE_WORKTREE_SECONDS = (
    MAX_ALPHA_WORKER_READ_BASE_WORKTREE_SECONDS
    + 3 * GIT_WORKTREE_COMMAND_TIMEOUT_SECONDS * MAX_GIT_WORKTREE_INSPECT_COMMANDS
)
MAX_ALPHA_WORKER_ACCEPTANCE_WORKTREE_SECONDS = (
    2 * GIT_WORKTREE_COMMAND_TIMEOUT_SECONDS * MAX_GIT_WORKTREE_INSPECT_COMMANDS
)


class AlphaWorkerFailureCode(StrEnum):
    CHECK_FAILED = "alpha-acceptance-check-failed"
    INVALID_ACCEPTANCE_RESULT = "invalid-alpha-acceptance-result"
    PROVIDER_FAILED = "alpha-change-provider-failed"
    ARTIFACT_FAILED = "alpha-worker-artifact-failed"
    UNEXPECTED = "alpha-worker-unexpected-failure"


class AlphaWorkerError(RuntimeError):
    """A stable worker failure that never includes provider, project, or stream content."""

    def __init__(self, code: AlphaWorkerFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class AlphaChangeProviderPort(Protocol):
    def propose(self, call: AlphaChangeProviderCall) -> AlphaChangeProviderResult: ...


class AlphaAcceptanceRunnerPort(Protocol):
    def run(
        self,
        command: AlphaAcceptanceCommand,
        spec: WorktreeExecutionSpec,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> AlphaAcceptanceResult: ...


class AlphaArtifactStorePort(Protocol):
    def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        encoding: str | None = None,
    ) -> ArtifactRef: ...


class AlphaRuntimeWorkerPort(Protocol):
    def next_ready_node(self) -> AlphaReadyNode | None: ...

    def prepare_node(
        self,
        run_id: str,
        node_id: str,
        *,
        worker_id: str,
        lease_expires_at: datetime,
    ) -> AlphaPreparedNode: ...

    def should_cancel_node(self, spec: WorktreeExecutionSpec) -> bool: ...

    def record_provider_dispatch(
        self,
        spec: WorktreeExecutionSpec,
        *,
        provider_request_id: str,
        context_digest: str,
        context_artifact_digest: str,
        principal_id: str,
    ) -> str: ...

    def acknowledge_cancellation(
        self,
        spec: WorktreeExecutionSpec,
        *,
        result_digest: str | None,
        principal_id: str,
    ) -> AlphaRunResponse: ...

    def record_node_success(
        self,
        spec: WorktreeExecutionSpec,
        *,
        result_digest: str,
        principal_id: str,
    ) -> AlphaRunResponse: ...

    def record_node_failure(
        self,
        spec: WorktreeExecutionSpec,
        *,
        failure_code: str,
        result_digest: str | None,
        principal_id: str,
    ) -> AlphaRunResponse: ...


@dataclass(frozen=True, slots=True)
class AlphaWorkerPolicy:
    worker_id: str
    classification: DataClassification = DataClassification.PRIVATE
    locality: LocalityPolicy = LocalityPolicy.LOCAL_ONLY
    stdout_limit_bytes: int = 1024 * 1024
    stderr_limit_bytes: int = 1024 * 1024
    lease_grace_seconds: int = 30

    def __post_init__(self) -> None:
        if (
            not isinstance(self.worker_id, str)
            or not self.worker_id
            or len(self.worker_id) > 120
            or any(not 0x21 <= ord(character) <= 0x7E for character in self.worker_id)
            or not isinstance(self.classification, DataClassification)
            or not isinstance(self.locality, LocalityPolicy)
        ):
            raise ValueError("invalid alpha worker policy")
        for limit in (self.stdout_limit_bytes, self.stderr_limit_bytes):
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 1 <= limit <= MAX_ALPHA_ACCEPTANCE_STREAM_BYTES
            ):
                raise ValueError("invalid alpha worker policy")
        if (
            isinstance(self.lease_grace_seconds, bool)
            or not isinstance(self.lease_grace_seconds, int)
            or not 1 <= self.lease_grace_seconds <= 3_600
        ):
            raise ValueError("invalid alpha worker policy")


@dataclass(frozen=True, slots=True)
class AlphaWorkerCycleResult:
    status: Literal["idle", "node-succeeded", "node-failed", "node-canceled", "claim-conflict"]
    run_id: str | None = None
    node_id: str | None = None
    run_status: str | None = None
    outcome_artifact_digest: str | None = None
    failure_code: str | None = None


@dataclass(slots=True)
class _StageArtifacts:
    context: AlphaArtifactLink | None = None
    proposal: AlphaArtifactLink | None = None
    provider: AlphaArtifactLink | None = None
    effect: AlphaArtifactLink | None = None
    checks: list[AlphaCheckArtifacts] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AlphaRuntimeWorker:
    runtime: AlphaRuntimeWorkerPort
    artifacts: AlphaArtifactStorePort
    provider: AlphaChangeProviderPort
    change_executor: TextChangeExecutor
    acceptance: AlphaAcceptanceRunnerPort
    worktrees: GitWorktreeLifecycle = field(default_factory=GitWorktreeLifecycle, repr=False)
    evidence: AlphaEvidenceCollector = field(default_factory=AlphaEvidenceCollector, repr=False)
    policy: AlphaWorkerPolicy = field(default_factory=lambda: AlphaWorkerPolicy("alpha-worker"))

    def run_once(self) -> AlphaWorkerCycleResult:
        ready = self.runtime.next_ready_node()
        if ready is None:
            return AlphaWorkerCycleResult(status="idle")
        bounded_timeout_stages = len(ready.node.checks) + int(
            "repository-write" in ready.node.effects
        )
        acceptance_check_count = len(ready.node.checks)
        base_worktree_seconds = (
            MAX_ALPHA_WORKER_WRITE_BASE_WORKTREE_SECONDS
            if "repository-write" in ready.node.effects
            else MAX_ALPHA_WORKER_READ_BASE_WORKTREE_SECONDS
        )
        worktree_seconds = (
            base_worktree_seconds
            + acceptance_check_count * MAX_ALPHA_WORKER_ACCEPTANCE_WORKTREE_SECONDS
        )
        expires_at = utc_now() + timedelta(
            seconds=(
                ready.node.budget.timeout_seconds * bounded_timeout_stages
                + worktree_seconds
                + acceptance_check_count * BUBBLEWRAP_ACCEPTANCE_PROBE_TIMEOUT_SECONDS
                + self.policy.lease_grace_seconds
            )
        )
        try:
            prepared = self.runtime.prepare_node(
                ready.run_id,
                ready.node.node_id,
                worker_id=self.policy.worker_id,
                lease_expires_at=expires_at,
            )
        except RuntimeApiError:
            return AlphaWorkerCycleResult(
                status="claim-conflict",
                run_id=ready.run_id,
                node_id=ready.node.node_id,
            )
        return self._execute(prepared)

    def _execute(self, prepared: AlphaPreparedNode) -> AlphaWorkerCycleResult:
        stages = _StageArtifacts()
        spec = prepared.spec
        committed_head: str | None = None
        commit_effects: tuple[WorktreeCommitEffect, ...] = ()
        phase = "execution"
        try:
            self._require_active(spec)
            if "repository-write" in prepared.node.effects:
                context = self.evidence.collect(
                    spec,
                    objective=prepared.node.objective,
                    constraints=prepared.intent.constraints,
                )
                stages.context = self._store_json(
                    alpha_change_context_payload(context),
                    media_type=ALPHA_CONTEXT_MEDIA_TYPE,
                    expected_digest=context.digest,
                )
                phase = "provider"
                self._require_active(spec)
                request_id = alpha_provider_request_id(spec.lease.digest)
                dispatch_event_id = self.runtime.record_provider_dispatch(
                    spec,
                    provider_request_id=request_id,
                    context_digest=context.digest,
                    context_artifact_digest=stages.context.digest,
                    principal_id=self.policy.worker_id,
                )
                provider_result = self.provider.propose(
                    AlphaChangeProviderCall(
                        request_id=request_id,
                        correlation_id=prepared.correlation_id,
                        run_id=spec.lease.run_id,
                        node_id=spec.lease.node_id,
                        context=context,
                        classification=self.policy.classification,
                        locality=self.policy.locality,
                        budget=GatewayBudget(
                            prepared.node.budget.max_input_tokens,
                            prepared.node.budget.max_output_tokens,
                            prepared.node.budget.timeout_seconds * 1_000,
                            prepared.node.budget.max_cost_microusd,
                        ),
                        estimated_input_tokens=(
                            len(canonical_json_bytes(alpha_change_context_payload(context))) + 3
                        )
                        // 4,
                        causation_id=dispatch_event_id,
                    )
                )
                phase = "execution"
                stages.proposal = self._store_json(
                    alpha_change_proposal_payload(provider_result.proposal),
                    media_type=ALPHA_PROPOSAL_MEDIA_TYPE,
                    expected_digest=provider_result.proposal.digest,
                )
                stages.provider = self._store_json(
                    alpha_change_provider_result_payload(provider_result),
                    media_type=ALPHA_PROVIDER_MEDIA_TYPE,
                )
                self._require_active(spec)
                effect_result = self.change_executor.execute(
                    spec,
                    provider_result.proposal,
                    TextChangeAdmission(
                        worktree_spec_digest=spec.digest,
                        lease_digest=spec.lease.digest,
                        evidence_digest=context.digest,
                        proposal_digest=provider_result.proposal.digest,
                    ),
                )
                stages.effect = self._store_effect(effect_result)
                commit_effects = tuple(
                    WorktreeCommitEffect(
                        path=effect.path,
                        after_digest=effect.after_digest,
                    )
                    for effect in effect_result.effects
                )
                self._require_active(spec)

            committed = self.worktrees.commit_changes(spec, effects=commit_effects)
            committed_head = committed.head_commit
            for check in prepared.node.checks:
                command = AlphaAcceptanceCommand(
                    check_id=check.check_id,
                    argv=check.argv,
                    expected_exit_code=check.expected_exit_code,
                    timeout_seconds=prepared.node.budget.timeout_seconds,
                    stdout_limit_bytes=self.policy.stdout_limit_bytes,
                    stderr_limit_bytes=self.policy.stderr_limit_bytes,
                )
                result = self.acceptance.run(
                    command,
                    spec,
                    cancel_requested=lambda: self.runtime.should_cancel_node(spec),
                )
                self._validate_acceptance_result(command, spec, result)
                stages.checks.append(self._store_check(command, result))
                if not result.passed:
                    raise AlphaWorkerError(AlphaWorkerFailureCode.CHECK_FAILED)
            self._require_active(spec)
            outcome = self._store_outcome(
                prepared,
                stages,
                status="succeeded",
                head_commit=committed_head,
                failure_code=None,
            )
            self._require_active(spec)
            response = self.runtime.record_node_success(
                spec,
                result_digest=outcome.digest,
                principal_id=self.policy.worker_id,
            )
            return AlphaWorkerCycleResult(
                status="node-succeeded",
                run_id=spec.lease.run_id,
                node_id=spec.lease.node_id,
                run_status=response.status,
                outcome_artifact_digest=outcome.digest,
            )
        except RuntimeApiError:
            if self.runtime.should_cancel_node(spec):
                return self._acknowledge_cancellation(
                    prepared,
                    stages,
                    head_commit=committed_head,
                )
            return AlphaWorkerCycleResult(
                status="claim-conflict",
                run_id=spec.lease.run_id,
                node_id=spec.lease.node_id,
            )
        except Exception as error:
            if self.runtime.should_cancel_node(spec):
                return self._acknowledge_cancellation(
                    prepared,
                    stages,
                    head_commit=committed_head,
                )
            failure_code = _failure_code(error, phase=phase)
            outcome_digest: str | None = None
            try:
                outcome_digest = self._store_outcome(
                    prepared,
                    stages,
                    status="failed",
                    head_commit=committed_head,
                    failure_code=failure_code,
                ).digest
            except Exception:
                failure_code = AlphaWorkerFailureCode.ARTIFACT_FAILED.value
            response = self.runtime.record_node_failure(
                spec,
                failure_code=failure_code,
                result_digest=outcome_digest,
                principal_id=self.policy.worker_id,
            )
            return AlphaWorkerCycleResult(
                status="node-failed",
                run_id=spec.lease.run_id,
                node_id=spec.lease.node_id,
                run_status=response.status,
                outcome_artifact_digest=outcome_digest,
                failure_code=failure_code,
            )

    def _acknowledge_cancellation(
        self,
        prepared: AlphaPreparedNode,
        stages: _StageArtifacts,
        *,
        head_commit: str | None,
    ) -> AlphaWorkerCycleResult:
        outcome_digest: str | None = None
        with suppress(Exception):
            outcome_digest = self._store_outcome(
                prepared,
                stages,
                status="canceled",
                head_commit=head_commit,
                failure_code=None,
            ).digest
        try:
            response = self.runtime.acknowledge_cancellation(
                prepared.spec,
                result_digest=outcome_digest,
                principal_id=self.policy.worker_id,
            )
        except RuntimeApiError:
            return AlphaWorkerCycleResult(
                status="claim-conflict",
                run_id=prepared.spec.lease.run_id,
                node_id=prepared.spec.lease.node_id,
            )
        return AlphaWorkerCycleResult(
            status="node-canceled",
            run_id=prepared.spec.lease.run_id,
            node_id=prepared.spec.lease.node_id,
            run_status=response.status,
            outcome_artifact_digest=outcome_digest,
        )

    def _require_active(self, spec: WorktreeExecutionSpec) -> None:
        if self.runtime.should_cancel_node(spec):
            raise AlphaAcceptanceError(AlphaAcceptanceFailureCode.CANCELED)

    def _store_effect(self, result: TextChangeExecutionResult) -> AlphaArtifactLink:
        return self._store_json(
            text_change_result_payload(result),
            media_type=ALPHA_EFFECT_MEDIA_TYPE,
            expected_digest=result.result_digest,
        )

    def _store_check(
        self,
        command: AlphaAcceptanceCommand,
        result: AlphaAcceptanceResult,
    ) -> AlphaCheckArtifacts:
        stdout = self._store_bytes(result.stdout.captured)
        stderr = self._store_bytes(result.stderr.captured)
        command_link = self._store_json(
            alpha_acceptance_command_payload(command),
            media_type=ALPHA_ACCEPTANCE_COMMAND_MEDIA_TYPE,
            expected_digest=command.digest,
        )
        result_link = self._store_json(
            alpha_acceptance_result_payload(result),
            media_type=ALPHA_ACCEPTANCE_RESULT_MEDIA_TYPE,
            expected_digest=result.digest,
        )
        return AlphaCheckArtifacts(
            check_id=result.check_id,
            command_digest=command.digest,
            result_digest=result.digest,
            passed=result.passed,
            command=command_link,
            result=result_link,
            stdout=stdout,
            stderr=stderr,
        )

    def _store_outcome(
        self,
        prepared: AlphaPreparedNode,
        stages: _StageArtifacts,
        *,
        status: Literal["succeeded", "failed", "canceled"],
        head_commit: str | None,
        failure_code: str | None,
    ) -> AlphaArtifactLink:
        if status == "succeeded":
            if _COMMIT.fullmatch(head_commit or "") is None or failure_code is not None:
                raise AlphaWorkerError(AlphaWorkerFailureCode.ARTIFACT_FAILED)
            if len(stages.checks) != len(prepared.node.checks):
                raise AlphaWorkerError(AlphaWorkerFailureCode.ARTIFACT_FAILED)
            if "repository-write" in prepared.node.effects and any(
                link is None
                for link in (stages.context, stages.proposal, stages.provider, stages.effect)
            ):
                raise AlphaWorkerError(AlphaWorkerFailureCode.ARTIFACT_FAILED)
        elif (status == "failed" and failure_code is None) or (
            status == "canceled" and failure_code is not None
        ):
            raise AlphaWorkerError(AlphaWorkerFailureCode.ARTIFACT_FAILED)
        manifest = AlphaNodeOutcomeManifest(
            run_id=prepared.spec.lease.run_id,
            node_id=prepared.spec.lease.node_id,
            attempt=prepared.spec.lease.attempt,
            fencing_token=prepared.spec.lease.fencing_token,
            lease_digest=prepared.spec.lease.digest,
            worktree_spec_digest=prepared.spec.digest,
            base_commit=prepared.spec.base_commit,
            head_commit=head_commit,
            repository_write="repository-write" in prepared.node.effects,
            status=status,
            failure_code=failure_code,
            context_artifact=stages.context,
            proposal_artifact=stages.proposal,
            provider_artifact=stages.provider,
            effect_artifact=stages.effect,
            checks=tuple(stages.checks),
        )
        return self._store_json(
            alpha_node_outcome_payload(manifest),
            media_type=ALPHA_OUTCOME_MEDIA_TYPE,
            expected_digest=manifest.digest,
        )

    def _store_json(
        self,
        payload: Mapping[str, JsonInput],
        *,
        media_type: str,
        expected_digest: str | None = None,
    ) -> AlphaArtifactLink:
        try:
            reference = self.artifacts.put_bytes(
                canonical_json_bytes(dict(payload)),
                media_type=media_type,
                encoding="utf-8",
            )
            if expected_digest is not None and reference.digest != expected_digest:
                raise AlphaWorkerError(AlphaWorkerFailureCode.ARTIFACT_FAILED)
            return AlphaArtifactLink.from_reference(reference)
        except AlphaWorkerError:
            raise
        except Exception as error:
            raise AlphaWorkerError(AlphaWorkerFailureCode.ARTIFACT_FAILED) from error

    def _store_bytes(self, payload: bytes) -> AlphaArtifactLink:
        try:
            return AlphaArtifactLink.from_reference(self.artifacts.put_bytes(payload))
        except Exception as error:
            raise AlphaWorkerError(AlphaWorkerFailureCode.ARTIFACT_FAILED) from error

    @staticmethod
    def _validate_acceptance_result(
        command: AlphaAcceptanceCommand,
        spec: WorktreeExecutionSpec,
        result: AlphaAcceptanceResult,
    ) -> None:
        if (
            not isinstance(result, AlphaAcceptanceResult)
            or result.check_id != command.check_id
            or result.command_digest != command.digest
            or result.worktree_spec_digest != spec.digest
            or result.expected_exit_code != command.expected_exit_code
        ):
            raise AlphaWorkerError(AlphaWorkerFailureCode.INVALID_ACCEPTANCE_RESULT)


def _failure_code(error: Exception, *, phase: str) -> str:
    if isinstance(
        error,
        AlphaWorkerError | AlphaEvidenceError | AlphaChangeProviderError | TextChangeExecutionError,
    ):
        return error.code.value
    if isinstance(error, AlphaAcceptanceError | WorktreeLifecycleError):
        return error.code.value
    if isinstance(error, AlphaChangeContractError):
        return error.code.value
    if isinstance(error, GatewayAdmissionError):
        return error.code.value
    if isinstance(error, KernelError | OSError):
        return AlphaWorkerFailureCode.ARTIFACT_FAILED.value
    if phase == "provider":
        return AlphaWorkerFailureCode.PROVIDER_FAILED.value
    return AlphaWorkerFailureCode.UNEXPECTED.value


__all__ = [
    "ALPHA_NODE_OUTCOME_SCHEMA",
    "MAX_ALPHA_WORKER_ACCEPTANCE_WORKTREE_SECONDS",
    "MAX_ALPHA_WORKER_READ_BASE_WORKTREE_SECONDS",
    "MAX_ALPHA_WORKER_WRITE_BASE_WORKTREE_SECONDS",
    "AlphaAcceptanceRunnerPort",
    "AlphaArtifactLink",
    "AlphaArtifactStorePort",
    "AlphaChangeProviderPort",
    "AlphaCheckArtifacts",
    "AlphaNodeOutcomeManifest",
    "AlphaRuntimeWorker",
    "AlphaRuntimeWorkerPort",
    "AlphaWorkerCycleResult",
    "AlphaWorkerError",
    "AlphaWorkerFailureCode",
    "AlphaWorkerPolicy",
    "alpha_node_outcome_payload",
]
