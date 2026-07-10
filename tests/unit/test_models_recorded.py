from __future__ import annotations

import pytest

from blackcell.control import ActionArgument
from blackcell.models import (
    ACTION_PROPOSAL_SCHEMA,
    ActionProposal,
    DecisionModel,
    RecordedDecision,
    RecordedModel,
    UnknownRecordingError,
    action_proposal_from_mapping,
)


def _proposal() -> ActionProposal:
    return ActionProposal(
        proposal_id="proposal-1",
        context_frame_id="frame-1",
        affordance="inspect_file",
        arguments=(ActionArgument("path", "pyproject.toml"),),
        expected_effects=(),
        rationale="Inspect the dependency declaration.",
        evidence_ids=("e-1",),
    )


def test_recorded_model_replays_by_canonical_frame_key() -> None:
    frame = {"objective": "inspect", "facts": [{"id": "e-1", "value": 1}]}
    proposal = _proposal()
    key = RecordedModel.key_for(frame)
    model = RecordedModel({key: RecordedDecision(proposal, response_metadata={"fixture": "one"})})

    result = model.decide({"facts": [{"value": 1, "id": "e-1"}], "objective": "inspect"})

    assert isinstance(model, DecisionModel)
    assert result.proposal == proposal
    assert result.invocation.replayed is True
    assert result.invocation.duration_ms == 0
    assert result.invocation.response_metadata["recording_key"] == key


def test_recorded_model_key_includes_schema() -> None:
    frame = {"objective": "inspect"}

    assert RecordedModel.key_for(frame) != RecordedModel.key_for(
        frame, {**ACTION_PROPOSAL_SCHEMA, "title": "different"}
    )


def test_recorded_model_rejects_unknown_frame() -> None:
    model: RecordedModel[ActionProposal] = RecordedModel({})

    with pytest.raises(UnknownRecordingError, match="no model recording"):
        model.decide({"objective": "unknown"})


def test_action_proposal_parser_rejects_ambient_authority_objects() -> None:
    with pytest.raises(ValueError, match="JSON scalar"):
        action_proposal_from_mapping(
            {
                "proposal_id": "proposal-1",
                "context_frame_id": "frame-1",
                "affordance": "inspect_file",
                "arguments": [{"name": "executor", "value": object()}],
                "expected_effects": [],
                "rationale": "Inspect first.",
                "required_evidence": [],
                "evidence_ids": [],
                "assertions": [],
            }
        )
