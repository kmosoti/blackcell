from __future__ import annotations

from typing import Protocol

from blackcell.features.replay_run.command import ReplayRun
from blackcell.features.replay_run.models import (
    DecodedRunProtocol,
    ReplayArtifactVerification,
    ReplayProjectionVerification,
)
from blackcell.kernel import EventEnvelope


class RunHistoryReader(Protocol):
    def read_run(self, command: ReplayRun) -> tuple[EventEnvelope, ...]: ...


class RunProtocolDecoder(Protocol):
    def decode_protocol(
        self,
        command: ReplayRun,
        events: tuple[EventEnvelope, ...],
    ) -> DecodedRunProtocol: ...


class RunArtifactVerifier(Protocol):
    def verify_artifacts(
        self,
        command: ReplayRun,
        protocol: DecodedRunProtocol,
        events: tuple[EventEnvelope, ...],
    ) -> tuple[ReplayArtifactVerification, ...]: ...


class RunProjectionVerifier(Protocol):
    def verify_projections(
        self,
        command: ReplayRun,
        protocol: DecodedRunProtocol,
        events: tuple[EventEnvelope, ...],
    ) -> tuple[ReplayProjectionVerification, ...]: ...


__all__ = [
    "RunArtifactVerifier",
    "RunHistoryReader",
    "RunProjectionVerifier",
    "RunProtocolDecoder",
]
