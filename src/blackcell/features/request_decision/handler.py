from __future__ import annotations

from datetime import datetime

from blackcell.features.request_decision.artifacts import (
    DECISION_FAILURE_SCHEMA_VERSION_V2,
    decode_decision_output,
)
from blackcell.features.request_decision.command import RequestDecision
from blackcell.features.request_decision.errors import (
    DecisionGatewayError,
    DecisionIdentityConflict,
    DecisionOutputViolation,
)
from blackcell.features.request_decision.models import (
    DecisionAdapterResult,
    DecisionAttemptClaim,
    DecisionDiagnosticCode,
    DecisionFailure,
    DecisionFailureDiagnostic,
    DecisionFailureKind,
    DecisionFailureRecord,
    DecisionGatewayCompletion,
    DecisionLocality,
    DecisionPreparation,
    DecisionRequestRecord,
    DecisionResponse,
    DecisionRoute,
    DecisionSuccessRecord,
    DecisionTerminalRecord,
    DecisionUsage,
)
from blackcell.features.request_decision.ports import (
    Clock,
    DecisionAttemptJournal,
    DecisionGatewayPort,
)
from blackcell.kernel import utc_now
from blackcell.kernel._json import json_digest


class RequestDecisionHandler:
    """Prepare, durably bracket, and validate one gateway-owned model decision.

    ``prepare`` and ``acquire`` never invoke a model. A workflow can therefore
    durably record the request, route, and fenced attempt before calling
    ``invoke``. ``handle`` preserves the original combined compatibility path.
    """

    def __init__(
        self,
        gateway: DecisionGatewayPort,
        journal: DecisionAttemptJournal,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._gateway = gateway
        self._journal = journal
        self._clock = clock

    def prepare(
        self,
        command: RequestDecision,
    ) -> DecisionPreparation | DecisionTerminalRecord:
        request_record = self._journal.register(
            command,
            registered_at=self._now(),
        )
        resumed = self._journal.resume(request_record)
        if resumed is not None:
            return resumed
        try:
            route = self._gateway.route(command)
            _validate_route(command, route)
        except DecisionGatewayError as error:
            return self._reject(
                request_record,
                _gateway_failure(command, error, failed_at=self._now()),
            )
        except (TypeError, ValueError) as error:
            failure = DecisionFailure(
                request_id=command.request_id,
                request_digest=command.request_digest,
                kind=DecisionFailureKind.INTEGRITY,
                code="gateway_route_contract_invalid",
                retryable=False,
                failed_at=self._now(),
                exception_type=type(error).__name__,
            )
            return self._reject(request_record, failure)
        return self._record_route(request_record, route)

    def _record_route(
        self,
        request: DecisionRequestRecord,
        route: DecisionRoute,
    ) -> DecisionPreparation | DecisionTerminalRecord:
        try:
            return self._journal.record_route(
                request,
                route,
                recorded_at=self._now(),
            )
        except DecisionIdentityConflict:
            resumed = self._journal.resume(request)
            if resumed is None:
                raise
            return resumed

    def _reject(
        self,
        request: DecisionRequestRecord,
        failure: DecisionFailure,
    ) -> DecisionPreparation | DecisionTerminalRecord:
        try:
            return self._journal.reject(
                request,
                failure,
                recorded_at=self._now(),
            )
        except DecisionIdentityConflict:
            resumed = self._journal.resume(request)
            if resumed is None:
                raise
            return resumed

    def handle(
        self,
        preparation: DecisionPreparation,
    ) -> DecisionSuccessRecord | DecisionFailureRecord:
        acquired = self.acquire(preparation)
        if isinstance(acquired, DecisionSuccessRecord | DecisionFailureRecord):
            return acquired
        return self.invoke(preparation, acquired)

    def acquire(
        self,
        preparation: DecisionPreparation,
    ) -> DecisionAttemptClaim | DecisionTerminalRecord:
        """Durably fence an attempt without crossing the live gateway boundary."""

        return self._journal.acquire(preparation, acquired_at=self._now())

    def invoke(
        self,
        preparation: DecisionPreparation,
        claim: DecisionAttemptClaim,
    ) -> DecisionSuccessRecord | DecisionFailureRecord:
        """Invoke only the exact request, route, and attempt already durably claimed."""

        _validate_claim(preparation, claim)
        admitted = self._journal.begin_invoke(
            preparation,
            claim,
            invoked_at=self._now(),
        )
        if isinstance(admitted, DecisionSuccessRecord | DecisionFailureRecord):
            return admitted
        claim = admitted
        request = _request(preparation.request_record)
        try:
            adapter_result = self._gateway.invoke(request, preparation.route)
        except DecisionGatewayError as error:
            usage = None
            if error.completion is not None:
                failed_at = max(
                    error.completion.completed_at,
                    claim.attempt_record.attempt.started_at,
                    claim.invoked_at or claim.attempt_record.attempt.started_at,
                )
                usage = _completion_usage(request, claim, error.completion)
            else:
                failed_at = self._now()
                if claim.invoked_at is not None:
                    failed_at = max(failed_at, claim.invoked_at)
            failure = _gateway_failure(
                request,
                error,
                failed_at=failed_at,
                route_id=preparation.route.route_id,
                attempt_id=claim.attempt_record.attempt.attempt_id,
            )
            return self._journal.fail(
                preparation.request_record,
                failure,
                preparation=preparation,
                claim=claim,
                usage=usage,
                recorded_at=self._now(),
            )
        usage = _usage(request, claim, adapter_result)
        failure = _result_failure(request, preparation, claim, adapter_result)
        if failure is not None:
            return self._journal.fail(
                preparation.request_record,
                failure,
                preparation=preparation,
                claim=claim,
                usage=usage,
                recorded_at=self._now(),
            )
        try:
            proposal = decode_decision_output(adapter_result.output, request)
        except DecisionOutputViolation as error:
            failure = _invalid_output_failure(
                request,
                preparation,
                claim,
                adapter_result,
                code=error.code,
                path=error.path,
                exception_type=type(error).__name__,
            )
            return self._journal.fail(
                preparation.request_record,
                failure,
                preparation=preparation,
                claim=claim,
                usage=usage,
                recorded_at=self._now(),
            )
        except (TypeError, ValueError) as error:
            failure = _invalid_output_failure(
                request,
                preparation,
                claim,
                adapter_result,
                code=DecisionDiagnosticCode.INVALID_STRUCTURE,
                path="$",
                exception_type=type(error).__name__,
            )
            return self._journal.fail(
                preparation.request_record,
                failure,
                preparation=preparation,
                claim=claim,
                usage=usage,
                recorded_at=self._now(),
            )
        response = DecisionResponse(
            request_id=request.request_id,
            request_digest=request.request_digest,
            route_id=preparation.route.route_id,
            attempt_id=claim.attempt_record.attempt.attempt_id,
            proposal=proposal,
            completed_at=adapter_result.completed_at,
        )
        return self._journal.succeed(
            preparation,
            claim,
            response,
            usage,
            recorded_at=self._now(),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decision handler clock must return a timezone-aware timestamp")
        return value


def _validate_route(request: RequestDecision, route: DecisionRoute) -> None:
    if route.capability is not request.capability:
        raise ValueError("gateway route capability does not match the decision request")
    if request.locality is DecisionLocality.LOCAL_ONLY and not route.local:
        raise ValueError("gateway selected a remote route for a local-only decision request")
    if request.deterministic_required and not route.deterministic:
        raise ValueError("gateway selected a non-deterministic route for a deterministic request")


def _validate_claim(
    preparation: DecisionPreparation,
    claim: DecisionAttemptClaim,
) -> None:
    request = preparation.request_record.request
    attempt = claim.attempt_record.attempt
    if (
        attempt.request_id != request.request_id
        or attempt.request_digest != request.request_digest
        or attempt.route_id != preparation.route.route_id
        or attempt.started_at < preparation.prepared_at
    ):
        raise ValueError("decision attempt claim does not match its preparation")


def _result_failure(
    request: RequestDecision,
    preparation: DecisionPreparation,
    claim: DecisionAttemptClaim,
    result: DecisionAdapterResult,
) -> DecisionFailure | None:
    attempt = claim.attempt_record.attempt
    kind: DecisionFailureKind | None = None
    code = ""
    if result.completed_at < attempt.started_at:
        kind = DecisionFailureKind.INTEGRITY
        code = "decision_completion_precedes_attempt"
    elif claim.invoked_at is None:
        kind = DecisionFailureKind.INTEGRITY
        code = "decision_invocation_admission_missing"
    elif result.completed_at < claim.invoked_at:
        kind = DecisionFailureKind.INTEGRITY
        code = "decision_completion_precedes_invocation"
    elif result.input_tokens > request.budget.max_input_tokens:
        kind = DecisionFailureKind.BUDGET
        code = "input_token_budget_exceeded"
    elif result.output_tokens > request.budget.max_output_tokens:
        kind = DecisionFailureKind.BUDGET
        code = "output_token_budget_exceeded"
    elif result.latency_ms > request.budget.max_latency_ms:
        kind = DecisionFailureKind.BUDGET
        code = "latency_budget_exceeded"
    elif result.cost_microusd > request.budget.max_cost_microusd:
        kind = DecisionFailureKind.BUDGET
        code = "cost_budget_exceeded"
    elif request.deterministic_required and not result.deterministic:
        kind = DecisionFailureKind.INTEGRITY
        code = "determinism_requirement_violated"
    elif preparation.route.deterministic and not result.deterministic:
        kind = DecisionFailureKind.INTEGRITY
        code = "route_determinism_downgraded"
    if kind is None:
        return None
    return DecisionFailure(
        request_id=request.request_id,
        request_digest=request.request_digest,
        kind=kind,
        code=code,
        retryable=False,
        failed_at=max(
            result.completed_at,
            attempt.started_at,
            claim.invoked_at or attempt.started_at,
        ),
        route_id=preparation.route.route_id,
        attempt_id=attempt.attempt_id,
    )


def _gateway_failure(
    request: RequestDecision,
    error: DecisionGatewayError,
    *,
    failed_at: datetime,
    route_id: str | None = None,
    attempt_id: str | None = None,
) -> DecisionFailure:
    return DecisionFailure(
        request_id=request.request_id,
        request_digest=request.request_digest,
        kind=error.kind,
        code=error.code,
        retryable=error.retryable,
        failed_at=failed_at,
        route_id=route_id,
        attempt_id=attempt_id,
        exception_type=error.exception_type,
    )


def _usage(
    request: RequestDecision,
    claim: DecisionAttemptClaim,
    result: DecisionAdapterResult,
) -> DecisionUsage:
    return DecisionUsage(
        request_id=request.request_id,
        attempt_id=claim.attempt_record.attempt.attempt_id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=result.latency_ms,
        cost_microusd=result.cost_microusd,
        deterministic=result.deterministic,
    )


def _completion_usage(
    request: RequestDecision,
    claim: DecisionAttemptClaim,
    completion: DecisionGatewayCompletion,
) -> DecisionUsage:
    return DecisionUsage(
        request_id=request.request_id,
        attempt_id=claim.attempt_record.attempt.attempt_id,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        latency_ms=completion.latency_ms,
        cost_microusd=completion.cost_microusd,
        deterministic=completion.deterministic,
    )


def _invalid_output_failure(
    request: RequestDecision,
    preparation: DecisionPreparation,
    claim: DecisionAttemptClaim,
    result: DecisionAdapterResult,
    *,
    code: DecisionDiagnosticCode,
    path: str,
    exception_type: str,
) -> DecisionFailure:
    return DecisionFailure(
        request_id=request.request_id,
        request_digest=request.request_digest,
        kind=DecisionFailureKind.SCHEMA,
        code="decision_output_invalid",
        retryable=False,
        failed_at=result.completed_at,
        route_id=preparation.route.route_id,
        attempt_id=claim.attempt_record.attempt.attempt_id,
        exception_type=exception_type,
        diagnostic=DecisionFailureDiagnostic(
            code=code,
            path=path,
            rejected_output_digest=json_digest(result.output),
        ),
        schema_version=DECISION_FAILURE_SCHEMA_VERSION_V2,
    )


def _request(record: DecisionRequestRecord) -> RequestDecision:
    return record.request


__all__ = ["RequestDecisionHandler"]
