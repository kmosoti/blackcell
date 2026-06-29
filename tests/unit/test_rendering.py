"""Deterministic Linear presentation rendering and comparison."""

from blackcell.config.model import BlackcellConfig
from blackcell.contracts.plan import PlanSpec
from blackcell.services.rendering import (
    normalize_presentation_text,
    render_project_description,
)


def test_presentation_normalization_accepts_linear_markdown_rewrites() -> None:
    expected = (
        "\n- one\n  - nested\n- two\n\n"
        "[repo](https://github.com/kmosoti/blackcell)\n\n"
        "| A | B |\n| --- | ---: |\n"
    )
    actual = (
        "\r\n* one\r\n  * nested\r\n* two\r\n\r\n"
        "[repo](<https://github.com/kmosoti/blackcell>)\r\n\r\n"
        "| A | B |\r\n| -- | -- |\r\n"
    )

    assert normalize_presentation_text(actual) == normalize_presentation_text(expected)


def test_project_description_exposes_decision_delivery_map_and_contract(
    config: BlackcellConfig, plan: PlanSpec
) -> None:
    content = render_project_description(plan, config)

    assert "## Decision required" in content
    assert "manually move the Project status from `Proposal` to `Approved`" in content
    assert "BlackCell will not approve it" in content
    assert "[kmosoti/blackcell](https://github.com/kmosoti/blackcell)" in content
    assert "| Assignment | Title | Type | Priority | Dependencies | Acceptance |" in content
    assert "| `BCP-0001-002` | Dependent child | `task` | `medium` |" in content
    assert "## Authority and workflow" in content
    assert "## Approval gate" in content
    assert content.rfind("## Machine contract") > content.rfind("## Approval gate")
