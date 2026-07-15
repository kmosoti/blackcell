from __future__ import annotations

from blackcell.features.replay_run.command import ReplayRun
from blackcell.features.replay_run.models import (
    ReplayClassification,
    ReplayFinding,
    ReplayIntegrityError,
    RunNotFoundError,
    RunReplayReport,
)
from blackcell.features.replay_run.ports import (
    RunArtifactVerifier,
    RunHistoryReader,
    RunProjectionVerifier,
    RunProtocolDecoder,
)
from blackcell.kernel import EventEnvelope


class ReplayRunHandler:
    """Verify recorded history through read-only capabilities only."""

    def __init__(
        self,
        history: RunHistoryReader,
        protocol: RunProtocolDecoder,
        artifacts: RunArtifactVerifier,
        projections: RunProjectionVerifier,
    ) -> None:
        self._history = history
        self._protocol = protocol
        self._artifacts = artifacts
        self._projections = projections

    def handle(self, command: ReplayRun) -> RunReplayReport:
        try:
            events = self._history.read_run(command)
        except ReplayIntegrityError as error:
            return _corrupt_report(
                command,
                events=(),
                stream_id=error.run_stream_id or command.run_id,
                error=error,
            )
        if not events:
            raise RunNotFoundError(f"run {command.run_id!r} does not exist")
        stream_id = events[0].stream_id
        protocol_version: str | None = None
        try:
            decoded = self._protocol.decode_protocol(command, events)
            protocol_version = decoded.protocol_version
            artifacts = self._artifacts.verify_artifacts(command, decoded, events)
            projections = self._projections.verify_projections(command, decoded, events)
        except ReplayIntegrityError as error:
            return _corrupt_report(
                command,
                events=events,
                stream_id=stream_id,
                error=error,
                protocol_version=protocol_version,
            )
        return RunReplayReport(
            run_id=command.run_id,
            run_stream_id=decoded.run_stream_id,
            protocol_version=decoded.protocol_version,
            classification=decoded.classification,
            outcome=decoded.outcome,
            events=events,
            artifacts=artifacts,
            projections=projections,
        )


def _corrupt_report(
    command: ReplayRun,
    *,
    events: tuple[EventEnvelope, ...],
    stream_id: str,
    error: ReplayIntegrityError,
    protocol_version: str | None = None,
) -> RunReplayReport:
    return RunReplayReport(
        run_id=command.run_id,
        run_stream_id=stream_id,
        protocol_version=error.protocol_version or protocol_version,
        classification=ReplayClassification.CORRUPT,
        outcome=None,
        events=events,
        artifacts=(),
        projections=(),
        finding=ReplayFinding(error.stage, error.code, error.public_message),
    )


__all__ = ["ReplayRunHandler"]
