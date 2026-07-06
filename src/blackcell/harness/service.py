from blackcell.harness.models import AgentSpec, HarnessPlan, PlanStep, RunTrace, TraceEvent
from blackcell.runtime import list_runtime_adapters
from blackcell.world.models import WorldSnapshot


def plan_harness(snapshot: WorldSnapshot) -> HarnessPlan:
    available_runtime_names = tuple(
        adapter.name for adapter in list_runtime_adapters() if adapter.available
    )
    return HarnessPlan(
        goal="Build and iterate on a world-model-driven software harness.",
        agents=(
            AgentSpec(
                key="observer",
                role="research-and-observation",
                objective="Turn repository state into typed observations and facts.",
                sandbox="read-only",
            ),
            AgentSpec(
                key="planner",
                role="constraint-aware-planning",
                objective=(
                    "Translate facts, beliefs, and expectations into executable work packets."
                ),
                sandbox="read-only",
            ),
        ),
        steps=(
            PlanStep("step:observe", "Observe the repo and emit typed facts.", ("world.observe",)),
            PlanStep(
                "step:validate",
                "Validate NeSy rules against the observed fact surface.",
                ("nesy.validate",),
            ),
            PlanStep(
                "step:dispatch",
                "Dispatch through a runtime adapter while preserving traceability.",
                available_runtime_names,
            ),
        ),
    )


def run_harness(plan: HarnessPlan, *, runtime: str) -> RunTrace:
    if runtime != "dry-run":
        raise ValueError("only the dry-run runtime is implemented in the first overhaul slice")

    events = tuple(
        TraceEvent(index=index, kind="plan-step", message=step.summary)
        for index, step in enumerate(plan.steps, start=1)
    )
    return RunTrace(runtime=runtime, status="simulated", events=events)
