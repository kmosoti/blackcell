from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime

from blackcell.evaluation.types import BenchmarkScenario, ContextCondition, EvidenceFixture, Trial
from blackcell.features.derive_signal_packet import SignalClaim, SignalConflict, SignalPacket
from blackcell.features.retrieve_evidence import EvidenceRetriever, RetrieveEvidence
from blackcell.models import JsonObject


class ExperimentContextBudgetError(ValueError):
    """A treatment exceeded the experiment's shared model-context ceiling."""


def build_context(
    scenario: BenchmarkScenario,
    condition: ContextCondition,
    *,
    latest_n: int = 3,
) -> JsonObject:
    """Render one of OperatorBench's controlled context interventions."""

    if latest_n <= 0:
        raise ValueError("latest_n must be positive")
    ordered = sorted(scenario.observations, key=lambda item: (item.sequence, item.evidence_id))
    task = _task_context(scenario)

    if condition is ContextCondition.RAW_CHRONOLOGICAL:
        return {
            "condition": condition.value,
            "task": task,
            "observations": [_render_observation(item) for item in ordered],
        }
    if condition is ContextCondition.LATEST_N:
        selected = ordered[-latest_n:]
        return {
            "condition": condition.value,
            "task": task,
            "latest_n": latest_n,
            "observations": [_render_observation(item) for item in selected],
        }
    if condition is ContextCondition.STRUCTURED:
        return {
            "condition": condition.value,
            "task": task,
            "state": dict(scenario.structured_context),
        }
    raise ValueError(f"unknown context condition: {condition!r}")


def build_trial_context(
    scenario: BenchmarkScenario,
    trial: Trial,
    *,
    retrievers: Mapping[ContextCondition, EvidenceRetriever] | None = None,
) -> JsonObject:
    if scenario.scenario_id != trial.scenario_id:
        raise ValueError("trial does not belong to scenario")
    if trial.condition in {
        ContextCondition.TERM_RETRIEVAL,
        ContextCondition.FTS5_RETRIEVAL,
    }:
        if retrievers is None or trial.condition not in retrievers:
            raise ValueError(f"no retriever configured for {trial.condition.value}")
        context = _build_retrieval_context(
            scenario,
            trial.condition,
            retrievers[trial.condition],
            max_results=trial.retrieval_result_limit,
        )
    else:
        context = build_context(scenario, trial.condition, latest_n=trial.latest_n)
    characters = serialized_chars(context)
    if characters > trial.context_character_budget:
        raise ExperimentContextBudgetError(
            f"{trial.condition.value} context uses {characters} characters, "
            f"exceeding the shared {trial.context_character_budget}-character budget"
        )
    return context


def serialized_chars(context: JsonObject) -> int:
    return len(json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def _render_observation(item: EvidenceFixture) -> JsonObject:
    return {
        "evidence_id": item.evidence_id,
        "sequence": item.sequence,
        "kind": item.kind,
        "content": item.content,
        "observed_at": item.observed_at,
        "stale": item.stale,
        "supersedes": list(item.supersedes),
        "attributes": dict(item.attributes),
    }


def _build_retrieval_context(
    scenario: BenchmarkScenario,
    condition: ContextCondition,
    retriever: EvidenceRetriever,
    *,
    max_results: int,
) -> JsonObject:
    packet = _signal_packet(scenario)
    observations = {item.evidence_id: item for item in scenario.observations}
    selection = retriever.handle(
        RetrieveEvidence(scenario.task.instruction, max_results=max_results),
        packet,
    )
    return {
        "condition": condition.value,
        "task": _task_context(scenario),
        "evidence": [
            {
                "evidence_id": candidate.source_event_id,
                "claim_id": candidate.claim_id,
                "kind": candidate.predicate,
                "content": candidate.value,
                "observed_at": observations[candidate.source_event_id].observed_at,
                "stale": candidate.stale,
                "supersedes": list(observations[candidate.source_event_id].supersedes),
                "attributes": dict(observations[candidate.source_event_id].attributes),
                "relevance_score": candidate.score,
                "selection_reasons": list(candidate.reasons),
                "conflicted": candidate.conflicted,
            }
            for candidate in selection.candidates
        ],
        "retrieval": {
            "source_packet_id": selection.source_packet_id,
            "selection_id": selection.selection_id,
            "selected_count": len(selection.candidates),
            "omitted_count": selection.omitted_count,
            "result_limit": max_results,
        },
    }


def _signal_packet(scenario: BenchmarkScenario) -> SignalPacket:
    observations = tuple(
        sorted(scenario.observations, key=lambda item: (item.sequence, item.evidence_id))
    )
    maximum_sequence = max((item.sequence for item in observations), default=0)
    claims = tuple(
        SignalClaim(
            claim_id=f"claim:{item.evidence_id}",
            subject=f"task:{scenario.task.task_id}",
            predicate=item.kind,
            value=item.content,
            confidence=1.0,
            effective_at=_observed_at(item),
            freshness_seconds=maximum_sequence - item.sequence,
            stale=item.stale,
            source_event_id=item.evidence_id,
            domain="operator-bench",
            stream_id=f"scenario:{scenario.scenario_id}",
            stream_sequence=item.sequence,
            global_position=item.sequence,
        )
        for item in observations
    )
    generated_at = max(
        (_observed_at(item) for item in observations),
        default=datetime(1970, 1, 1, tzinfo=UTC),
    )
    return SignalPacket(
        purpose="operator-bench-context-retrieval",
        state_domain="operator-bench",
        state_stream_id=f"scenario:{scenario.scenario_id}",
        generated_at=generated_at,
        state_global_position=maximum_sequence,
        state_stream_position=maximum_sequence,
        claims=claims,
        conflicts=_signal_conflicts(scenario, claims),
        provenance_event_ids=tuple(sorted(item.evidence_id for item in observations)),
        mean_confidence=1.0 if claims else 0.0,
        stale_claim_count=sum(item.stale for item in observations),
    )


def _signal_conflicts(
    scenario: BenchmarkScenario,
    claims: tuple[SignalClaim, ...],
) -> tuple[SignalConflict, ...]:
    if "conflict" not in scenario.tags:
        return ()
    claims_by_predicate: dict[str, list[SignalClaim]] = {}
    for claim in claims:
        claims_by_predicate.setdefault(claim.predicate, []).append(claim)
    return tuple(
        SignalConflict(
            subject=group[0].subject,
            predicate=predicate,
            source_event_ids=tuple(claim.source_event_id for claim in group),
            claim_ids=tuple(claim.claim_id for claim in group),
            values=tuple(claim.value for claim in group),
        )
        for predicate, group in sorted(claims_by_predicate.items())
        if len(group) > 1
    )


def _observed_at(item: EvidenceFixture) -> datetime:
    return datetime.fromisoformat(item.observed_at.replace("Z", "+00:00"))


def _task_context(scenario: BenchmarkScenario) -> JsonObject:
    return {
        "task_id": scenario.task.task_id,
        "instruction": scenario.task.instruction,
        "available_actions": list(scenario.task.safe_actions + scenario.task.forbidden_actions),
    }
