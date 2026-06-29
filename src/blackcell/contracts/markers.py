"""Sync-safe deterministic remote markers."""

from urllib.parse import quote

from blackcell.contracts.plan import PlanDigest, PlanSpec, WorkItemSpec


def plan_marker(plan: PlanSpec, digest: PlanDigest | None = None) -> str:
    value = digest or plan.digest()
    return (
        f"blackcell://plan/{quote(plan.plan_id)}"
        f"?revision={plan.revision}&digest={quote(str(value), safe=':')}"
    )


def item_marker(plan: PlanSpec, item: WorkItemSpec, digest: PlanDigest | None = None) -> str:
    value = digest or plan.digest()
    return (
        f"blackcell://item/{quote(plan.plan_id)}/{quote(item.key)}"
        f"?digest={quote(str(value), safe=':')}"
    )
