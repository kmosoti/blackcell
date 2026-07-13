from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from blackcell.features.replay_run import (
    DecodedRunProtocol,
    ReplayArtifactVerification,
    ReplayClassification,
    ReplayIntegrityError,
    ReplayIntegrityStage,
    ReplayProjectionStage,
    ReplayProjectionVerification,
    ReplayRun,
    ReplayRunHandler,
    ReplayVerificationStatus,
    RunNotFoundError,
)
from blackcell.kernel import EventEnvelope

RUN_ID = "run:replay:1"
STREAM_ID = f"daily-operator-run:{RUN_ID}"
DIGEST = f"sha256:{'a' * 64}"
NOW = datetime(2026, 7, 13, tzinfo=UTC)


class ReplayPorts:
    def __init__(
        self,
        classification: ReplayClassification = ReplayClassification.COMPLETED,
        outcome: str | None = "executed",
        *,
        corrupt: bool = False,
    ) -> None:
        self.classification = classification
        self.outcome = outcome
        self.corrupt = corrupt
        self.calls: list[str] = []

    def read_run(self, command: ReplayRun) -> tuple[EventEnvelope, ...]:
        self.calls.append("history")
        return (_event(command.run_id),)

    def decode_protocol(
        self,
        command: ReplayRun,
        events: tuple[EventEnvelope, ...],
    ) -> DecodedRunProtocol:
        del events
        self.calls.append("protocol")
        if self.corrupt:
            raise ReplayIntegrityError(
                ReplayIntegrityStage.PROTOCOL,
                "fixture-corrupt",
                "fixture history is corrupt",
                protocol_version="daily-operator/v2",
            )
        return DecodedRunProtocol(
            command.run_id,
            STREAM_ID,
            "daily-operator/v2",
            self.classification,
            self.outcome,
        )

    def verify_artifacts(
        self,
        command: ReplayRun,
        protocol: DecodedRunProtocol,
        events: tuple[EventEnvelope, ...],
    ) -> tuple[ReplayArtifactVerification, ...]:
        del command, protocol
        self.calls.append("artifacts")
        return (
            ReplayArtifactVerification(
                events[0].event_id,
                events[0].event_type,
                1,
                "artifact",
                DIGEST,
            ),
        )

    def verify_projections(
        self,
        command: ReplayRun,
        protocol: DecodedRunProtocol,
        events: tuple[EventEnvelope, ...],
    ) -> tuple[ReplayProjectionVerification, ...]:
        del command, protocol, events
        self.calls.append("projections")
        return (
            ReplayProjectionVerification(
                ReplayProjectionStage.INITIAL,
                ReplayVerificationStatus.NOT_RECORDED,
            ),
            ReplayProjectionVerification(
                ReplayProjectionStage.OUTCOME,
                ReplayVerificationStatus.NOT_RECORDED,
            ),
        )


class EmptyHistory(ReplayPorts):
    def read_run(self, command: ReplayRun) -> tuple[EventEnvelope, ...]:
        del command
        return ()


class FailingHistory(ReplayPorts):
    def read_run(self, command: ReplayRun) -> tuple[EventEnvelope, ...]:
        del command
        raise OSError("storage unavailable")


def test_replay_run_accepts_only_an_explicit_nonempty_run_id() -> None:
    assert tuple(inspect.signature(ReplayRun).parameters) == ("run_id",)
    with pytest.raises(ValueError, match="run_id"):
        ReplayRun(" ")


@pytest.mark.parametrize(
    ("classification", "outcome"),
    (
        (ReplayClassification.COMPLETED, "execution-failed"),
        (ReplayClassification.FAILED, "failed"),
        (ReplayClassification.INTERRUPTED, None),
    ),
)
def test_handler_keeps_history_classification_separate_from_material_outcome(
    classification: ReplayClassification,
    outcome: str | None,
) -> None:
    ports = ReplayPorts(classification, outcome)
    handler = ReplayRunHandler(ports, ports, ports, ports)

    report = handler.handle(ReplayRun(RUN_ID))

    assert report.classification is classification
    assert report.outcome == outcome
    assert report.event_count == 1
    assert report.finding is None
    assert ports.calls == ["history", "protocol", "artifacts", "projections"]


def test_handler_classifies_only_recognized_integrity_errors_as_corrupt() -> None:
    ports = ReplayPorts(corrupt=True)
    handler = ReplayRunHandler(ports, ports, ports, ports)

    report = handler.handle(ReplayRun(RUN_ID))

    assert report.classification is ReplayClassification.CORRUPT
    assert report.protocol_version == "daily-operator/v2"
    assert report.outcome is None
    assert report.artifacts == ()
    assert report.projections == ()
    assert report.finding is not None
    assert report.finding.code == "fixture-corrupt"
    assert ports.calls == ["history", "protocol"]


def test_missing_history_is_not_mislabeled_as_corruption() -> None:
    ports = ReplayPorts()
    handler = ReplayRunHandler(EmptyHistory(), ports, ports, ports)

    with pytest.raises(RunNotFoundError, match=RUN_ID):
        handler.handle(ReplayRun(RUN_ID))


def test_infrastructure_failure_propagates_instead_of_claiming_corruption() -> None:
    ports = ReplayPorts()
    handler = ReplayRunHandler(FailingHistory(), ports, ports, ports)

    with pytest.raises(OSError, match="storage unavailable"):
        handler.handle(ReplayRun(RUN_ID))


def test_handler_constructor_exposes_only_the_four_read_ports() -> None:
    assert tuple(inspect.signature(ReplayRunHandler).parameters) == (
        "history",
        "protocol",
        "artifacts",
        "projections",
    )


def _event(run_id: str) -> EventEnvelope:
    event = EventEnvelope.create(
        stream_id=f"daily-operator-run:{run_id}",
        stream_sequence=1,
        event_type="fixture.event",
        schema_version=1,
        actor="fixture",
        source="fixture",
        payload={"run_id": run_id},
        recorded_at=NOW,
        effective_at=NOW,
        correlation_id=run_id,
    )
    return replace(event, global_position=1)
