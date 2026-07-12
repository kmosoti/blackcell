from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from blackcell.features.request_decision import (
    DecisionAdapterResult,
    DecisionAffordance,
    DecisionArgument,
    DecisionArgumentSpec,
    DecisionAttempt,
    DecisionAttemptClaim,
    DecisionAttemptRecord,
    DecisionBudget,
    DecisionCapability,
    DecisionClassification,
    DecisionFailure,
    DecisionFailureKind,
    DecisionFailureRecord,
    DecisionGatewayError,
    DecisionJournalError,
    DecisionLocality,
    DecisionPreparation,
    DecisionProposal,
    DecisionRequestRecord,
    DecisionRequirements,
    DecisionResponse,
    DecisionRoute,
    DecisionSuccessRecord,
    DecisionUsage,
    RequestDecision,
    RequestDecisionHandler,
    decode_decision_attempt,
    decode_decision_failure,
    decode_decision_output,
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
from blackcell.kernel import JsonValue
from blackcell.kernel._json import json_digest

NOW = datetime(2026, 7, 11, 18, tzinfo=UTC)


class Gateway:
    def __init__(
        self,
        events: list[str],
        *,
        route: DecisionRoute | None = None,
        route_error: DecisionGatewayError | None = None,
        invoke_error: DecisionGatewayError | None = None,
        result: DecisionAdapterResult | None = None,
    ) -> None:
        self.events = events
        self.selected_route = route or _route()
        self.route_error = route_error
        self.invoke_error = invoke_error
        self.result = result or _adapter_result()
        self.invocations = 0

    def route(self, request: RequestDecision) -> DecisionRoute:
        self.events.append(f"route:{request.request_id}")
        if self.route_error is not None:
            raise self.route_error
        return self.selected_route

    def invoke(
        self,
        request: RequestDecision,
        route: DecisionRoute,
    ) -> DecisionAdapterResult:
        self.events.append(f"invoke:{request.request_id}:{route.profile_id}")
        self.invocations += 1
        if self.invoke_error is not None:
            raise self.invoke_error
        return self.result


class Journal:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.terminal: DecisionSuccessRecord | DecisionFailureRecord | None = None
        self.claim: DecisionAttemptClaim | None = None
        self.invocation_started = False

    def register(
        self,
        request: RequestDecision,
        *,
        registered_at: datetime,
    ) -> DecisionRequestRecord:
        self.events.append(f"register:{request.request_id}")
        return DecisionRequestRecord(request, request.request_digest, registered_at)

    def record_route(
        self,
        request: DecisionRequestRecord,
        route: DecisionRoute,
        *,
        recorded_at: datetime,
    ) -> DecisionPreparation | DecisionSuccessRecord | DecisionFailureRecord:
        self.events.append(f"record-route:{route.profile_id}")
        if self.terminal is not None:
            return self.terminal
        return DecisionPreparation(request, route, route.route_id, recorded_at)

    def reject(
        self,
        request: DecisionRequestRecord,
        failure: DecisionFailure,
        *,
        recorded_at: datetime,
    ) -> DecisionFailureRecord:
        del recorded_at
        self.events.append(f"reject:{failure.code}")
        record = DecisionFailureRecord(request, failure, failure.failure_id)
        self.terminal = record
        return record

    def acquire(
        self,
        preparation: DecisionPreparation,
        *,
        acquired_at: datetime,
    ) -> DecisionAttemptClaim | DecisionSuccessRecord | DecisionFailureRecord:
        self.events.append(f"acquire:{preparation.route.profile_id}")
        if self.terminal is not None:
            return self.terminal
        request = _recorded_request(preparation.request_record)
        attempt = DecisionAttempt(
            request.request_id,
            request.request_digest,
            preparation.route.route_id,
            1,
            acquired_at,
        )
        self.claim = DecisionAttemptClaim(
            DecisionAttemptRecord(attempt, attempt.attempt_id),
            1,
            "claim:1",
        )
        return self.claim

    def begin_invoke(
        self,
        preparation: DecisionPreparation,
        claim: DecisionAttemptClaim,
        *,
        invoked_at: datetime,
    ) -> DecisionAttemptClaim | DecisionSuccessRecord | DecisionFailureRecord:
        del preparation
        self.events.append("begin-invoke")
        if self.terminal is not None:
            return self.terminal
        if self.claim != claim or self.invocation_started:
            raise DecisionJournalError("decision claim is stale or fenced")
        self.invocation_started = True
        self.claim = replace(claim, fencing_revision=2, invoked_at=invoked_at)
        return self.claim

    def succeed(
        self,
        preparation: DecisionPreparation,
        claim: DecisionAttemptClaim,
        response: DecisionResponse,
        usage: DecisionUsage,
        *,
        recorded_at: datetime,
    ) -> DecisionSuccessRecord:
        del recorded_at
        self.events.append(f"succeed:{response.proposal.proposal_id}")
        record = DecisionSuccessRecord(
            preparation,
            claim.attempt_record,
            response,
            response.response_id,
            usage,
            usage.usage_id,
        )
        self.terminal = record
        return record

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
        del recorded_at
        self.events.append(f"fail:{failure.code}")
        record = DecisionFailureRecord(
            request,
            failure,
            failure.failure_id,
            preparation,
            None if claim is None else claim.attempt_record,
            usage,
            None if usage is None else usage.usage_id,
        )
        self.terminal = record
        return record


def test_request_is_a_complete_immutable_gateway_contract() -> None:
    request = _request()

    assert request.request_id == "decision:1"
    assert request.run_id == request.correlation_id == "run:1"
    assert request.model_input["context_payload"] == '{"status":"ready"}'
    assert request.output_schema["additionalProperties"] is False
    properties = cast(
        "Mapping[str, JsonValue]",
        request.output_schema["properties"],
    )
    frame_schema = cast("Mapping[str, JsonValue]", properties["context_frame_id"])
    affordance_schema = cast("Mapping[str, JsonValue]", properties["affordance"])
    assert frame_schema["const"] == "sha256:" + "1" * 64
    assert affordance_schema["const"] == "inspect"
    assert request.affordances[0].arguments == (
        DecisionArgumentSpec("optional", False),
        DecisionArgumentSpec("path"),
    )
    with pytest.raises(TypeError):
        cast("dict[str, JsonValue]", request.model_input)["objective"] = "mutated"


@pytest.mark.parametrize(
    ("factory", "message"),
    (
        (lambda: replace(_requirements(), request_id=" "), "request_id"),
        (
            lambda: replace(_requirements(), requested_at=datetime(2026, 7, 11)),
            "timezone-aware",
        ),
        (lambda: DecisionBudget(-1, 1, 1, 1), "non-negative"),
        (lambda: DecisionBudget(True, 1, 1, 1), "integers"),
        (lambda: replace(_request(), evidence_event_ids=("event:1", "event:1")), "unique"),
        (lambda: replace(_request(), affordances=()), "at least one"),
        (
            lambda: replace(
                _request(),
                affordances=(
                    DecisionAffordance("inspect"),
                    DecisionAffordance("inspect"),
                ),
            ),
            "affordance names must be unique",
        ),
        (
            lambda: DecisionAffordance(
                "inspect",
                (DecisionArgumentSpec("path"), DecisionArgumentSpec("path")),
            ),
            "argument names must be unique",
        ),
    ),
)
def test_request_contract_rejects_ambiguous_identity_and_policy(factory, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        factory()


def test_feature_models_fail_closed_on_invalid_boundary_values() -> None:
    request = _request()
    route = _route()
    request_record = DecisionRequestRecord(request, request.request_digest, NOW)
    attempt = DecisionAttempt(request.request_id, request.request_digest, route.route_id, 1, NOW)
    attempt_record = DecisionAttemptRecord(attempt, attempt.attempt_id)
    proposal = decode_decision_output(_valid_output(), request)
    cases: tuple[tuple[object, str], ...] = (
        (lambda: DecisionArgumentSpec(" "), "argument name"),
        (
            lambda: DecisionArgumentSpec("path", cast("bool", 1)),
            "required marker",
        ),
        (lambda: DecisionAffordance(" "), "affordance name"),
        (
            lambda: replace(
                _requirements(),
                capability=cast("DecisionCapability", "reason"),
            ),
            "capability",
        ),
        (
            lambda: replace(
                _requirements(),
                classification=cast("DecisionClassification", "private"),
            ),
            "classification",
        ),
        (
            lambda: replace(
                _requirements(),
                locality=cast("DecisionLocality", "local-only"),
            ),
            "locality",
        ),
        (
            lambda: replace(_requirements(), estimated_input_tokens=cast("int", True)),
            "estimated_input_tokens",
        ),
        (
            lambda: replace(_requirements(), estimated_input_tokens=-1),
            "non-negative",
        ),
        (
            lambda: replace(
                _requirements(),
                deterministic_required=cast("bool", 1),
            ),
            "deterministic_required",
        ),
        (lambda: DecisionArgument(" ", "value"), "argument name"),
        (
            lambda: DecisionProposal(
                " ",
                request.context_frame_id,
                "inspect",
                (),
                "why",
                (),
            ),
            "proposal_id",
        ),
        (
            lambda: replace(
                proposal,
                arguments=(
                    DecisionArgument("path", "one"),
                    DecisionArgument("path", "two"),
                ),
            ),
            "argument names must be unique",
        ),
        (
            lambda: replace(proposal, evidence_event_ids=(" ",)),
            "must not be blank",
        ),
        (
            lambda: replace(proposal, evidence_event_ids=("event:1", "event:1")),
            "must be unique",
        ),
        (lambda: replace(route, profile_id=" "), "profile_id"),
        (
            lambda: replace(route, capability=cast("DecisionCapability", "reason")),
            "route capability",
        ),
        (lambda: replace(route, local=cast("bool", 1)), "markers must be booleans"),
        (
            lambda: DecisionRequestRecord(request, "sha256:" + "f" * 64, NOW),
            "does not match",
        ),
        (
            lambda: DecisionPreparation(
                request_record,
                replace(route, capability=DecisionCapability.REVIEW),
                replace(route, capability=DecisionCapability.REVIEW).route_id,
                NOW,
            ),
            "capability does not match",
        ),
        (
            lambda: DecisionPreparation(
                request_record,
                replace(route, local=False),
                replace(route, local=False).route_id,
                NOW,
            ),
            "local-only",
        ),
        (
            lambda: DecisionPreparation(
                request_record,
                replace(route, deterministic=False),
                replace(route, deterministic=False).route_id,
                NOW,
            ),
            "requires a deterministic route",
        ),
        (
            lambda: DecisionPreparation(
                request_record,
                route,
                "sha256:" + "f" * 64,
                NOW,
            ),
            "does not match",
        ),
        (
            lambda: DecisionPreparation(
                request_record,
                route,
                route.route_id,
                NOW - timedelta(seconds=1),
            ),
            "cannot precede",
        ),
        (
            lambda: replace(attempt, attempt_number=cast("int", True)),
            "attempt_number must be an integer",
        ),
        (lambda: replace(attempt, attempt_number=0), "attempt_number must be positive"),
        (
            lambda: DecisionAttemptRecord(attempt, "sha256:" + "f" * 64),
            "does not match",
        ),
        (
            lambda: DecisionAttemptClaim(attempt_record, cast("int", True), "claim"),
            "fencing_revision must be an integer",
        ),
        (
            lambda: DecisionAttemptClaim(attempt_record, 0, "claim"),
            "fencing_revision must be positive",
        ),
        (
            lambda: DecisionAttemptClaim(attempt_record, 1, " "),
            "claim_token",
        ),
        (
            lambda: DecisionAttemptClaim(
                attempt_record,
                2,
                "claim",
                datetime(2026, 7, 11),
            ),
            "timezone-aware",
        ),
        (
            lambda: DecisionAttemptClaim(
                attempt_record,
                2,
                "claim",
                NOW - timedelta(microseconds=1),
            ),
            "cannot precede",
        ),
        (
            lambda: DecisionAdapterResult(
                cast("Mapping[str, JsonValue]", ()), 0, 0, 0, 0, True, NOW
            ),
            "output must be an object",
        ),
        (
            lambda: DecisionAdapterResult({}, -1, 0, 0, 0, True, NOW),
            "non-negative",
        ),
        (
            lambda: DecisionAdapterResult({}, 0, 0, 0, 0, cast("bool", 1), NOW),
            "determinism marker",
        ),
    )
    for factory, message in cases:
        with pytest.raises((TypeError, ValueError), match=message):
            cast("Any", factory)()


def test_all_decision_artifacts_round_trip_with_content_identities() -> None:
    request = _request()
    route = _route()
    attempt = DecisionAttempt(request.request_id, request.request_digest, route.route_id, 1, NOW)
    proposal = decode_decision_output(_valid_output(), request)
    response = DecisionResponse(
        request.request_id,
        request.request_digest,
        route.route_id,
        attempt.attempt_id,
        proposal,
        NOW,
    )
    failure = DecisionFailure(
        request.request_id,
        request.request_digest,
        DecisionFailureKind.ADAPTER,
        "adapter_failed",
        True,
        NOW,
        route.route_id,
        attempt.attempt_id,
        "RuntimeError",
    )
    usage = DecisionUsage(request.request_id, attempt.attempt_id, 10, 4, 12, 3, True)

    assert (
        decode_decision_request(
            encode_decision_request(request),
            expected_request_digest=request.request_digest,
        )
        == request
    )
    assert (
        decode_decision_route(encode_decision_route(route), expected_route_id=route.route_id)
        == route
    )
    assert (
        decode_decision_attempt(
            encode_decision_attempt(attempt), expected_attempt_id=attempt.attempt_id
        )
        == attempt
    )
    assert (
        decode_decision_response(
            encode_decision_response(response),
            expected_response_id=response.response_id,
            request=request,
        )
        == response
    )
    assert (
        decode_decision_failure(
            encode_decision_failure(failure), expected_failure_id=failure.failure_id
        )
        == failure
    )
    assert (
        decode_decision_usage(encode_decision_usage(usage), expected_usage_id=usage.usage_id)
        == usage
    )


@pytest.mark.parametrize(
    ("encode", "decode", "factory"),
    (
        (encode_decision_request, decode_decision_request, lambda: _request()),
        (encode_decision_route, decode_decision_route, lambda: _route()),
    ),
)
def test_artifact_codecs_reject_unknown_fields_and_digest_mismatch(encode, decode, factory) -> None:
    value = factory()
    payload = json.loads(encode(value))
    payload["unexpected"] = True

    with pytest.raises(ValueError, match="unexpected fields"):
        decode(json.dumps(payload))
    with pytest.raises(ValueError, match="identity does not match"):
        if isinstance(value, RequestDecision):
            decode(encode(value), expected_request_digest="sha256:" + "f" * 64)
        else:
            decode(encode(value), expected_route_id="sha256:" + "f" * 64)


def test_request_codec_rejects_tool_authority_and_derived_contract_tampering() -> None:
    request = _request()
    payload = json.loads(encode_decision_request(request))
    payload["tools_allowed"] = True
    with pytest.raises(ValueError, match="tool authority"):
        decode_decision_request(json.dumps(payload))

    payload = json.loads(encode_decision_request(request))
    payload["output_schema"]["properties"]["affordance"]["const"] = "delete"
    with pytest.raises(ValueError, match="output schema does not match"):
        decode_decision_request(json.dumps(payload))


def test_artifact_decoders_reject_malformed_nested_values() -> None:
    with pytest.raises(ValueError, match="valid JSON"):
        decode_decision_request("{")

    request_payload = json.loads(encode_decision_request(_request()))
    mutations: tuple[tuple[tuple[str, ...], object, str], ...] = (
        (("schema_version",), "decision-request/v99", "unsupported"),
        (("affordances",), "inspect", "affordances must be an array"),
        (("affordances", "0", "arguments"), "path", "arguments must be an array"),
        (("budget", "max_input_tokens"), True, "must be an integer"),
        (("requested_at",), "not-a-time", "ISO-8601"),
        (("requested_at",), "2026-07-11T18:00:00", "timezone-aware"),
        (("capability",), "unknown", "not recognized"),
        (("evidence_event_ids",), (1,), "string array"),
    )
    for path, value, message in mutations:
        payload = json.loads(json.dumps(request_payload))
        target: object = payload
        for part in path[:-1]:
            if part.isdigit():
                target = cast("list[object]", target)[int(part)]
            else:
                target = cast("dict[str, object]", target)[part]
        cast("dict[str, object]", target)[path[-1]] = value
        with pytest.raises((TypeError, ValueError), match=message):
            decode_decision_request(json.dumps(payload))

    failure = DecisionFailure(
        _request().request_id,
        _request().request_digest,
        DecisionFailureKind.ADMISSION,
        "no_profile",
        False,
        NOW,
    )
    failure_payload = json.loads(encode_decision_failure(failure))
    failure_payload["exception_type"] = " "
    with pytest.raises(ValueError, match="non-empty string or null"):
        decode_decision_failure(json.dumps(failure_payload))

    output = dict(_valid_output())
    output["arguments"] = ({"name": "path", "value": {"nested": True}},)
    with pytest.raises(ValueError, match="JSON scalar"):
        decode_decision_output(output, _request())


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ({"context_frame_id": "sha256:" + "2" * 64}, "different ContextFrame"),
        ({"affordance": "delete"}, "undeclared affordance"),
        (
            {"arguments": ({"name": "unknown", "value": "README.md"},)},
            "undeclared arguments",
        ),
        ({"arguments": ()}, "omits required arguments"),
        ({"evidence_event_ids": ("event:outside",)}, "outside its ContextFrame"),
    ),
)
def test_decision_output_is_semantically_bound_to_its_request(mutation, message: str) -> None:
    output = dict(_valid_output())
    output.update(mutation)

    with pytest.raises(ValueError, match=message):
        decode_decision_output(output, _request())


def test_two_phase_handler_records_request_before_live_inference() -> None:
    events: list[str] = []
    gateway = Gateway(events)
    journal = Journal(events)
    handler = RequestDecisionHandler(gateway, journal, clock=lambda: NOW)

    prepared = handler.prepare(_request())

    assert isinstance(prepared, DecisionPreparation)
    assert gateway.invocations == 0
    assert events == [
        "register:decision:1",
        "route:decision:1",
        "record-route:reason-local",
    ]

    result = handler.handle(prepared)

    assert isinstance(result, DecisionSuccessRecord)
    assert result.response.proposal.affordance == "inspect"
    assert result.response.proposal.evidence_event_ids == ("event:1",)
    assert events[3:] == [
        "acquire:reason-local",
        "begin-invoke",
        "invoke:decision:1:reason-local",
        "succeed:proposal:1",
    ]


def test_staged_attempt_can_be_recorded_before_live_inference() -> None:
    events: list[str] = []
    gateway = Gateway(events)
    handler = RequestDecisionHandler(gateway, Journal(events), clock=lambda: NOW)
    prepared = handler.prepare(_request())
    assert isinstance(prepared, DecisionPreparation)

    claim = handler.acquire(prepared)

    assert isinstance(claim, DecisionAttemptClaim)
    assert gateway.invocations == 0
    assert events[-1] == "acquire:reason-local"

    result = handler.invoke(prepared, claim)

    assert isinstance(result, DecisionSuccessRecord)
    assert gateway.invocations == 1
    assert events[-2:] == [
        "invoke:decision:1:reason-local",
        "succeed:proposal:1",
    ]


@pytest.mark.parametrize(
    "mismatch",
    ("request", "digest", "route", "time", "attempt", "token", "revision"),
)
def test_staged_invoke_rejects_a_foreign_claim_before_gateway_call(mismatch: str) -> None:
    events: list[str] = []
    gateway = Gateway(events)
    handler = RequestDecisionHandler(gateway, Journal(events), clock=lambda: NOW)
    prepared = handler.prepare(_request())
    assert isinstance(prepared, DecisionPreparation)
    claim = handler.acquire(prepared)
    assert isinstance(claim, DecisionAttemptClaim)
    attempt = claim.attempt_record.attempt
    if mismatch == "token":
        forged_claim = replace(claim, claim_token="claim:forged")
    elif mismatch == "revision":
        forged_claim = replace(claim, fencing_revision=2)
    else:
        changes = {
            "request": {"request_id": "decision:other"},
            "digest": {"request_digest": json_digest("other-request")},
            "route": {"route_id": json_digest("other-route")},
            "time": {"started_at": prepared.prepared_at - timedelta(microseconds=1)},
            "attempt": {"attempt_number": 2},
        }[mismatch]
        forged_attempt = replace(attempt, **changes)
        forged_claim = replace(
            claim,
            attempt_record=replace(
                claim.attempt_record,
                attempt=forged_attempt,
                attempt_artifact_digest=forged_attempt.attempt_id,
            ),
        )

    with pytest.raises(
        ValueError if mismatch in {"request", "digest", "route", "time"} else DecisionJournalError
    ):
        handler.invoke(prepared, forged_claim)

    assert gateway.invocations == 0


@pytest.mark.parametrize("terminal_kind", ("success", "failure"))
def test_staged_claim_reuse_returns_terminal_without_duplicate_inference(
    terminal_kind: str,
) -> None:
    events: list[str] = []
    error = (
        None
        if terminal_kind == "success"
        else DecisionGatewayError(
            DecisionFailureKind.ADAPTER,
            "adapter_failed",
            retryable=False,
        )
    )
    gateway = Gateway(events, invoke_error=error)
    handler = RequestDecisionHandler(gateway, Journal(events), clock=lambda: NOW)
    prepared = handler.prepare(_request())
    assert isinstance(prepared, DecisionPreparation)
    claim = handler.acquire(prepared)
    assert isinstance(claim, DecisionAttemptClaim)

    first = handler.invoke(prepared, claim)
    second = handler.invoke(prepared, claim)

    assert second is first
    assert gateway.invocations == 1


def test_route_rejection_is_terminal_without_an_attempt_or_model_call() -> None:
    events: list[str] = []
    gateway = Gateway(
        events,
        route_error=DecisionGatewayError(
            DecisionFailureKind.ADMISSION,
            "no_profile",
            retryable=False,
        ),
    )
    handler = RequestDecisionHandler(gateway, Journal(events), clock=lambda: NOW)

    result = handler.prepare(_request())

    assert isinstance(result, DecisionFailureRecord)
    assert result.failure.kind is DecisionFailureKind.ADMISSION
    assert result.preparation is None
    assert result.attempt_record is None
    assert gateway.invocations == 0
    assert events[-1] == "reject:no_profile"


def test_adapter_failure_is_recorded_without_fabricated_usage() -> None:
    events: list[str] = []
    gateway = Gateway(
        events,
        invoke_error=DecisionGatewayError(
            DecisionFailureKind.TIMEOUT,
            "adapter_timeout",
            retryable=True,
            exception_type="TimeoutError",
        ),
    )
    handler = RequestDecisionHandler(gateway, Journal(events), clock=lambda: NOW)
    prepared = handler.prepare(_request())
    assert isinstance(prepared, DecisionPreparation)

    result = handler.handle(prepared)

    assert isinstance(result, DecisionFailureRecord)
    assert result.failure.kind is DecisionFailureKind.TIMEOUT
    assert result.failure.exception_type == "TimeoutError"
    assert result.usage is None
    assert result.attempt_record is not None


@pytest.mark.parametrize(
    ("result_factory", "kind", "code"),
    (
        (
            lambda: _adapter_result(output_tokens=21),
            DecisionFailureKind.BUDGET,
            "output_token_budget_exceeded",
        ),
        (
            lambda: _adapter_result(input_tokens=101),
            DecisionFailureKind.BUDGET,
            "input_token_budget_exceeded",
        ),
        (
            lambda: _adapter_result(latency_ms=1_001),
            DecisionFailureKind.BUDGET,
            "latency_budget_exceeded",
        ),
        (
            lambda: _adapter_result(cost_microusd=101),
            DecisionFailureKind.BUDGET,
            "cost_budget_exceeded",
        ),
        (
            lambda: _adapter_result(completed_at=NOW - timedelta(seconds=1)),
            DecisionFailureKind.INTEGRITY,
            "decision_completion_precedes_attempt",
        ),
        (
            lambda: _adapter_result(output={"unexpected": True}),
            DecisionFailureKind.SCHEMA,
            "decision_output_invalid",
        ),
        (
            lambda: _adapter_result(deterministic=False),
            DecisionFailureKind.INTEGRITY,
            "determinism_requirement_violated",
        ),
    ),
)
def test_post_call_rejections_preserve_known_usage(result_factory, kind, code: str) -> None:
    events: list[str] = []
    result = result_factory()
    handler = RequestDecisionHandler(
        Gateway(events, result=result),
        Journal(events),
        clock=lambda: NOW,
    )
    prepared = handler.prepare(_request())
    assert isinstance(prepared, DecisionPreparation)

    outcome = handler.handle(prepared)

    assert isinstance(outcome, DecisionFailureRecord)
    assert outcome.failure.kind is kind
    assert outcome.failure.code == code
    assert outcome.usage is not None
    assert outcome.usage.output_tokens == result.output_tokens


def test_route_determinism_downgrade_is_distinct_from_request_requirement() -> None:
    events: list[str] = []
    handler = RequestDecisionHandler(
        Gateway(events, result=_adapter_result(deterministic=False)),
        Journal(events),
        clock=lambda: NOW,
    )
    request = _request(deterministic_required=False)
    prepared = handler.prepare(request)
    assert isinstance(prepared, DecisionPreparation)

    outcome = handler.handle(prepared)

    assert isinstance(outcome, DecisionFailureRecord)
    assert outcome.failure.code == "route_determinism_downgraded"


def test_terminal_attempt_retry_does_not_touch_the_gateway() -> None:
    events: list[str] = []
    gateway = Gateway(events)
    journal = Journal(events)
    handler = RequestDecisionHandler(gateway, journal, clock=lambda: NOW)
    prepared = handler.prepare(_request())
    assert isinstance(prepared, DecisionPreparation)
    first = handler.handle(prepared)
    assert isinstance(first, DecisionSuccessRecord)
    invocations = gateway.invocations

    retried = handler.handle(prepared)

    assert retried is first
    assert gateway.invocations == invocations
    assert events[-1] == "acquire:reason-local"


def test_invalid_route_is_durably_rejected_before_inference() -> None:
    events: list[str] = []
    gateway = Gateway(events, route=replace(_route(), local=False))
    handler = RequestDecisionHandler(gateway, Journal(events), clock=lambda: NOW)

    result = handler.prepare(_request(locality=DecisionLocality.LOCAL_ONLY))

    assert isinstance(result, DecisionFailureRecord)
    assert result.failure.kind is DecisionFailureKind.INTEGRITY
    assert result.failure.code == "gateway_route_contract_invalid"
    assert gateway.invocations == 0


def test_handler_rejects_a_naive_clock_before_journal_activity() -> None:
    events: list[str] = []
    handler = RequestDecisionHandler(
        Gateway(events),
        Journal(events),
        clock=lambda: datetime(2026, 7, 11),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        handler.prepare(_request())
    assert events == []


def _requirements() -> DecisionRequirements:
    return DecisionRequirements(
        "decision:1",
        "node:planner",
        DecisionCapability.REASON,
        DecisionClassification.PRIVATE,
        DecisionLocality.LOCAL_ONLY,
        DecisionBudget(100, 20, 1_000, 100),
        12,
        True,
        NOW,
    )


def _request(
    *,
    locality: DecisionLocality | None = None,
    deterministic_required: bool | None = None,
) -> RequestDecision:
    requirements = _requirements()
    if locality is not None:
        requirements = replace(requirements, locality=locality)
    if deterministic_required is not None:
        requirements = replace(
            requirements,
            deterministic_required=deterministic_required,
        )
    return RequestDecision(
        requirements,
        "run:1",
        "run:1",
        "event:context-recorded",
        "sha256:" + "1" * 64,
        "inspect project status",
        '{"status":"ready"}',
        ("event:1",),
        (
            DecisionAffordance(
                "inspect",
                (
                    DecisionArgumentSpec("path"),
                    DecisionArgumentSpec("optional", False),
                ),
            ),
        ),
    )


def _route() -> DecisionRoute:
    return DecisionRoute(
        "reason-local",
        "recorded",
        "reason-v1",
        DecisionCapability.REASON,
        True,
        True,
        NOW,
    )


def _valid_output() -> dict[str, JsonValue]:
    return {
        "proposal_id": "proposal:1",
        "context_frame_id": "sha256:" + "1" * 64,
        "affordance": "inspect",
        "arguments": ({"name": "path", "value": "README.md"},),
        "rationale": "inspect the cited repository evidence",
        "evidence_event_ids": ("event:1",),
    }


def _adapter_result(
    *,
    output: dict[str, JsonValue] | None = None,
    input_tokens: int = 10,
    output_tokens: int = 4,
    latency_ms: int = 12,
    cost_microusd: int = 3,
    deterministic: bool = True,
    completed_at: datetime = NOW,
) -> DecisionAdapterResult:
    return DecisionAdapterResult(
        _valid_output() if output is None else output,
        input_tokens,
        output_tokens,
        latency_ms,
        cost_microusd,
        deterministic,
        completed_at,
    )


def _recorded_request(record: DecisionRequestRecord) -> RequestDecision:
    assert isinstance(record.request, RequestDecision)
    return record.request
