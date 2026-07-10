from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from blackcell.models.base import (
    ACTION_PROPOSAL_SCHEMA,
    ActionProposal,
    DecisionResult,
    JsonObject,
    ModelInvocation,
    ModelUsage,
    ProposalParser,
    UnknownRecordingError,
    action_proposal_from_mapping,
)

ProposalT = TypeVar("ProposalT")


@dataclass(frozen=True, slots=True)
class RecordedDecision[ProposalT]:
    proposal: ProposalT
    usage: ModelUsage = field(default_factory=ModelUsage)
    response_metadata: Mapping[str, Any] | None = None


class RecordedModel[ProposalT]:
    """Deterministic model fixture keyed by canonical ContextFrame content.

    The replay key is independent of dict insertion order.  A recording can be
    reused any number of times, making it suitable for deterministic tests and
    historical replay without contacting a provider.
    """

    def __init__(
        self,
        recordings: Mapping[str, RecordedDecision[ProposalT] | ProposalT],
        *,
        name: str = "recorded",
        model: str | None = "fixture",
    ) -> None:
        self._recordings = dict(recordings)
        self._name = name
        self._model = model

    @property
    def name(self) -> str:
        return self._name

    @staticmethod
    def key_for(
        context_frame: Mapping[str, Any],
        output_schema: Mapping[str, Any] | None = None,
    ) -> str:
        document = {
            "context_frame": context_frame,
            "output_schema": output_schema or ACTION_PROPOSAL_SCHEMA,
        }
        try:
            encoded = json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        except (TypeError, ValueError) as error:
            raise ValueError("recording keys require JSON-serializable input") from error
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def for_frames(
        cls,
        recordings: Mapping[str, tuple[Mapping[str, Any], ProposalT]],
        *,
        output_schema: Mapping[str, Any] | None = None,
        name: str = "recorded",
    ) -> RecordedModel[ProposalT]:
        keyed = {
            cls.key_for(frame, output_schema): RecordedDecision(proposal)
            for frame, proposal in recordings.values()
        }
        return cls(keyed, name=name)

    def decide(
        self,
        context_frame: Mapping[str, Any],
        *,
        output_schema: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> DecisionResult[ProposalT]:
        key = self.key_for(context_frame, output_schema)
        try:
            raw_recording = self._recordings[key]
        except KeyError as error:
            raise UnknownRecordingError(f"no model recording for key {key}") from error

        recording = (
            raw_recording
            if isinstance(raw_recording, RecordedDecision)
            else RecordedDecision(raw_recording)
        )
        invocation_id = correlation_id or f"recorded-{key[:16]}"
        metadata: JsonObject = {"recording_key": key}
        if recording.response_metadata:
            metadata.update(_safe_metadata(recording.response_metadata))
        invocation = ModelInvocation(
            provider="recorded",
            model=self._model,
            invocation_id=invocation_id,
            replayed=True,
            duration_ms=0.0,
            configuration={"deterministic": True},
            response_metadata=metadata,
            usage=recording.usage,
        )
        return DecisionResult(proposal=recording.proposal, invocation=invocation)


def action_proposal_recording(
    value: Mapping[str, Any],
    parser: ProposalParser[ActionProposal] = action_proposal_from_mapping,
) -> RecordedDecision[ActionProposal]:
    """Build and validate a recording at fixture-construction time."""

    return RecordedDecision(parser(value))


def _safe_metadata(value: Mapping[str, Any]) -> JsonObject:
    # Metadata is intentionally shallow and content-free. Provider payloads do
    # not belong in deterministic recordings.
    safe: JsonObject = {}
    for key, item in value.items():
        if isinstance(key, str) and (item is None or isinstance(item, (str, int, float, bool))):
            safe[key] = item
    return safe
