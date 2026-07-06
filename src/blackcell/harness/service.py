from pathlib import Path
from typing import Literal

from blackcell.harness.models import (
    AgentSpec,
    HarnessPlan,
    LatentTraceActionStats,
    LatentTraceSummary,
    PlanStep,
    RunTrace,
    TraceEvent,
)
from blackcell.latent import (
    load_transitions,
    record_simulation,
    simulate_transition,
    summarize_prediction_stats,
)
from blackcell.latent.ids import stable_digest
from blackcell.ledger import make_event, record_run
from blackcell.ledger.models import Payload
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


def run_harness(
    plan: HarnessPlan,
    *,
    runtime: str,
    snapshot: WorldSnapshot,
    latent_db: Path | None = None,
    latent_mode: Literal["off", "summary", "record", "stats"] = "summary",
    ledger_db: Path | None = None,
) -> RunTrace:
    if runtime != "dry-run":
        raise ValueError("only the dry-run runtime is implemented in the first overhaul slice")

    events = tuple(
        TraceEvent(index=index, kind="plan-step", message=step.summary)
        for index, step in enumerate(plan.steps, start=1)
    )
    if latent_mode == "off":
        return _attach_ledger(
            RunTrace(runtime=runtime, status="simulated", events=events),
            plan=plan,
            ledger_db=ledger_db,
        )

    should_record = latent_mode in {"record", "stats"} and latent_db is not None
    should_show_stats = latent_mode == "stats" and latent_db is not None
    confidence_labels = None
    transition_memory = ()
    stats = None
    if latent_db is not None and latent_mode in {"record", "stats"}:
        transition_memory = load_transitions(latent_db)
        stats = summarize_prediction_stats(latent_db)
        confidence_labels = {
            action.action_id: action.confidence_label for action in stats.action_stats
        }
    latent_simulation = simulate_transition(
        snapshot,
        transition_memory=transition_memory,
        confidence_labels_by_action=confidence_labels,
    )
    record_result = record_simulation(latent_simulation, path=latent_db) if should_record else None
    latent_summary = LatentTraceSummary(
        state_id=latent_simulation.state.state_id,
        action_id=latent_simulation.prediction.action.action_id,
        prediction_id=latent_simulation.prediction.prediction_id,
        confidence_label=latent_simulation.prediction.confidence_label,
        sample_count=latent_simulation.prediction.sample_count,
        error_id=latent_simulation.error.error_id,
        transition_id=latent_simulation.transition.transition_id,
        sample_id=latent_simulation.self_supervision_sample.sample_id,
        recorded_path=str(record_result.path) if record_result is not None else None,
    )
    events += (
        TraceEvent(
            index=len(events) + 1,
            kind="latent-prediction",
            message=(
                "V0 latent loop encoded z_t, predicted z_hat_next, compared z_next, "
                f"and produced a {latent_summary.confidence_label} prediction."
            ),
        ),
    )
    latent_stats = ()
    if should_show_stats and latent_db is not None:
        stats = summarize_prediction_stats(latent_db)
    if should_show_stats and stats is not None:
        latent_stats = tuple(
            LatentTraceActionStats(
                action_id=action.action_id,
                sample_count=action.sample_count,
                mean_semantic_distance=action.mean_semantic_distance,
                surprise_count=action.surprise_count,
                confidence_label=action.confidence_label,
            )
            for action in stats.action_stats
        )
    return _attach_ledger(
        RunTrace(
            runtime=runtime,
            status="simulated",
            events=events,
            latent=latent_summary,
            latent_stats=latent_stats,
        ),
        plan=plan,
        ledger_db=ledger_db,
    )


def _attach_ledger(
    trace: RunTrace,
    *,
    plan: HarnessPlan,
    ledger_db: Path | None,
) -> RunTrace:
    if ledger_db is None:
        return trace
    payload: Payload = {
        "goal": plan.goal,
        "runtime": trace.runtime,
        "event_count": len(trace.events),
        "latent": trace.latent is not None,
    }
    created_at = "deterministic:harness-dry-run:v1"
    run_id = stable_digest(
        "ledger-run",
        {
            "kind": "harness-run",
            "status": trace.status,
            "created_at": created_at,
            "payload": payload,
        },
    )
    events = tuple(
        make_event(
            run_id=run_id,
            sequence=event.index,
            kind=event.kind,
            source="harness",
            message=event.message,
            payload={"trace_index": event.index},
        )
        for event in trace.events
    )
    result = record_run(
        path=ledger_db,
        run_id=run_id,
        kind="harness-run",
        status=trace.status,
        created_at=created_at,
        payload=payload,
        events=events,
    )
    return RunTrace(
        runtime=trace.runtime,
        status=trace.status,
        events=trace.events,
        latent=trace.latent,
        latent_stats=trace.latent_stats,
        ledger_path=str(result.path),
        ledger_run_id=result.run_id,
    )
