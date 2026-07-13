from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import cast

from blackcell.adapters.persistence.sqlite.run_records import KernelRunRecorder
from blackcell.adapters.persistence.sqlite.run_records_v2 import KernelFeedbackRunRecorder
from blackcell.features.execute_affordance import (
    ExecutionEvidenceJournal,
    ExecutionJournalError,
)
from blackcell.features.replay_run import (
    DecodedRunProtocol,
    ReplayArtifactVerification,
    ReplayClassification,
    ReplayIntegrityError,
    ReplayIntegrityStage,
    ReplayProjectionStage,
    ReplayProjectionVerification,
    ReplayRun,
    ReplayVerificationStatus,
)
from blackcell.features.request_decision import (
    DecisionEvidenceJournal,
    DecisionJournalError,
)
from blackcell.kernel import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
    EventEnvelope,
    EventIntegrityError,
    EventStore,
)
from blackcell.workflows.run_grammar import validate_run_grammar
from blackcell.workflows.run_protocol import (
    INITIAL_STATE_RECORDED,
    OUTCOME_STATE_RECORDED,
    RUN_FAILED,
    RUN_WORKFLOW_VERSION_V1,
    RUN_WORKFLOW_VERSION_V2,
    RunProtocolIntegrityError,
    RunProtocolVersion,
    run_stream_id,
)

_PROJECTION_ERROR_MARKERS = (
    "cutoff",
    "initial state",
    "outcome state",
    "projection",
    "snapshot",
    "state transition",
)


class KernelRunReplayAdapter:
    """Read and verify v1/v2 run history without exposing recorder mutation methods."""

    def __init__(
        self,
        events: EventStore,
        artifacts: ArtifactStore,
        decision_evidence: DecisionEvidenceJournal,
        execution_evidence: ExecutionEvidenceJournal,
    ) -> None:
        self._events = events
        self._v1 = KernelRunRecorder(events, artifacts, clock=_forbidden_replay_clock)
        self._v2 = KernelFeedbackRunRecorder(
            events,
            artifacts,
            decision_evidence,
            execution_evidence,
            clock=_forbidden_replay_clock,
        )

    def read_run(self, command: ReplayRun) -> tuple[EventEnvelope, ...]:
        stream_id = run_stream_id(command.run_id)
        try:
            return self._events.read_stream(stream_id)
        except (EventIntegrityError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise ReplayIntegrityError(
                ReplayIntegrityStage.PROTOCOL,
                "event-envelope-invalid",
                str(error) or "recorded event envelope is invalid",
                run_stream_id=stream_id,
            ) from error

    def decode_protocol(
        self,
        command: ReplayRun,
        events: tuple[EventEnvelope, ...],
    ) -> DecodedRunProtocol:
        version = _safe_protocol_version(events)
        try:
            grammar = validate_run_grammar(events, run_id=command.run_id)
            if any(event.global_position is None for event in events):
                raise RunProtocolIntegrityError(
                    "historical replay requires stored event occurrences"
                )
            positions = tuple(cast("int", event.global_position) for event in events)
            if tuple(sorted(set(positions))) != positions:
                raise RunProtocolIntegrityError(
                    "historical run positions must be strictly increasing"
                )
            terminal = events[-1]
            classification = ReplayClassification.INTERRUPTED
            outcome: str | None = None
            if grammar.terminal:
                outcome = _payload_text(terminal.payload, "outcome")
                classification = (
                    ReplayClassification.FAILED
                    if terminal.event_type == RUN_FAILED
                    else ReplayClassification.COMPLETED
                )
            return DecodedRunProtocol(
                command.run_id,
                run_stream_id(command.run_id),
                grammar.protocol_version.value,
                classification,
                outcome,
            )
        except (RunProtocolIntegrityError, TypeError, ValueError) as error:
            raise ReplayIntegrityError(
                ReplayIntegrityStage.PROTOCOL,
                "run-protocol-invalid",
                str(error) or "recorded run protocol is invalid",
                protocol_version=version,
            ) from error

    def verify_artifacts(
        self,
        command: ReplayRun,
        protocol: DecodedRunProtocol,
        events: tuple[EventEnvelope, ...],
    ) -> tuple[ReplayArtifactVerification, ...]:
        try:
            if protocol.protocol_version == RUN_WORKFLOW_VERSION_V1:
                self._v1.verify_history(command.run_id, events)
            elif protocol.protocol_version == RUN_WORKFLOW_VERSION_V2:
                self._v2.verify_history(command.run_id, events)
            else:  # pragma: no cover - protocol decoder owns this invariant
                raise RunProtocolIntegrityError("run workflow version is unsupported")
            return _artifact_catalog(events)
        except _INTEGRITY_ERRORS as error:
            message = str(error) or "recorded run evidence is invalid"
            projection_error = any(
                marker in message.lower() for marker in _PROJECTION_ERROR_MARKERS
            )
            raise ReplayIntegrityError(
                (
                    ReplayIntegrityStage.PROJECTION
                    if projection_error
                    else ReplayIntegrityStage.ARTIFACT
                ),
                (
                    "projection-evidence-invalid"
                    if projection_error
                    else "recorded-evidence-invalid"
                ),
                message,
                protocol_version=protocol.protocol_version,
            ) from error

    def verify_projections(
        self,
        command: ReplayRun,
        protocol: DecodedRunProtocol,
        events: tuple[EventEnvelope, ...],
    ) -> tuple[ReplayProjectionVerification, ...]:
        del command
        if protocol.protocol_version == RunProtocolVersion.V1.value:
            return _unrecorded_projections()
        try:
            return (
                _projection(events, INITIAL_STATE_RECORDED, ReplayProjectionStage.INITIAL),
                _projection(events, OUTCOME_STATE_RECORDED, ReplayProjectionStage.OUTCOME),
            )
        except (RunProtocolIntegrityError, TypeError, ValueError) as error:
            raise ReplayIntegrityError(
                ReplayIntegrityStage.PROJECTION,
                "projection-metadata-invalid",
                str(error) or "recorded projection metadata is invalid",
                protocol_version=protocol.protocol_version,
            ) from error


_INTEGRITY_ERRORS = (
    RunProtocolIntegrityError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    DecisionJournalError,
    ExecutionJournalError,
    json.JSONDecodeError,
    UnicodeDecodeError,
    TypeError,
    ValueError,
)


def _artifact_catalog(
    events: tuple[EventEnvelope, ...],
) -> tuple[ReplayArtifactVerification, ...]:
    found: list[ReplayArtifactVerification] = []
    for event in events:
        for field, value in sorted(event.payload.items()):
            if field != "artifact" and not field.endswith("_artifact"):
                continue
            if value is None:
                continue
            if not isinstance(value, Mapping):
                raise RunProtocolIntegrityError(
                    f"{event.event_type}.{field} artifact link must be an object"
                )
            digest = _payload_text(cast("Mapping[str, object]", value), "digest")
            found.append(
                ReplayArtifactVerification(
                    event.event_id,
                    event.event_type,
                    event.stream_sequence,
                    field,
                    digest,
                )
            )
    return tuple(found)


def _projection(
    events: tuple[EventEnvelope, ...],
    event_type: str,
    stage: ReplayProjectionStage,
) -> ReplayProjectionVerification:
    event = next((item for item in events if item.event_type == event_type), None)
    if event is None:
        return ReplayProjectionVerification(stage, ReplayVerificationStatus.NOT_RECORDED)
    digest = _payload_text(event.payload, "snapshot_digest")
    position = event.payload.get("cutoff_global_position")
    if isinstance(position, bool) or not isinstance(position, int) or position < 0:
        raise ValueError("projection cutoff must be a non-negative integer")
    cutoff = _payload_text(event.payload, "effective_time_cutoff")
    return ReplayProjectionVerification(
        stage,
        ReplayVerificationStatus.VERIFIED,
        digest,
        position,
        datetime.fromisoformat(cutoff),
    )


def _unrecorded_projections() -> tuple[ReplayProjectionVerification, ...]:
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


def _safe_protocol_version(events: tuple[EventEnvelope, ...]) -> str | None:
    if not events:
        return None
    value = events[0].payload.get("workflow_version")
    if value in {RUN_WORKFLOW_VERSION_V1, RUN_WORKFLOW_VERSION_V2}:
        return cast("str", value)
    return None


def _payload_text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RunProtocolIntegrityError(f"run replay field {field!r} must be non-empty text")
    return value


def _forbidden_replay_clock() -> datetime:
    raise AssertionError("historical replay attempted to use a live clock")


__all__ = ["KernelRunReplayAdapter"]
