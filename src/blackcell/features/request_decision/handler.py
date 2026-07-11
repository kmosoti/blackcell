from __future__ import annotations

from datetime import datetime

from blackcell.features.request_decision.artifacts import decode_decision_output
from blackcell.features.request_decision.command import RequestDecision
from blackcell.features.request_decision.errors import DecisionGatewayError
from blackcell.features.request_decision.models import (
    DecisionAdapterResult,
    DecisionAttemptClaim,
    DecisionFailure,
    DecisionFailureKind,
    DecisionFailureRecord,
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


class RequestDecisionHandler:
    """Prepare, durably bracket, and validate one gateway-owned model decision.

    ``prepare`` never invokes a model. A workflow can therefore record the
    request and selected route before calling ``handle``, which acquires the
    durable attempt claim immediately before live inference.
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
        try:
            route = self._gateway.route(command)
            _validate_route(command, route)
        except DecisionGatewayError as error:
            return self._journal.reject(
                request_record,
                _gateway_failure(command, error, failed_at=self._now()),
                recorded_at=self._now(),
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
            return self._journal.reject(
                request_record,
                failure,
                recorded_at=self._now(),
            )
        return self._journal.record_route(
            request_record,
            route,
            recorded_at=self._now(),
        )

    def handle(
        self,
        preparation: DecisionPreparation,
    ) -> DecisionSuccessRecord | DecisionFailureRecord:
        acquired = self._journal.acquire(preparation, acquired_at=self._now())
        if isinstance(acquired, DecisionSuccessRecord | DecisionFailureRecord):
            return acquired
        claim = acquired
        request = _request(preparation.request_record)
        try:
            adapter_result = self._gateway.invoke(request, preparation.route)
        except DecisionGatewayError as error:
            failure = _gateway_failure(
                request,
                error,
                failed_at=self._now(),
                route_id=preparation.route.route_id,
                attempt_id=claim.attempt_record.attempt.attempt_id,
            )
            return self._journal.fail(
                preparation.request_record,
                failure,
                preparation=preparation,
                claim=claim,
                usage=None,
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
        except (TypeError, ValueError) as error:
            failure = DecisionFailure(
                request_id=request.request_id,
                request_digest=request.request_digest,
                kind=DecisionFailureKind.SCHEMA,
                code="decision_output_invalid",
                retryable=False,
                failed_at=adapter_result.completed_at,
                route_id=preparation.route.route_id,
                attempt_id=claim.attempt_record.attempt.attempt_id,
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
        failed_at=max(result.completed_at, attempt.started_at),
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


def _request(record: DecisionRequestRecord) -> RequestDecision:
    return record.request


__all__ = ["RequestDecisionHandler"]
