from __future__ import annotations

from typing import cast

import pytest

from blackcell.features.request_decision import DecisionArgument, DecisionProposal
from blackcell.kernel import JsonScalar
from blackcell.workflows.decision_bridge import action_proposal_from_decision


def test_bridge_preserves_model_proposal_semantics_under_a_new_owner_identity() -> None:
    model = DecisionProposal(
        proposal_id="proposal:daily-plan",
        context_frame_id=f"sha256:{'1' * 64}",
        affordance="repository.inspect",
        arguments=(
            DecisionArgument("z_limit", 10),
            DecisionArgument("include_hidden", False),
            DecisionArgument("path", "README.md"),
        ),
        rationale="inspect the bounded evidence before planning",
        evidence_event_ids=("event:1", "event:2"),
    )

    action = action_proposal_from_decision(model)

    assert action.proposal_id == model.proposal_id
    assert action.context_frame_id == model.context_frame_id
    assert action.affordance == model.affordance
    assert tuple((item.name, item.value) for item in action.arguments) == tuple(
        (item.name, item.value) for item in model.arguments
    )
    assert action.rationale == model.rationale
    assert action.evidence_event_ids == model.evidence_event_ids
    assert action.proposal_digest != model.proposal_digest
    assert action.schema_version == "action-proposal/v2"
    assert model.schema_version == "decision-proposal/v1"


@pytest.mark.parametrize("value", (None, True, 7, 1.25, "literal; $(not-a-shell)"))
def test_bridge_preserves_each_json_scalar_without_string_coercion(value: JsonScalar) -> None:
    model = DecisionProposal(
        "proposal:scalar",
        f"sha256:{'2' * 64}",
        "inspect",
        (DecisionArgument("value", value),),
        "preserve the typed value",
        (),
    )

    action = action_proposal_from_decision(model)

    assert action.arguments[0].value == value
    assert type(action.arguments[0].value) is type(value)


def test_bridge_rejects_untyped_inputs() -> None:
    with pytest.raises(TypeError, match="DecisionProposal"):
        action_proposal_from_decision(cast("DecisionProposal", object()))
