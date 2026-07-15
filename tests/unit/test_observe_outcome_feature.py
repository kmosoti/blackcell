from __future__ import annotations

import json
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from blackcell.features.observe_outcome import (
    CollectOutcomeHandler,
    ObserveOutcome,
    OutcomeArgument,
    OutcomeArtifactCodecError,
    OutcomeClaim,
    OutcomeEvidencePointer,
    OutcomeExecutionBinding,
    OutcomeObservation,
    OutcomeObservationContractError,
    OutcomeObservationStatus,
    OutcomeTarget,
    decode_outcome_observation,
    encode_outcome_observation,
)
from blackcell.kernel._json import canonical_json_bytes

NOW = datetime(2026, 7, 12, 12, tzinfo=UTC)
SPEC_ID = f"sha256:{'1' * 64}"


class Observer:
    observer_id = "fixture-observer"
    contract_version = "fixture-observer/v1"

    def __init__(self, observation: OutcomeObservation) -> None:
        self.observation = observation
        self.calls: list[ObserveOutcome] = []

    def observe(self, command: ObserveOutcome) -> OutcomeObservation:
        self.calls.append(command)
        return self.observation


def test_observer_command_contains_only_execution_binding_scope_and_targets() -> None:
    command = _command(
        targets=(
            OutcomeTarget("repository", "git.clean"),
            OutcomeTarget("path:README.md", "present"),
        )
    )

    assert tuple(item.key for item in command.targets) == (
        ("path:README.md", "present"),
        ("repository", "git.clean"),
    )
    assert {item.name for item in fields(ObserveOutcome)} == {
        "binding",
        "evaluation_spec_id",
        "domain",
        "stream_id",
        "targets",
    }
    assert not hasattr(command, "expected_values")
    with pytest.raises(ValueError, match="at least one target"):
        replace(command, targets=())
    with pytest.raises(ValueError, match="targets must be unique"):
        replace(command, targets=(command.targets[0], command.targets[0]))


def test_observation_statuses_require_explicit_evidence_and_honest_claims() -> None:
    observed = _observation()
    inconclusive = replace(
        observed,
        observation_id="outcome:inconclusive",
        status=OutcomeObservationStatus.INCONCLUSIVE,
        claims=(),
    )

    assert observed.status is OutcomeObservationStatus.OBSERVED
    assert inconclusive.status is OutcomeObservationStatus.INCONCLUSIVE
    with pytest.raises(ValueError, match="requires at least one claim"):
        replace(observed, claims=())
    with pytest.raises(ValueError, match="cannot assert claims"):
        replace(inconclusive, claims=observed.claims)
    with pytest.raises(ValueError, match="explicit evidence"):
        replace(observed, evidence=())
    with pytest.raises(ValueError, match="explicit evidence"):
        replace(inconclusive, evidence=())


def test_models_normalize_set_semantic_values_and_reject_ambiguous_evidence() -> None:
    binding = _binding(
        arguments=(OutcomeArgument("z", 2), OutcomeArgument("a", 1)),
    )
    observation = _observation(
        binding=binding,
        claims=(
            OutcomeClaim("claim:z", "repository", "git.clean", True),
            OutcomeClaim("claim:a", "path:README.md", "present", True),
        ),
        evidence=(
            OutcomeEvidencePointer(locator="fixture://z"),
            OutcomeEvidencePointer(locator="fixture://a"),
        ),
    )

    assert tuple(item.name for item in binding.arguments) == ("a", "z")
    assert tuple(item.claim_id for item in observation.claims) == ("claim:a", "claim:z")
    assert tuple(item.locator for item in observation.evidence) == (
        "fixture://a",
        "fixture://z",
    )
    with pytest.raises(ValueError, match="argument names must be unique"):
        replace(binding, arguments=(OutcomeArgument("a", 1), OutcomeArgument("a", 2)))
    with pytest.raises(ValueError, match="claim ids must be unique"):
        replace(observation, claims=(observation.claims[0], observation.claims[0]))
    with pytest.raises(ValueError, match="evidence pointers must be unique"):
        replace(observation, evidence=(observation.evidence[0], observation.evidence[0]))
    with pytest.raises(ValueError, match="requires a locator"):
        OutcomeEvidencePointer()
    with pytest.raises(ValueError, match="SHA-256"):
        OutcomeEvidencePointer(digest="sha256:bad")
    with pytest.raises(ValueError, match="between zero and one"):
        OutcomeClaim("claim:bad", "repository", "git.clean", True, 1.1)


def test_handler_accepts_one_exact_target_bounded_observation() -> None:
    command = _command()
    observation = _observation(binding=command.binding)
    observer = Observer(observation)

    returned = CollectOutcomeHandler(observer).handle(command)

    assert returned is observation
    assert observer.calls == [command]


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (
            lambda item: replace(
                item,
                binding=replace(item.binding, run_id="run:other"),
            ),
            "different execution binding",
        ),
        (
            lambda item: replace(item, evaluation_spec_id=f"sha256:{'2' * 64}"),
            "different evaluation specification",
        ),
        (lambda item: replace(item, domain="other"), "different operational-state scope"),
        (lambda item: replace(item, stream_id="observations:other"), "scope"),
        (lambda item: replace(item, observer_id="other"), "identity"),
        (
            lambda item: replace(item, observer_contract_version="other/v1"),
            "contract version",
        ),
        (
            lambda item: replace(
                item,
                observed_at=item.binding.completed_at - timedelta(seconds=1),
            ),
            "cannot precede execution",
        ),
        (
            lambda item: replace(
                item,
                claims=(OutcomeClaim("claim:other", "other", "status", "ready"),),
            ),
            "outside requested targets",
        ),
    ),
)
def test_handler_rejects_every_observer_boundary_mismatch(mutate, message: str) -> None:
    command = _command()
    observer = Observer(mutate(_observation(binding=command.binding)))

    with pytest.raises(OutcomeObservationContractError, match=message):
        CollectOutcomeHandler(observer).handle(command)

    assert observer.calls == [command]


def test_handler_rejects_invalid_observer_metadata_and_result_type() -> None:
    observation = _observation()
    observer = Observer(observation)
    observer.observer_id = " "
    with pytest.raises(ValueError, match="observer_id"):
        CollectOutcomeHandler(observer)

    observer.observer_id = "fixture-observer"
    observer.contract_version = ""
    with pytest.raises(ValueError, match="contract_version"):
        CollectOutcomeHandler(observer)

    class WrongTypeObserver(Observer):
        contract_version = "fixture/v1"

        def observe(self, command: ObserveOutcome):
            self.calls.append(command)
            return {"status": "observed"}

    with pytest.raises(OutcomeObservationContractError, match="unsupported result type"):
        CollectOutcomeHandler(WrongTypeObserver(observation)).handle(_command())


def test_outcome_artifact_codec_is_canonical_strict_and_identity_checked() -> None:
    observation = _codec_observation()

    encoded = encode_outcome_observation(observation)

    assert canonical_json_bytes(json.loads(encoded)) == encoded
    assert decode_outcome_observation(encoded) == observation
    with pytest.raises(OutcomeArtifactCodecError, match="canonical JSON"):
        decode_outcome_observation(encoded + b"\n")
    with pytest.raises(TypeError, match="must be bytes"):
        decode_outcome_observation(cast("bytes", "not-bytes"))


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (lambda item: item.update({"extra": True}), "fields differ"),
        (
            lambda item: item["binding"].update({"binding_id": f"sha256:{'9' * 64}"}),
            "binding_id does not match",
        ),
        (
            lambda item: item.update({"observation_digest": f"sha256:{'8' * 64}"}),
            "observation_digest does not match",
        ),
        (lambda item: item.update({"schema_version": "outcome-observation/v99"}), "unsupported"),
        (lambda item: item.update({"status": "unknown"}), "not recognized"),
        (lambda item: item.update({"claims": {"not": "an array"}}), "must be a JSON array"),
        (
            lambda item: item["claims"].reverse(),
            "canonical domain ordering",
        ),
    ),
)
def test_outcome_artifact_codec_rejects_schema_and_identity_tampering(mutate, message: str) -> None:
    payload = json.loads(encode_outcome_observation(_codec_observation()))
    mutate(payload)

    with pytest.raises(OutcomeArtifactCodecError, match=message):
        decode_outcome_observation(canonical_json_bytes(payload))


def _command(
    *,
    binding: OutcomeExecutionBinding | None = None,
    targets: tuple[OutcomeTarget, ...] = (OutcomeTarget("repository", "git.clean"),),
) -> ObserveOutcome:
    return ObserveOutcome(
        binding or _binding(),
        SPEC_ID,
        "repository",
        "observations:daily",
        targets,
    )


def _binding(
    *,
    arguments: tuple[OutcomeArgument, ...] = (OutcomeArgument("path", "README.md"),),
) -> OutcomeExecutionBinding:
    return OutcomeExecutionBinding(
        run_id="run:1",
        invocation_id="invocation:1",
        proposal_id="proposal:1",
        proposal_digest=f"sha256:{'2' * 64}",
        authorization_decision_id="authorization:1",
        authorized_action_digest=f"sha256:{'3' * 64}",
        execution_result_id=f"sha256:{'4' * 64}",
        execution_identity_digest=f"sha256:{'5' * 64}",
        execution_status="succeeded",
        affordance="inspect",
        arguments=arguments,
        execution_adapter_id="fixture-executor",
        execution_adapter_contract_version="fixture-executor/v1",
        completed_at=NOW,
    )


def _observation(
    *,
    binding: OutcomeExecutionBinding | None = None,
    claims: tuple[OutcomeClaim, ...] = (
        OutcomeClaim("claim:outcome", "repository", "git.clean", True, 0.95),
    ),
    evidence: tuple[OutcomeEvidencePointer, ...] = (
        OutcomeEvidencePointer(
            locator="fixture://repository/status",
            digest=f"sha256:{'6' * 64}",
        ),
    ),
) -> OutcomeObservation:
    return OutcomeObservation(
        observation_id="outcome:1",
        binding=binding or _binding(),
        evaluation_spec_id=SPEC_ID,
        domain="repository",
        stream_id="observations:daily",
        observer_id="fixture-observer",
        observer_contract_version="fixture-observer/v1",
        status=OutcomeObservationStatus.OBSERVED,
        observed_at=NOW + timedelta(seconds=1),
        claims=claims,
        evidence=evidence,
    )


def _codec_observation() -> OutcomeObservation:
    return _observation(
        binding=_binding(
            arguments=(OutcomeArgument("a", 1), OutcomeArgument("z", 2)),
        ),
        claims=(
            OutcomeClaim("claim:a", "repository", "git.clean", True),
            OutcomeClaim("claim:z", "repository", "git.clean", False),
        ),
        evidence=(
            OutcomeEvidencePointer(locator="fixture://a"),
            OutcomeEvidencePointer(locator="fixture://z"),
        ),
    )
