"""Plan validation, canonicalization, markers, and ordering."""

import json
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from blackcell.contracts.errors import ValidationFailure
from blackcell.contracts.markers import item_marker, plan_marker
from blackcell.contracts.plan import PlanSpec
from tests.conftest import plan_data


def test_digest_is_stable_across_json_object_key_order() -> None:
    original = plan_data()
    reordered = json.loads(json.dumps(original, sort_keys=True))

    first = PlanSpec.model_validate(original)
    second = PlanSpec.model_validate(reordered)

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.digest() == second.digest()


def test_canonicalization_normalizes_unicode_nfc() -> None:
    composed = plan_data()
    decomposed = plan_data()
    composed["title"] = "Caf\u00e9"
    decomposed["title"] = "Cafe\u0301"

    first = PlanSpec.model_validate(composed)
    second = PlanSpec.model_validate(decomposed)

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.digest() == second.digest()


def test_markers_are_visible_and_deterministic(plan: PlanSpec) -> None:
    digest = str(plan.digest())

    assert plan_marker(plan) == f"blackcell://plan/BCP-0001?revision=1&digest={digest}"
    assert item_marker(plan, plan.work_items[0]) == (
        f"blackcell://item/BCP-0001/BCP-0001-001?digest={digest}"
    )


def test_ordered_work_items_respects_parent_and_dependency(plan: PlanSpec) -> None:
    keys = [item.key for item in plan.ordered_work_items()]

    assert keys.index("BCP-0001-001") < keys.index("BCP-0001-002")
    assert set(keys) == {"BCP-0001-001", "BCP-0001-002", "BCP-0001-003"}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda data: data.update({"plan_id": "BC-1"}),
            "plan_id must match BCP-NNNN",
        ),
        (
            lambda data: data["work_items"][1].update({"parent_key": "BCP-0001-999"}),
            "references missing parent",
        ),
        (
            lambda data: data["work_items"][1].update({"blocked_by": ["BCP-0001-999"]}),
            "references missing dependency",
        ),
        (
            lambda data: data["work_items"][0].update({"acceptance": [" "]}),
            "acceptance criteria",
        ),
        (
            lambda data: data["work_items"][0].update({"labels": ["bad label"]}),
            "invalid label",
        ),
    ],
)
def test_plan_rejects_invalid_contracts(
    mutate: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    data = plan_data()
    mutate(data)

    with pytest.raises(ValidationError, match=message):
        PlanSpec.model_validate(data)


def test_plan_rejects_duplicate_keys() -> None:
    data = plan_data()
    data["work_items"][1]["key"] = data["work_items"][0]["key"]
    data["work_items"][1]["parent_key"] = None
    data["work_items"][1]["blocked_by"] = []

    with pytest.raises(ValidationError, match="work item keys must be unique"):
        PlanSpec.model_validate(data)


def test_plan_rejects_dependency_cycle() -> None:
    data = plan_data()
    data["work_items"][0]["blocked_by"] = ["BCP-0001-002"]
    data["work_items"][1]["parent_key"] = None

    with pytest.raises(ValidationFailure, match="dependencies contain a cycle"):
        PlanSpec.model_validate(data)


def test_array_order_is_part_of_digest() -> None:
    data = plan_data()
    reversed_data = plan_data()
    reversed_data["work_items"].reverse()

    assert PlanSpec.model_validate(data).digest() != PlanSpec.model_validate(reversed_data).digest()
