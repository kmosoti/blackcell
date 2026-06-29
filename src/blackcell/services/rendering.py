"""Human-readable Linear descriptions with deterministic visible markers."""

import re

from blackcell.config.model import BlackcellConfig
from blackcell.contracts.markers import item_marker, plan_marker
from blackcell.contracts.plan import PlanSpec, WorkItemSpec

_LIST_BULLET = re.compile(r"^([ \t]*)[*-] ", re.MULTILINE)
_LINK_DESTINATION = re.compile(r"\]\(<([^>\n]+)>\)")
_TABLE_SEPARATOR_CELL = re.compile(r"^:?-{2,}:?$")


def normalize_presentation_text(value: str | None) -> str:
    normalized = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = _LIST_BULLET.sub(r"\1- ", normalized)
    normalized = _LINK_DESTINATION.sub(r"](\1)", normalized)
    lines = []
    for line in normalized.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) > 1 and all(_TABLE_SEPARATOR_CELL.fullmatch(cell) for cell in cells):
            line = "| " + " | ".join("---" for _ in cells) + " |"
        lines.append(line)
    return "\n".join(lines).strip()


def repository_url(plan: PlanSpec) -> str:
    return f"https://github.com/{plan.repository.owner}/{plan.repository.name}"


def render_project_summary(plan: PlanSpec) -> str:
    return f"{plan.plan_id} r{plan.revision}: {plan.objective}"[:255]


def render_project_description(plan: PlanSpec, config: BlackcellConfig) -> str:
    brand = config.linear.project_presentation.brand
    work_items = "\n".join(
        "| "
        f"`{item.key}` | {item.title} | `{item.type.value}` | `{item.priority.value}` | "
        f"{', '.join(f'`{key}`' for key in item.blocked_by) or 'None'} | "
        f"{len(item.acceptance)} |"
        for item in plan.work_items
    )
    repository = f"{plan.repository.owner}/{plan.repository.name}"
    return (
        f"# {plan.title}\n\n"
        f"## Decision required\n\n"
        f"Review this proposal in Linear. If it is approved without changes, manually move "
        f"the Project status from `{config.linear.project_statuses.proposal}` to "
        f"`{config.linear.project_statuses.approved}`. {brand} will not approve it.\n\n"
        f"## Outcome\n\n{plan.objective}\n\n"
        f"## Repository\n\n"
        f"[{repository}]({repository_url(plan)})\n\n"
        f"## Delivery map\n\n"
        f"| Assignment | Title | Type | Priority | Dependencies | Acceptance |\n"
        f"| --- | --- | --- | --- | --- | ---: |\n"
        f"{work_items}\n\n"
        f"## Authority and workflow\n\n"
        f"- Linear owns planning state, approval, priority, hierarchy, and dependencies.\n"
        f"- GitHub owns repository state, code, review, and merge.\n"
        f"- {brand} may materialize assignments only after manual approval.\n"
        f"- GitHub issue echoes are verified read-only through Linear's native integration.\n\n"
        f"## Approval gate\n\n"
        f"- Approval is a manual owner action in Linear.\n"
        f"- An approved directive is immutable. Any digest or presentation divergence is "
        f"an anomaly, not an implicit update.\n\n"
        f"## Machine contract\n\n"
        f"- Revision: `{plan.revision}`\n"
        f"- Digest: `{plan.digest()}`\n"
        f"- Marker: `{plan_marker(plan)}`\n\n"
        f"{plan_marker(plan)}"
    )


def render_issue_description(plan: PlanSpec, item: WorkItemSpec) -> str:
    acceptance = "\n".join(f"- [ ] {criterion}" for criterion in item.acceptance)
    dependencies = ", ".join(f"`{key}`" for key in item.blocked_by) or "None"
    parent = f"`{item.parent_key}`" if item.parent_key else "None"
    return (
        f"{item.description}\n\n"
        f"## Acceptance criteria\n\n{acceptance}\n\n"
        f"## Planning metadata\n\n"
        f"- Type: `{item.type.value}`\n"
        f"- Priority: `{item.priority.value}`\n"
        f"- Parent: {parent}\n"
        f"- Blocked by: {dependencies}\n\n"
        f"{item_marker(plan, item)}"
    )
