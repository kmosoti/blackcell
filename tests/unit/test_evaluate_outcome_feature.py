from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest

from blackcell.features.evaluate_outcome import (
    EvaluateOutcome,
    EvaluationArtifactCodecError,
    EvaluationAuthorizationOutcome,
    EvaluationCriterion,
    EvaluationExecutionStatus,
    EvaluationFact,
    EvaluationFinding,
    EvaluationObservation,
    EvaluationObservationStatus,
    EvaluationSourceEvent,
    EvaluationSpec,
    EvaluationVerdict,
    OutcomeEvaluator,
    decode_evaluation_spec,
    decode_outcome_evaluation,
    encode_evaluation_spec,
    encode_outcome_evaluation,
    outcome_evaluation_payload,
)
from blackcell.kernel import JsonScalar
from blackcell.kernel._json import canonical_json_bytes, json_digest

NOW = datetime(2026, 7, 12, 18, tzinfo=UTC)
BINDING_ID = f"sha256:{'1' * 64}"
OBSERVATION_DIGEST = f"sha256:{'2' * 64}"
PAYLOAD_HASH = f"sha256:{'3' * 64}"
EXECUTION_EVENT_ID = "event:execution"


def test_spec_is_developer_owned_content_addressed_and_canonical() -> None:
    spec = EvaluationSpec(
        "daily-ready",
        "repository is ready",
        (
            EvaluationCriterion("z", "repository", "git.clean", True),
            EvaluationCriterion("a", "repository", "tests.pass", True, 0.8),
        ),
    )

    assert tuple(item.criterion_id for item in spec.criteria) == ("a", "z")
    assert spec.spec_id.startswith("sha256:")
    assert decode_evaluation_spec(encode_evaluation_spec(spec)) == spec
    with pytest.raises(ValueError, match="targets must be unique"):
        replace(
            spec,
            criteria=(
                EvaluationCriterion("one", "repository", "git.clean", True),
                EvaluationCriterion("two", "repository", "git.clean", False),
            ),
        )
    with pytest.raises(ValueError, match="required criterion"):
        replace(
            spec,
            criteria=(
                EvaluationCriterion("optional", "repository", "ready", True, required=False),
            ),
        )


@pytest.mark.parametrize(
    ("outcome", "code"),
    (
        (EvaluationAuthorizationOutcome.DENY, "authorization-denied"),
        (
            EvaluationAuthorizationOutcome.REQUIRE_APPROVAL,
            "authorization-requires-approval",
        ),
    ),
)
def test_blocked_control_paths_are_explicitly_not_evaluated(outcome, code: str) -> None:
    command = EvaluateOutcome("run:1", _spec(), outcome, None, None, None, None, 10)

    evaluation = OutcomeEvaluator(clock=lambda: NOW).handle(command)

    assert evaluation.verdict is EvaluationVerdict.NOT_EVALUATED
    assert evaluation.execution_status is None
    assert {item.verdict for item in evaluation.findings} == {EvaluationVerdict.NOT_EVALUATED}
    assert {item.code for item in evaluation.findings} == {code}


def test_unknown_execution_is_inconclusive_without_fabricated_observation() -> None:
    command = EvaluateOutcome(
        "run:1",
        _spec(),
        EvaluationAuthorizationOutcome.ALLOW,
        EvaluationExecutionStatus.UNKNOWN,
        EXECUTION_EVENT_ID,
        BINDING_ID,
        None,
        10,
    )

    evaluation = OutcomeEvaluator(clock=lambda: NOW).handle(command)

    assert evaluation.verdict is EvaluationVerdict.INCONCLUSIVE
    assert evaluation.outcome_observation_id is None
    assert {item.code for item in evaluation.findings} == {"execution-unknown"}


def test_fresh_independent_fact_passes_and_wrong_fact_fails() -> None:
    passed = OutcomeEvaluator(clock=lambda: NOW).handle(_command(_observation(value=True)))
    failed = OutcomeEvaluator(clock=lambda: NOW).handle(_command(_observation(value=False)))

    assert passed.verdict is EvaluationVerdict.PASS
    assert passed.findings[0].code == "expected-value-observed"
    assert passed.findings[0].source_event_ids == ("event:outcome",)
    assert failed.verdict is EvaluationVerdict.FAIL
    assert failed.findings[0].code == "unexpected-value-observed"


def test_executor_failure_does_not_override_independently_observed_goal_state() -> None:
    evaluation = OutcomeEvaluator(clock=lambda: NOW).handle(
        _command(
            _observation(
                value=True,
                execution_status=EvaluationExecutionStatus.FAILED,
            ),
            execution=EvaluationExecutionStatus.FAILED,
        )
    )

    assert evaluation.execution_status is EvaluationExecutionStatus.FAILED
    assert evaluation.verdict is EvaluationVerdict.PASS


def test_bool_and_integer_values_are_not_treated_as_the_same_json_fact() -> None:
    evaluation = OutcomeEvaluator(clock=lambda: NOW).handle(_command(_observation(value=1)))

    assert evaluation.verdict is EvaluationVerdict.FAIL
    assert evaluation.findings[0].actual_value == 1


def test_missing_conflicting_low_confidence_and_observer_uncertainty_are_inconclusive() -> None:
    missing = _observation(
        facts=(EvaluationFact("claim:other", "repository", "other", True, 1.0, "event:outcome"),)
    )
    with pytest.raises(ValueError, match="outside EvaluationSpec targets"):
        _command(missing)

    partial_spec = EvaluationSpec(
        "partial",
        "repository is ready",
        (
            EvaluationCriterion("clean", "repository", "git.clean", True),
            EvaluationCriterion("tests", "repository", "tests.pass", True),
        ),
    )
    no_match = _observation(
        spec=partial_spec,
        facts=(
            EvaluationFact(
                "claim:tests",
                "repository",
                "tests.pass",
                True,
                1.0,
                "event:outcome",
            ),
        ),
    )
    missing_result = OutcomeEvaluator(clock=lambda: NOW).handle(
        _command(no_match, spec=partial_spec)
    )
    assert missing_result.verdict is EvaluationVerdict.INCONCLUSIVE
    assert {item.code for item in missing_result.findings} == {
        "expected-value-observed",
        "no-fresh-outcome-evidence",
    }

    conflicting = _observation(
        facts=(
            EvaluationFact("claim:a", "repository", "git.clean", True, 1.0, "event:outcome"),
            EvaluationFact("claim:b", "repository", "git.clean", False, 1.0, "event:outcome"),
        )
    )
    conflict_result = OutcomeEvaluator(clock=lambda: NOW).handle(_command(conflicting))
    assert conflict_result.verdict is EvaluationVerdict.INCONCLUSIVE
    assert conflict_result.findings[0].code == "conflicting-fresh-outcome-evidence"

    low_spec = EvaluationSpec(
        "strict",
        "repository is clean",
        (EvaluationCriterion("clean", "repository", "git.clean", True, 0.9),),
    )
    low = _observation(value=True, confidence=0.8, spec=low_spec)
    low_result = OutcomeEvaluator(clock=lambda: NOW).handle(_command(low, spec=low_spec))
    assert low_result.verdict is EvaluationVerdict.INCONCLUSIVE
    assert low_result.findings[0].actual_present

    inconclusive = replace(
        _observation(),
        status=EvaluationObservationStatus.INCONCLUSIVE,
        sources=(_source(event_type="outcome.observation-inconclusive"),),
        facts=(),
    )
    inconclusive_result = OutcomeEvaluator(clock=lambda: NOW).handle(_command(inconclusive))
    assert inconclusive_result.verdict is EvaluationVerdict.INCONCLUSIVE
    assert inconclusive_result.findings[0].code == "outcome-observation-inconclusive"


def test_only_post_initial_state_events_can_satisfy_a_criterion() -> None:
    stale = replace(
        _observation(),
        sources=(_source(position=10),),
    )

    with pytest.raises(ValueError, match="newer than the initial state"):
        _command(stale)


def test_command_enforces_branch_spec_and_execution_binding_integrity() -> None:
    observation = _observation()
    with pytest.raises(ValueError, match="blocked authorization"):
        EvaluateOutcome(
            "run:1",
            _spec(),
            EvaluationAuthorizationOutcome.DENY,
            EvaluationExecutionStatus.SUCCEEDED,
            EXECUTION_EVENT_ID,
            BINDING_ID,
            observation,
            10,
        )
    with pytest.raises(ValueError, match="different EvaluationSpec"):
        _command(replace(observation, evaluation_spec_id=f"sha256:{'3' * 64}"))
    with pytest.raises(ValueError, match="different execution binding"):
        _command(replace(observation, execution_binding_id=f"sha256:{'4' * 64}"))
    with pytest.raises(ValueError, match="requires an independent outcome observation"):
        EvaluateOutcome(
            "run:1",
            _spec(),
            EvaluationAuthorizationOutcome.ALLOW,
            EvaluationExecutionStatus.SUCCEEDED,
            EXECUTION_EVENT_ID,
            BINDING_ID,
            None,
            10,
        )


def test_optional_findings_do_not_override_required_success() -> None:
    spec = EvaluationSpec(
        "required-and-optional",
        "required state is achieved",
        (
            EvaluationCriterion("clean", "repository", "git.clean", True),
            EvaluationCriterion(
                "optional-tests",
                "repository",
                "tests.pass",
                True,
                required=False,
            ),
        ),
    )
    observation = _observation(
        spec=spec,
        facts=(
            EvaluationFact("claim:clean", "repository", "git.clean", True, 1.0, "event:outcome"),
            EvaluationFact("claim:tests", "repository", "tests.pass", False, 1.0, "event:outcome"),
        ),
    )

    evaluation = OutcomeEvaluator(clock=lambda: NOW).handle(_command(observation, spec=spec))

    assert evaluation.verdict is EvaluationVerdict.PASS
    assert {item.verdict for item in evaluation.findings} == {
        EvaluationVerdict.PASS,
        EvaluationVerdict.FAIL,
    }


def test_spec_and_evaluation_codecs_reject_tampering_and_noncanonical_order() -> None:
    spec = _spec()
    evaluation = OutcomeEvaluator(clock=lambda: NOW).handle(_command(_observation()))
    encoded_spec = encode_evaluation_spec(spec)
    encoded_evaluation = encode_outcome_evaluation(evaluation)

    assert decode_outcome_evaluation(encoded_evaluation, spec=spec) == evaluation
    with pytest.raises(EvaluationArtifactCodecError, match="canonical JSON"):
        decode_evaluation_spec(encoded_spec + b"\n")

    spec_payload = json.loads(encoded_spec)
    spec_payload["criteria"][0]["expected_value"] = False
    with pytest.raises(EvaluationArtifactCodecError, match="spec_id does not match"):
        decode_evaluation_spec(canonical_json_bytes(spec_payload))

    evaluation_payload = json.loads(encoded_evaluation)
    evaluation_payload["evaluation_id"] = f"sha256:{'9' * 64}"
    with pytest.raises(EvaluationArtifactCodecError, match="evaluation_id does not match"):
        decode_outcome_evaluation(canonical_json_bytes(evaluation_payload), spec=spec)


@pytest.mark.parametrize(
    ("factory", "message"),
    (
        (lambda: EvaluationCriterion("", "s", "p", True), "criterion_id"),
        (lambda: EvaluationCriterion("c", "s", "p", True, True), "numeric"),
        (lambda: EvaluationCriterion("c", "s", "p", True, 1.1), "between"),
        (
            lambda: EvaluationCriterion("c", "s", "p", True, required=cast("bool", 1)),
            "boolean",
        ),
        (
            lambda: EvaluationCriterion("c", "s", "p", cast("JsonScalar", (1,))),
            "JSON scalar",
        ),
        (lambda: replace(_spec(), schema_version="evaluation-spec/v99"), "unsupported"),
        (lambda: replace(_spec(), name=" "), "name"),
        (lambda: replace(_spec(), criteria=()), "at least one"),
        (
            lambda: replace(
                _spec(),
                criteria=(
                    EvaluationCriterion("same", "a", "one", True),
                    EvaluationCriterion("same", "a", "two", True),
                ),
            ),
            "criterion ids",
        ),
        (lambda: replace(_source(), event_id=" "), "event_id"),
        (lambda: replace(_source(), event_type="other"), "event type"),
        (lambda: replace(_source(), payload_hash="sha256:bad"), "SHA-256"),
        (lambda: replace(_source(), global_position=cast("int", True)), "integer"),
        (lambda: replace(_source(), global_position=0), "positive"),
        (
            lambda: EvaluationFact("", "s", "p", True, 1.0, "event:outcome"),
            "claim_id",
        ),
        (
            lambda: EvaluationFact("c", "s", "p", True, cast("float", True), "event:outcome"),
            "numeric",
        ),
        (
            lambda: EvaluationFact("c", "s", "p", True, 1.1, "event:outcome"),
            "between",
        ),
        (
            lambda: EvaluationFact("c", "s", "p", cast("JsonScalar", (1,)), 1.0, "event:outcome"),
            "JSON scalar",
        ),
        (lambda: replace(_observation(), observation_id=" "), "observation_id"),
        (lambda: replace(_observation(), observation_digest="sha256:bad"), "SHA-256"),
        (
            lambda: replace(
                _observation(),
                status=cast("EvaluationObservationStatus", "observed"),
            ),
            "status",
        ),
        (
            lambda: replace(_observation(), observed_at=NOW.replace(tzinfo=None)),
            "timezone-aware",
        ),
        (lambda: replace(_observation(), sources=()), "source events"),
        (
            lambda: replace(
                _observation(),
                sources=(_source(), replace(_source(), global_position=12)),
            ),
            "event ids",
        ),
        (
            lambda: replace(
                _observation(),
                sources=(_source(), replace(_source(), event_id="event:two")),
            ),
            "positions",
        ),
        (
            lambda: replace(
                _observation(),
                sources=(
                    _source(),
                    replace(_source(), event_id="event:two", global_position=12, stream_id="other"),
                ),
            ),
            "one domain stream",
        ),
        (
            lambda: replace(
                _observation(),
                facts=(
                    EvaluationFact("same", "s", "p", True, 1.0, "event:outcome"),
                    EvaluationFact("same", "s", "p", True, 1.0, "event:outcome"),
                ),
            ),
            "claim ids",
        ),
        (
            lambda: replace(
                _observation(),
                facts=(EvaluationFact("c", "s", "p", True, 1.0, "event:other"),),
            ),
            "cite",
        ),
        (lambda: replace(_observation(), facts=()), "requires facts"),
        (
            lambda: EvaluationFinding("c", True, EvaluationVerdict.PASS, "code", True, False, True),
            "must use null",
        ),
        (
            lambda: EvaluationFinding(
                "c",
                True,
                EvaluationVerdict.PASS,
                "code",
                True,
                True,
                True,
                actual_confidence=1.0,
                observed_claim_ids=("claim", "claim"),
                source_event_ids=("event",),
            ),
            "unique",
        ),
    ),
)
def test_evaluation_domain_contracts_fail_closed(factory, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        factory()


@pytest.mark.parametrize(
    ("factory", "message"),
    (
        (lambda: replace(_command(_observation()), run_id=" "), "run_id"),
        (
            lambda: replace(_command(_observation()), spec=cast("EvaluationSpec", object())),
            "EvaluationSpec",
        ),
        (
            lambda: replace(
                _command(_observation()),
                authorization_outcome=cast("EvaluationAuthorizationOutcome", "allow"),
            ),
            "authorization_outcome",
        ),
        (
            lambda: replace(
                _command(_observation()),
                execution_status=cast("EvaluationExecutionStatus", "succeeded"),
            ),
            "execution_status",
        ),
        (
            lambda: replace(_command(_observation()), initial_state_position=cast("int", True)),
            "integer",
        ),
        (lambda: replace(_command(_observation()), initial_state_position=-1), "non-negative"),
        (lambda: replace(_command(_observation()), execution_event_id=None), "identity"),
        (lambda: replace(_command(_observation()), execution_event_id=" "), "execution_event_id"),
        (
            lambda: replace(_command(_observation()), execution_binding_id="sha256:not-hex"),
            "SHA-256",
        ),
        (
            lambda: replace(
                _command(_observation(), execution=EvaluationExecutionStatus.UNKNOWN),
                observation=_observation(),
            ),
            "unknown execution",
        ),
        (
            lambda: _command(
                replace(
                    _observation(),
                    sources=(replace(_source(), correlation_id="run:other"),),
                )
            ),
            "correlated",
        ),
        (
            lambda: _command(
                replace(
                    _observation(),
                    sources=(replace(_source(), causation_id="event:other"),),
                )
            ),
            "caused",
        ),
    ),
)
def test_evaluate_command_rejects_ambiguous_or_unbound_inputs(factory, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        factory()


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (lambda item: replace(item, schema_version="outcome-evaluation/v99"), "unsupported"),
        (lambda item: replace(item, run_id=" "), "run_id"),
        (lambda item: replace(item, evaluation_spec_id="sha256:bad"), "SHA-256"),
        (
            lambda item: replace(
                item,
                authorization_outcome=cast("EvaluationAuthorizationOutcome", "allow"),
            ),
            "authorization_outcome",
        ),
        (
            lambda item: replace(
                item,
                execution_status=cast("EvaluationExecutionStatus", "succeeded"),
            ),
            "execution_status",
        ),
        (
            lambda item: replace(item, verdict=cast("EvaluationVerdict", "pass")),
            "verdict",
        ),
        (lambda item: replace(item, initial_state_position=cast("int", True)), "integer"),
        (lambda item: replace(item, initial_state_position=-1), "non-negative"),
        (lambda item: replace(item, evaluated_at=NOW.replace(tzinfo=None)), "timezone-aware"),
        (lambda item: replace(item, findings=()), "requires findings"),
        (
            lambda item: replace(item, findings=(item.findings[0], item.findings[0])),
            "findings must be unique",
        ),
        (lambda item: replace(item, execution_event_id=" "), "execution_event_id"),
        (
            lambda item: replace(item, outcome_observation_id=None),
            "requires an outcome observation",
        ),
        (
            lambda item: replace(item, outcome_observation_digest=None),
            "requires an observation digest",
        ),
        (
            lambda item: replace(item, outcome_evidence_binding_id=None),
            "requires bound outcome evidence",
        ),
        (
            lambda item: replace(item, verdict=EvaluationVerdict.INCONCLUSIVE),
            "does not match",
        ),
    ),
)
def test_outcome_evaluation_cannot_be_forged(mutate, message: str) -> None:
    evaluation = OutcomeEvaluator(clock=lambda: NOW).handle(_command(_observation()))

    with pytest.raises((TypeError, ValueError), match=message):
        mutate(evaluation)


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (lambda item: item.update({"extra": True}), "fields differ"),
        (lambda item: item.update({"schema_version": "outcome-evaluation/v99"}), "unsupported"),
        (lambda item: item.update({"authorization_outcome": "other"}), "violates its contract"),
        (lambda item: item.update({"execution_status": "other"}), "violates its contract"),
        (lambda item: item.update({"initial_state_position": True}), "violates its contract"),
        (lambda item: item.update({"evaluated_at": "not-a-time"}), "violates its contract"),
        (lambda item: item.update({"findings": {}}), "JSON array"),
        (lambda item: item["findings"][0].update({"required": 1}), "findings\\[0\\]"),
        (lambda item: item["findings"][0].update({"expected_value": []}), "findings\\[0\\]"),
        (lambda item: item["findings"][0].update({"observed_claim_ids": {}}), "findings\\[0\\]"),
    ),
)
def test_evaluation_artifact_decoder_rejects_malformed_contracts(mutate, message: str) -> None:
    spec = _spec()
    payload = outcome_evaluation_payload(
        OutcomeEvaluator(clock=lambda: NOW).handle(_command(_observation()))
    )
    mutate(payload)

    with pytest.raises(EvaluationArtifactCodecError, match=message):
        decode_outcome_evaluation(canonical_json_bytes(payload), spec=spec)


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (lambda item: item.update({"extra": True}), "fields differ"),
        (lambda item: item.update({"schema_version": "evaluation-spec/v99"}), "unsupported"),
        (lambda item: item.update({"criteria": {}}), "JSON array"),
        (lambda item: item["criteria"][0].update({"extra": True}), "fields differ"),
        (lambda item: item["criteria"][0].update({"minimum_confidence": True}), "criteria\\[0\\]"),
        (lambda item: item["criteria"][0].update({"required": 1}), "criteria\\[0\\]"),
        (lambda item: item["criteria"][0].update({"expected_value": []}), "criteria\\[0\\]"),
        (lambda item: item.update({"name": " "}), "EvaluationSpec violates"),
    ),
)
def test_evaluation_spec_decoder_rejects_malformed_contracts(mutate, message: str) -> None:
    payload = json.loads(encode_evaluation_spec(_spec()))
    mutate(payload)

    with pytest.raises(EvaluationArtifactCodecError, match=message):
        decode_evaluation_spec(canonical_json_bytes(payload))


def test_artifact_decoders_reject_wrong_transport_types_and_invalid_json() -> None:
    with pytest.raises(TypeError, match="bytes"):
        decode_evaluation_spec(cast("bytes", "not-bytes"))
    with pytest.raises(EvaluationArtifactCodecError, match="canonical JSON"):
        decode_evaluation_spec(b"not-json")


def test_decoder_requires_spec_policy_even_after_attacker_recomputes_identity() -> None:
    spec = _spec()
    payload = outcome_evaluation_payload(
        OutcomeEvaluator(clock=lambda: NOW).handle(_command(_observation()))
    )
    findings = cast("list[dict[str, object]]", payload["findings"])
    findings[0]["expected_value"] = False
    findings[0]["actual_value"] = False
    identity = dict(payload)
    identity.pop("evaluation_id")
    payload["evaluation_id"] = json_digest(identity)

    with pytest.raises(EvaluationArtifactCodecError, match="policy does not match"):
        decode_outcome_evaluation(canonical_json_bytes(payload), spec=spec)


def test_decoder_rejects_recomputed_identity_with_semantically_forged_pass() -> None:
    spec = _spec()
    payload = outcome_evaluation_payload(
        OutcomeEvaluator(clock=lambda: NOW).handle(_command(_observation()))
    )
    findings = cast("list[dict[str, object]]", payload["findings"])
    findings[0]["actual_value"] = False
    identity = dict(payload)
    identity.pop("evaluation_id")
    payload["evaluation_id"] = json_digest(identity)

    with pytest.raises(EvaluationArtifactCodecError, match="violates its contract"):
        decode_outcome_evaluation(canonical_json_bytes(payload), spec=spec)


def test_spec_decoder_enforces_confidence_semantics_after_identity_recomputation() -> None:
    spec = EvaluationSpec(
        "strict",
        "repository is clean",
        (EvaluationCriterion("clean", "repository", "git.clean", True, 0.9),),
    )
    passed = OutcomeEvaluator(clock=lambda: NOW).handle(
        _command(_observation(spec=spec, confidence=0.95), spec=spec)
    )
    definitive_payload = outcome_evaluation_payload(passed)
    definitive_findings = cast(
        "list[dict[str, object]]",
        definitive_payload["findings"],
    )
    definitive_findings[0]["actual_confidence"] = 0.8
    definitive_identity = dict(definitive_payload)
    definitive_identity.pop("evaluation_id")
    definitive_payload["evaluation_id"] = json_digest(definitive_identity)
    with pytest.raises(EvaluationArtifactCodecError, match="below the required confidence"):
        decode_outcome_evaluation(canonical_json_bytes(definitive_payload), spec=spec)

    low = OutcomeEvaluator(clock=lambda: NOW).handle(
        _command(_observation(spec=spec, confidence=0.8), spec=spec)
    )
    low_payload = outcome_evaluation_payload(low)
    low_findings = cast("list[dict[str, object]]", low_payload["findings"])
    low_findings[0]["actual_confidence"] = 0.95
    low_identity = dict(low_payload)
    low_identity.pop("evaluation_id")
    low_payload["evaluation_id"] = json_digest(low_identity)
    with pytest.raises(EvaluationArtifactCodecError, match="meets the required threshold"):
        decode_outcome_evaluation(canonical_json_bytes(low_payload), spec=spec)


def test_blocked_unknown_and_terminal_evaluations_round_trip_with_required_spec() -> None:
    spec = _spec()
    blocked = OutcomeEvaluator(clock=lambda: NOW).handle(
        EvaluateOutcome(
            "run:1",
            spec,
            EvaluationAuthorizationOutcome.DENY,
            None,
            None,
            None,
            None,
            10,
        )
    )
    unknown = OutcomeEvaluator(clock=lambda: NOW).handle(
        EvaluateOutcome(
            "run:1",
            spec,
            EvaluationAuthorizationOutcome.ALLOW,
            EvaluationExecutionStatus.UNKNOWN,
            EXECUTION_EVENT_ID,
            BINDING_ID,
            None,
            10,
        )
    )

    assert decode_outcome_evaluation(encode_outcome_evaluation(blocked), spec=spec) == blocked
    assert decode_outcome_evaluation(encode_outcome_evaluation(unknown), spec=spec) == unknown


def _spec() -> EvaluationSpec:
    return EvaluationSpec(
        "daily-ready",
        "repository is clean",
        (EvaluationCriterion("clean", "repository", "git.clean", True),),
    )


def _observation(
    *,
    value=True,
    confidence: float = 0.95,
    spec: EvaluationSpec | None = None,
    facts: tuple[EvaluationFact, ...] | None = None,
    execution_status: EvaluationExecutionStatus = EvaluationExecutionStatus.SUCCEEDED,
) -> EvaluationObservation:
    resolved = spec or _spec()
    return EvaluationObservation(
        observation_id="outcome:1",
        observation_digest=OBSERVATION_DIGEST,
        evaluation_spec_id=resolved.spec_id,
        execution_binding_id=BINDING_ID,
        execution_status=execution_status,
        status=EvaluationObservationStatus.OBSERVED,
        observed_at=NOW,
        sources=(_source(),),
        facts=(
            EvaluationFact(
                "claim:clean",
                "repository",
                "git.clean",
                value,
                confidence,
                "event:outcome",
            ),
        )
        if facts is None
        else facts,
    )


def _command(
    observation: EvaluationObservation,
    *,
    spec: EvaluationSpec | None = None,
    execution: EvaluationExecutionStatus = EvaluationExecutionStatus.SUCCEEDED,
) -> EvaluateOutcome:
    return EvaluateOutcome(
        "run:1",
        spec or _spec(),
        EvaluationAuthorizationOutcome.ALLOW,
        execution,
        EXECUTION_EVENT_ID,
        BINDING_ID,
        observation,
        10,
    )


def _source(
    *,
    position: int = 11,
    event_type: str = "observation.recorded",
) -> EvaluationSourceEvent:
    return EvaluationSourceEvent(
        "event:outcome",
        position,
        event_type,
        "observations:daily",
        "run:1",
        EXECUTION_EVENT_ID,
        PAYLOAD_HASH,
    )
