"""Production composition for the durable execution repository execution path."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from blackcell.adapters.execution.evidence import ExecutionEvidenceCollector, ExecutionEvidenceError
from blackcell.adapters.execution.text_changes import (
    TextChangeAdmission,
    TextChangeExecutionError,
    TextChangeExecutor,
    text_change_result_payload,
)
from blackcell.adapters.execution.worktree import (
    GitWorktreeLifecycle,
    WorktreeCommitEffect,
    WorktreeExecutionSpec,
    WorktreeLeaseIdentity,
    WorktreeLifecycleError,
    worktree_execution_spec_payload,
    worktree_inspection_payload,
    worktree_removal_payload,
)
from blackcell.adapters.models.change_provider import ChangeProviderError
from blackcell.gateway import (
    DataClassification,
    GatewayAdmissionError,
    GatewayBudget,
    LocalityPolicy,
)
from blackcell.kernel import ArtifactRef, JsonInput, KernelError
from blackcell.kernel._json import canonical_json_bytes
from blackcell.orchestration.acceptance import (
    AcceptanceCommand,
    AcceptanceError,
    AcceptanceFailureCode,
    AcceptanceResult,
    acceptance_command_payload,
    acceptance_result_payload,
)
from blackcell.orchestration.changes import (
    ChangeContractError,
    ChangeProviderCall,
    ChangeProviderResult,
    change_context_payload,
    change_proposal_payload,
    change_provider_result_payload,
)
from blackcell.orchestration.execution_plan import (
    AttemptEvidence,
    FailureClass,
    GoalSpec,
    Plan,
    PlanningRequest,
    PlanningResult,
    PolicyDecision,
    PraxisPromotionCandidate,
    RunLifecycleStatus,
    TaskSpec,
    ToolActionRequest,
)
from blackcell.orchestration.execution_runtime import (
    ExecutionCoordinator,
    ExecutionRunState,
    ExecutionRuntimeError,
)
from blackcell.orchestration.run_lifecycle import provider_request_id

EXECUTION_ATTEMPT_MEDIA_TYPE = "application/vnd.blackcell.execution-attempt+json"
EXECUTION_EVIDENCE_MEDIA_TYPE = "application/vnd.blackcell.execution-evidence+json"


class ExecutionArtifactStorePort(Protocol):
    def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        encoding: str | None = None,
    ) -> ArtifactRef: ...


class ExecutionAcceptancePort(Protocol):
    def run(
        self,
        command: AcceptanceCommand,
        spec: WorktreeExecutionSpec,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> AcceptanceResult: ...


class ExecutionChangeProviderPort(Protocol):
    def propose(self, call: ChangeProviderCall) -> ChangeProviderResult: ...


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    worker_id: str
    classification: DataClassification
    locality: LocalityPolicy
    provider_budget: GatewayBudget
    check_timeout_seconds: int
    stdout_limit_bytes: int
    stderr_limit_bytes: int
    max_changed_paths: int = 256
    remove_successful_worktrees: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.worker_id, str)
            or not self.worker_id.strip()
            or len(self.worker_id) > 120
            or not isinstance(self.classification, DataClassification)
            or not isinstance(self.locality, LocalityPolicy)
            or not isinstance(self.provider_budget, GatewayBudget)
        ):
            raise ValueError("invalid execution execution policy")
        for value, minimum, maximum in (
            (self.check_timeout_seconds, 1, 600),
            (self.stdout_limit_bytes, 1, 16 * 1024 * 1024),
            (self.stderr_limit_bytes, 1, 16 * 1024 * 1024),
            (self.max_changed_paths, 0, 10_000),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError("invalid execution execution policy")
        if not isinstance(self.remove_successful_worktrees, bool):
            raise ValueError("invalid execution execution policy")


class ExecutionError(RuntimeError):
    """A stable content-free failure at the concrete attempt boundary."""

    def __init__(self, code: str = "execution-execution-failed") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ProductionAttemptExecutor:
    """Execute admitted tasks through real worktree, proposal, effect, and sandbox ports."""

    repository_root: Path
    isolation_root: Path
    artifacts: ExecutionArtifactStorePort
    change_provider: ExecutionChangeProviderPort
    acceptance: ExecutionAcceptancePort
    policy: ExecutionPolicy
    worktrees: GitWorktreeLifecycle = field(default_factory=GitWorktreeLifecycle, repr=False)
    evidence: ExecutionEvidenceCollector | None = field(default=None, repr=False)
    changes: TextChangeExecutor | None = field(default=None, repr=False)
    cancel_requested: Callable[[str], bool] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.repository_root, Path) or not isinstance(self.isolation_root, Path):
            raise ValueError("invalid execution execution roots")
        object.__setattr__(self, "repository_root", self.repository_root.resolve(strict=True))
        object.__setattr__(self, "isolation_root", self.isolation_root.resolve(strict=True))
        if self.evidence is None:
            object.__setattr__(self, "evidence", ExecutionEvidenceCollector(self.worktrees))
        if self.changes is None:
            object.__setattr__(self, "changes", TextChangeExecutor(self.worktrees))

    def execute(
        self,
        *,
        run_id: str,
        goal: GoalSpec,
        plan: Plan,
        task: TaskSpec,
        attempt: int,
        workspace_id: str,
        base_commit: str,
        prior_failure_class: FailureClass | None,
        prior_failure_summary: str | None,
        policy_decision: PolicyDecision,
        remaining_budget: GatewayBudget,
    ) -> AttemptEvidence:
        expected_action = ToolActionRequest(
            run_id=run_id,
            plan_id=plan.plan_id,
            task_id=task.task_id,
            attempt=attempt,
            capability="repository-task",
            allowed_paths=task.allowed_paths,
        )
        if not policy_decision.allowed or policy_decision.action_digest != expected_action.digest:
            raise ExecutionError("execution-policy-denied")
        self._require_not_canceled(run_id)
        lease = WorktreeLeaseIdentity(
            run_id=run_id,
            node_id=task.task_id,
            attempt=attempt,
            fencing_token=attempt,
            worker_id=self.policy.worker_id,
        )
        spec = WorktreeExecutionSpec(
            lease=lease,
            repository_root=self.repository_root,
            isolation_root=self.isolation_root,
            base_commit=base_commit,
            allowed_paths=task.allowed_paths,
            max_changed_paths=self.policy.max_changed_paths,
        )
        artifacts: list[str] = []
        semantic: list[dict[str, JsonInput]] = []
        input_tokens: int | None = 0
        output_tokens: int | None = 0
        latency_ms = 0
        cost_microusd: int | None = 0
        try:
            self.worktrees.retain_plan_base_commit(
                self.repository_root,
                plan_id=plan.plan_id,
                base_commit=plan.base_commit,
            )
            self._require_not_canceled(run_id)
            created = self.worktrees.create(spec)
            artifacts.append(
                self._store_json(
                    worktree_execution_spec_payload(spec),
                    media_type="application/vnd.blackcell.worktree-spec+json",
                ).digest
            )
            artifacts.append(
                self._store_json(
                    worktree_inspection_payload(created),
                    media_type="application/vnd.blackcell.worktree-inspection+json",
                ).digest
            )
            commit_effects: tuple[WorktreeCommitEffect, ...] = ()
            if task.allowed_paths:
                self._require_not_canceled(run_id)
                context = self._evidence().collect(
                    spec,
                    objective=task.objective,
                    constraints=_repair_constraints(
                        goal.constraints,
                        prior_failure_class,
                        prior_failure_summary,
                    ),
                )
                context_ref = self._store_json(
                    change_context_payload(context),
                    media_type="application/vnd.blackcell.context+json",
                    expected_digest=context.digest,
                )
                artifacts.append(context_ref.digest)
                semantic.append({"kind": "context", "digest": context_ref.digest})
                call_budget = _intersect_budget(self.policy.provider_budget, remaining_budget)
                input_tokens = None
                output_tokens = None
                cost_microusd = None
                provider_result = self.change_provider.propose(
                    ChangeProviderCall(
                        request_id=provider_request_id(spec.lease.digest),
                        correlation_id=run_id,
                        run_id=run_id,
                        node_id=task.task_id,
                        context=context,
                        classification=self.policy.classification,
                        locality=self.policy.locality,
                        budget=call_budget,
                        estimated_input_tokens=(
                            len(canonical_json_bytes(change_context_payload(context))) + 3
                        )
                        // 4,
                        causation_id=policy_decision.decision_id,
                    )
                )
                self._require_not_canceled(run_id)
                input_tokens = provider_result.input_tokens
                output_tokens = provider_result.output_tokens
                latency_ms = provider_result.latency_ms
                cost_microusd = provider_result.cost_microusd
                if _usage_overdraws_budget(provider_result, call_budget):
                    raise ExecutionError("execution-cumulative-budget-exhausted")
                proposal_ref = self._store_json(
                    change_proposal_payload(provider_result.proposal),
                    media_type="application/vnd.blackcell.proposal+json",
                    expected_digest=provider_result.proposal.digest,
                )
                provider_ref = self._store_json(
                    change_provider_result_payload(provider_result),
                    media_type="application/vnd.blackcell.provider+json",
                )
                artifacts.extend((proposal_ref.digest, provider_ref.digest))
                semantic.append({"kind": "proposal", "digest": proposal_ref.digest})
                effect = self._changes().execute(
                    spec,
                    provider_result.proposal,
                    TextChangeAdmission(
                        worktree_spec_digest=spec.digest,
                        lease_digest=spec.lease.digest,
                        evidence_digest=context.digest,
                        proposal_digest=provider_result.proposal.digest,
                    ),
                )
                effect_ref = self._store_json(
                    text_change_result_payload(effect),
                    media_type="application/vnd.blackcell.effect+json",
                    expected_digest=effect.result_digest,
                )
                artifacts.append(effect_ref.digest)
                commit_effects = tuple(
                    WorktreeCommitEffect(item.path, item.after_digest) for item in effect.effects
                )

            self._require_not_canceled(run_id)
            committed = self.worktrees.commit_changes(spec, effects=commit_effects)
            check_evidence: list[dict[str, JsonInput]] = []
            failed: list[AcceptanceResult] = []
            for check in task.checks:
                self._require_not_canceled(run_id)
                command = AcceptanceCommand(
                    check_id=check.check_id,
                    argv=check.argv,
                    expected_exit_code=check.expected_exit_code,
                    timeout_seconds=self.policy.check_timeout_seconds,
                    stdout_limit_bytes=self.policy.stdout_limit_bytes,
                    stderr_limit_bytes=self.policy.stderr_limit_bytes,
                )
                result = self.acceptance.run(
                    command,
                    spec,
                    cancel_requested=lambda: self._is_canceled(run_id),
                )
                self._require_not_canceled(run_id)
                _validate_check(command, spec, result)
                command_ref = self._store_json(
                    acceptance_command_payload(command),
                    media_type="application/vnd.blackcell.check-command+json",
                    expected_digest=command.digest,
                )
                result_ref = self._store_json(
                    acceptance_result_payload(result),
                    media_type="application/vnd.blackcell.check-result+json",
                    expected_digest=result.digest,
                )
                stdout_ref = self.artifacts.put_bytes(result.stdout.captured)
                stderr_ref = self.artifacts.put_bytes(result.stderr.captured)
                artifacts.extend(
                    (command_ref.digest, result_ref.digest, stdout_ref.digest, stderr_ref.digest)
                )
                check_evidence.append(
                    {
                        "check_id": check.check_id,
                        "return_code": result.return_code,
                        "expected_exit_code": result.expected_exit_code,
                        "passed": result.passed,
                        "stdout_digest": stdout_ref.digest,
                        "stderr_digest": stderr_ref.digest,
                    }
                )
                if not result.passed:
                    failed.append(result)

            clean = self.worktrees.inspect(spec)
            if not clean.clean or not clean.path_policy_compliant:
                return self._failure(
                    spec,
                    run_id=run_id,
                    plan=plan,
                    task=task,
                    attempt=attempt,
                    workspace_id=workspace_id,
                    policy_decision=policy_decision,
                    artifacts=artifacts,
                    semantic=semantic,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    cost_microusd=cost_microusd,
                    error=ExecutionError("execution-workspace-not-clean"),
                )
            if failed:
                failed_ids = ",".join(item.check_id for item in failed)
                return self._finish(
                    spec,
                    run_id=run_id,
                    plan=plan,
                    task=task,
                    attempt=attempt,
                    workspace_id=workspace_id,
                    policy_decision=policy_decision,
                    artifacts=artifacts,
                    semantic=semantic,
                    check_evidence=check_evidence,
                    workspace_clean=True,
                    verifier_exit_code=failed[0].return_code,
                    required_checks_passed=False,
                    failure_class=FailureClass.LOGIC_BUG,
                    failure_summary=f"verification-failed:{failed_ids}",
                    head_commit=committed.head_commit,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    cost_microusd=cost_microusd,
                )

            if self.policy.remove_successful_worktrees:
                removal = self.worktrees.remove_success(
                    spec,
                    expected_head_commit=committed.head_commit,
                )
                artifacts.append(
                    self._store_json(
                        worktree_removal_payload(removal),
                        media_type="application/vnd.blackcell.worktree-removal+json",
                    ).digest
                )
            return self._finish(
                spec,
                run_id=run_id,
                plan=plan,
                task=task,
                attempt=attempt,
                workspace_id=workspace_id,
                policy_decision=policy_decision,
                artifacts=artifacts,
                semantic=semantic,
                check_evidence=check_evidence,
                workspace_clean=True,
                verifier_exit_code=0,
                required_checks_passed=True,
                failure_class=None,
                failure_summary=None,
                head_commit=committed.head_commit,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                cost_microusd=cost_microusd,
            )
        except (KernelError, OSError) as error:
            raise ExecutionError("execution-artifact-or-io-failed") from error
        except Exception as error:
            return self._failure(
                spec,
                run_id=run_id,
                plan=plan,
                task=task,
                attempt=attempt,
                workspace_id=workspace_id,
                policy_decision=policy_decision,
                artifacts=artifacts,
                semantic=semantic,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                cost_microusd=cost_microusd,
                error=error,
            )

    def _failure(
        self,
        spec: WorktreeExecutionSpec,
        *,
        run_id: str,
        plan: Plan,
        task: TaskSpec,
        attempt: int,
        workspace_id: str,
        policy_decision: PolicyDecision,
        artifacts: list[str],
        semantic: list[dict[str, JsonInput]],
        input_tokens: int | None,
        output_tokens: int | None,
        latency_ms: int,
        cost_microusd: int | None,
        error: Exception,
    ) -> AttemptEvidence:
        try:
            inspection = self.worktrees.retain(spec)
        except Exception as inspection_error:
            raise ExecutionError("execution-workspace-evidence-unavailable") from inspection_error
        failure_class, summary = _classify_failure(error)
        return self._finish(
            spec,
            run_id=run_id,
            plan=plan,
            task=task,
            attempt=attempt,
            workspace_id=workspace_id,
            policy_decision=policy_decision,
            artifacts=artifacts,
            semantic=semantic,
            check_evidence=[],
            workspace_clean=inspection.clean and inspection.path_policy_compliant,
            verifier_exit_code=1,
            required_checks_passed=False,
            failure_class=failure_class,
            failure_summary=summary,
            head_commit=inspection.head_commit,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            cost_microusd=cost_microusd,
        )

    def _finish(
        self,
        spec: WorktreeExecutionSpec,
        *,
        run_id: str,
        plan: Plan,
        task: TaskSpec,
        attempt: int,
        workspace_id: str,
        policy_decision: PolicyDecision,
        artifacts: list[str],
        semantic: list[dict[str, JsonInput]],
        check_evidence: list[dict[str, JsonInput]],
        workspace_clean: bool,
        verifier_exit_code: int,
        required_checks_passed: bool,
        failure_class: FailureClass | None,
        failure_summary: str | None,
        head_commit: str,
        input_tokens: int | None,
        output_tokens: int | None,
        latency_ms: int,
        cost_microusd: int | None,
    ) -> AttemptEvidence:
        semantic_payload: dict[str, JsonInput] = {
            "schema_version": "blackcell.execution-evidence/v1",
            "workspace_clean": workspace_clean,
            "verifier_exit_code": verifier_exit_code,
            "required_checks_passed": required_checks_passed,
            "failure_class": None if failure_class is None else failure_class.value,
            "failure_summary": failure_summary,
            "head_commit": head_commit,
            "inputs": semantic,
            "checks": check_evidence,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": latency_ms,
                "cost_microusd": cost_microusd,
            },
        }
        semantic_ref = self._store_json(
            semantic_payload,
            media_type=EXECUTION_EVIDENCE_MEDIA_TYPE,
        )
        artifacts.append(semantic_ref.digest)
        record_ref = self._store_json(
            {
                "schema_version": "blackcell.execution-attempt/v1",
                "run_id": run_id,
                "plan_id": plan.plan_id,
                "plan_version": plan.plan_revision,
                "task_id": task.task_id,
                "attempt": attempt,
                "workspace_id": workspace_id,
                "worktree_spec_digest": spec.digest,
                "policy_decision_id": policy_decision.decision_id,
                "base_commit": spec.base_commit,
                "head_commit": head_commit,
                "evidence_digest": semantic_ref.digest,
                "artifact_digests": sorted(set(artifacts)),
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "latency_ms": latency_ms,
                    "cost_microusd": cost_microusd,
                },
            },
            media_type=EXECUTION_ATTEMPT_MEDIA_TYPE,
        )
        artifacts.append(record_ref.digest)
        return AttemptEvidence(
            workspace_clean=workspace_clean,
            verifier_exit_code=verifier_exit_code,
            required_checks_passed=required_checks_passed,
            failure_class=failure_class,
            failure_summary=failure_summary,
            artifact_digests=tuple(artifacts),
            progress_digests=(semantic_ref.digest,),
            head_commit=head_commit,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            cost_microusd=cost_microusd,
        )

    def _store_json(
        self,
        payload: Mapping[str, JsonInput],
        *,
        media_type: str,
        expected_digest: str | None = None,
    ) -> ArtifactRef:
        reference = self.artifacts.put_bytes(
            canonical_json_bytes(dict(payload)),
            media_type=media_type,
            encoding="utf-8",
        )
        if expected_digest is not None and reference.digest != expected_digest:
            raise ExecutionError("execution-artifact-digest-mismatch")
        return reference

    def _evidence(self) -> ExecutionEvidenceCollector:
        if self.evidence is None:  # pragma: no cover - established in __post_init__
            raise ExecutionError()
        return self.evidence

    def _changes(self) -> TextChangeExecutor:
        if self.changes is None:  # pragma: no cover - established in __post_init__
            raise ExecutionError()
        return self.changes

    def _is_canceled(self, run_id: str) -> bool:
        return self.cancel_requested is not None and self.cancel_requested(run_id)

    def _require_not_canceled(self, run_id: str) -> None:
        if self._is_canceled(run_id):
            raise ExecutionError("execution-canceled")


@dataclass(frozen=True, slots=True)
class ExecutionRunResult:
    plan: Plan
    planning: PlanningResult
    state: ExecutionRunState
    promotion_candidate: PraxisPromotionCandidate | None


@dataclass(frozen=True, slots=True)
class ProductionExecution:
    """Application service that consumes the coordinator in the production process graph."""

    coordinator: ExecutionCoordinator

    def process(self, request: PlanningRequest, *, actor: str) -> ExecutionRunState:
        """Start or safely resume one public generated-plan run."""

        try:
            state = self.coordinator.journal.rehydrate(request.run_id)
        except ExecutionRuntimeError as error:
            if error.code != "execution-run-not-found":
                raise
            plan, _ = self.coordinator.compile_and_admit(
                request.run_id,
                request,
                actor=actor,
            )
            return self.coordinator.execute(request.run_id, request, plan, actor=actor)
        if (
            state.goal_digest != request.goal.digest
            or state.classification is not request.classification
            or state.locality is not request.locality
            or state.budget != request.budget
        ):
            raise ExecutionRuntimeError("execution-run-binding-mismatch")
        if state.status in {
            RunLifecycleStatus.SUCCEEDED,
            RunLifecycleStatus.BLOCKED,
            RunLifecycleStatus.CANCELED,
            RunLifecycleStatus.ESCALATED,
            RunLifecycleStatus.TERMINAL_FAILURE,
        }:
            return state
        if state.status is RunLifecycleStatus.REPLANNING or state.plan_id is None:
            return self.coordinator.reconcile_incomplete_planning(request.run_id, actor=actor)
        plan = self.coordinator.journal.plan(request.run_id)
        if state.status is RunLifecycleStatus.REPLAN_REQUIRED:
            if plan.plan_revision >= 2:
                return self.coordinator.exhaust_replan_budget(request.run_id, actor=actor)
            plan, _ = self.coordinator.compile_and_admit(
                request.run_id,
                request,
                actor=actor,
                previous=plan,
            )
        return self.coordinator.execute(request.run_id, request, plan, actor=actor)

    def run(
        self,
        request: PlanningRequest,
        *,
        actor: str,
        previous: Plan | None = None,
    ) -> ExecutionRunResult:
        plan, planning = self.coordinator.compile_and_admit(
            request.run_id,
            request,
            actor=actor,
            previous=previous,
        )
        state = self.coordinator.execute(
            request.run_id,
            request,
            plan,
            actor=actor,
        )
        return ExecutionRunResult(
            plan=plan,
            planning=planning,
            state=state,
            promotion_candidate=self.coordinator.journal.promotion_candidate(request.run_id),
        )


def _repair_constraints(
    constraints: tuple[str, ...],
    failure_class: FailureClass | None,
    failure_summary: str | None,
) -> tuple[str, ...]:
    if failure_class is None or failure_summary is None:
        return constraints
    repair = f"prior-attempt:{failure_class.value}:{failure_summary}"
    if len(repair.encode("utf-8")) > 2 * 1024 or repair in constraints:
        return constraints
    if len(constraints) >= 64:
        return constraints
    return (*constraints, repair)


def _intersect_budget(left: GatewayBudget, right: GatewayBudget) -> GatewayBudget:
    return GatewayBudget(
        min(left.max_input_tokens, right.max_input_tokens),
        min(left.max_output_tokens, right.max_output_tokens),
        min(left.max_latency_ms, right.max_latency_ms),
        min(left.max_cost_microusd, right.max_cost_microusd),
    )


def _usage_overdraws_budget(
    result: ChangeProviderResult,
    budget: GatewayBudget,
) -> bool:
    return (
        (result.input_tokens is not None and result.input_tokens > budget.max_input_tokens)
        or (result.output_tokens is not None and result.output_tokens > budget.max_output_tokens)
        or result.latency_ms > budget.max_latency_ms
        or (result.cost_microusd is not None and result.cost_microusd > budget.max_cost_microusd)
    )


def _validate_check(
    command: AcceptanceCommand,
    spec: WorktreeExecutionSpec,
    result: AcceptanceResult,
) -> None:
    if (
        not isinstance(result, AcceptanceResult)
        or result.check_id != command.check_id
        or result.command_digest != command.digest
        or result.worktree_spec_digest != spec.digest
        or result.expected_exit_code != command.expected_exit_code
    ):
        raise ExecutionError("invalid-execution-acceptance-result")


def _classify_failure(error: Exception) -> tuple[FailureClass, str]:
    code = _error_code(error)
    if isinstance(error, (ChangeProviderError, ChangeContractError, TextChangeExecutionError)):
        return FailureClass.CONTRACT_MISMATCH, code
    if isinstance(error, ExecutionEvidenceError):
        return FailureClass.INVALID_ASSUMPTION, code
    if isinstance(error, GatewayAdmissionError):
        return FailureClass.POLICY, code
    if isinstance(error, AcceptanceError):
        if error.code in {
            AcceptanceFailureCode.INVALID_COMMAND,
            AcceptanceFailureCode.INVALID_POLICY,
            AcceptanceFailureCode.EXECUTABLE_NOT_ALLOWED,
            AcceptanceFailureCode.WORKTREE_POLICY_VIOLATION,
        }:
            return FailureClass.POLICY, code
        return FailureClass.TRANSIENT, code
    if isinstance(error, WorktreeLifecycleError):
        return FailureClass.POLICY, code
    if isinstance(error, ExecutionError):
        return FailureClass.POLICY, code
    return FailureClass.UNCLASSIFIED, code


def _error_code(error: Exception) -> str:
    value = getattr(error, "code", None)
    text = getattr(value, "value", value)
    if isinstance(text, str) and text and len(text) <= 128:
        return text
    return "execution-unclassified-failure"


__all__ = [
    "EXECUTION_ATTEMPT_MEDIA_TYPE",
    "EXECUTION_EVIDENCE_MEDIA_TYPE",
    "ExecutionError",
    "ExecutionPolicy",
    "ExecutionRunResult",
    "ProductionAttemptExecutor",
    "ProductionExecution",
]
