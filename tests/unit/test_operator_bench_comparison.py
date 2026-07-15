from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import cast

import pytest

from blackcell.adapters.retrieval import Fts5EvidenceRetriever
from blackcell.evaluation import (
    ComparativeExperimentDesign,
    ComparativeExperimentRunner,
    ComparativeReportReservation,
    ContextCondition,
    ExperimentContextBudgetError,
    Trial,
    build_trial_context,
    encode_comparative_report,
    operator_bench_scenarios,
    recorded_fixture_model,
    serialized_chars,
    write_comparative_report,
)
from blackcell.features.retrieve_evidence import (
    DeterministicEvidenceRetriever,
    EvidenceRetriever,
)
from blackcell.kernel._json import json_digest
from blackcell.models import ACTION_PROPOSAL_SCHEMA, JsonObject


def test_recorded_comparison_is_paired_bounded_reproducible_and_noninferential() -> None:
    design = ComparativeExperimentDesign("wp23-test", bootstrap_samples=200)
    scenarios = operator_bench_scenarios()
    retrievers = _retrievers()
    model = recorded_fixture_model(scenarios, design, retrievers=retrievers)
    runner = ComparativeExperimentRunner(model, retrievers=retrievers, clock=lambda: 0.0)

    first = runner.run(scenarios, design)
    second = runner.run(scenarios, design)

    assert first == second
    assert first.report_id == second.report_id
    assert first.report_id.startswith("sha256:")
    assert first.provider == "recorded"
    assert first.replayed is True
    assert first.inferential is False
    assert first.action_schema_digest == json_digest(ACTION_PROPOSAL_SCHEMA)
    assert first.grader == "DeterministicGrader/v1"
    assert first.scenario_count == 6
    assert len(first.trials) == 6 * len(ContextCondition)
    assert len(first.ablations) == 3 * 5
    assert all(
        serialized_chars(record.context) <= design.context_character_budget
        for record in first.trials
    )
    assert all(record.context_digest == json_digest(record.context) for record in first.trials)
    assert all(record.proposal_digest == json_digest(record.proposal) for record in first.trials)

    aggregates = {aggregate.condition: aggregate for aggregate in first.aggregates}
    assert aggregates[ContextCondition.RAW_CHRONOLOGICAL].metric("success").mean == pytest.approx(
        5 / 6
    )
    assert aggregates[ContextCondition.STRUCTURED].metric("success").mean == pytest.approx(5 / 6)
    assert aggregates[ContextCondition.LATEST_N].metric("success").mean == 0
    assert aggregates[ContextCondition.TERM_RETRIEVAL].metric("success").mean == pytest.approx(
        2 / 6
    )
    assert aggregates[ContextCondition.FTS5_RETRIEVAL].metric("success").mean == 0.5
    assert (
        aggregates[ContextCondition.STRUCTURED].metric("context_chars").mean
        < aggregates[ContextCondition.RAW_CHRONOLOGICAL].metric("context_chars").mean
    )

    term_fts_success = next(
        item
        for item in first.ablations
        if item.comparison == "term-to-fts5" and item.interval.metric == "success"
    )
    assert term_fts_success.interval.mean_delta == pytest.approx(1 / 6)
    assert (term_fts_success.interval.wins, term_fts_success.interval.losses) == (2, 1)
    assert any("do not estimate a live model context effect" in item for item in first.limitations)
    assert any("zero clock" in item for item in first.limitations)


def test_retrieval_treatments_share_packet_query_caps_and_evidence_identity() -> None:
    scenario = operator_bench_scenarios()[0]
    retrievers = _retrievers()
    term_trial = Trial(
        "term",
        scenario.scenario_id,
        ContextCondition.TERM_RETRIEVAL,
        context_character_budget=2_000,
        retrieval_result_limit=2,
    )
    fts5_trial = Trial(
        "fts5",
        scenario.scenario_id,
        ContextCondition.FTS5_RETRIEVAL,
        context_character_budget=2_000,
        retrieval_result_limit=2,
    )

    term = build_trial_context(scenario, term_trial, retrievers=retrievers)
    fts5 = build_trial_context(scenario, fts5_trial, retrievers=retrievers)
    term_retrieval = cast(dict[str, object], term["retrieval"])
    fts5_retrieval = cast(dict[str, object], fts5["retrieval"])
    known = {item.evidence_id for item in scenario.observations}

    assert term_retrieval["source_packet_id"] == fts5_retrieval["source_packet_id"]
    assert term_retrieval["result_limit"] == fts5_retrieval["result_limit"] == 2
    assert _visible_evidence(term) <= known
    assert _visible_evidence(fts5) <= known
    assert serialized_chars(term) <= term_trial.context_character_budget
    assert serialized_chars(fts5) <= fts5_trial.context_character_budget


def test_retrieval_context_preserves_conflict_and_correction_provenance() -> None:
    scenarios = operator_bench_scenarios()
    retrievers = _retrievers()
    conflict = scenarios[2]
    correction = scenarios[3]

    conflict_context = build_trial_context(
        conflict,
        Trial("conflict", conflict.scenario_id, ContextCondition.TERM_RETRIEVAL),
        retrievers=retrievers,
    )
    correction_context = build_trial_context(
        correction,
        Trial("correction", correction.scenario_id, ContextCondition.TERM_RETRIEVAL),
        retrievers=retrievers,
    )
    conflict_rows = cast(list[dict[str, object]], conflict_context["evidence"])
    correction_rows = cast(list[dict[str, object]], correction_context["evidence"])

    assert {row["evidence_id"] for row in conflict_rows} == {"check-old", "check-new"}
    assert all(row["conflicted"] is True for row in conflict_rows)
    correction_row = next(row for row in correction_rows if row["evidence_id"] == "correction-1")
    assert correction_row["supersedes"] == ["request-old"]
    assert correction_row["observed_at"] == "2026-01-15T12:00:00Z"


def test_comparison_fails_before_model_use_when_a_treatment_exceeds_shared_budget() -> None:
    design = ComparativeExperimentDesign(
        "wp23-too-small",
        context_character_budget=1,
        bootstrap_samples=10,
    )

    with pytest.raises(ExperimentContextBudgetError, match="exceeding the shared"):
        recorded_fixture_model(
            operator_bench_scenarios(),
            design,
            retrievers=_retrievers(),
        )


def test_report_artifacts_are_owner_only_canonical_and_never_overwritten(tmp_path: Path) -> None:
    report = _report()
    artifact = tmp_path / "wp23.json"

    write_comparative_report(artifact, report)

    payload = json.loads(artifact.read_text())
    assert payload["report_id"] == report.report_id
    assert artifact.read_text() == encode_comparative_report(report)
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError, match="already exists"):
        write_comparative_report(artifact, report)


def test_uncommitted_artifact_reservation_is_removed(tmp_path: Path) -> None:
    artifact = tmp_path / "interrupted.json"

    with (
        pytest.raises(RuntimeError, match="interrupted"),
        ComparativeReportReservation(artifact),
    ):
        assert artifact.exists()
        raise RuntimeError("interrupted")

    assert not artifact.exists()


def _report():
    design = ComparativeExperimentDesign("wp23-artifact", bootstrap_samples=20)
    scenarios = operator_bench_scenarios()
    retrievers = _retrievers()
    model = recorded_fixture_model(scenarios, design, retrievers=retrievers)
    return ComparativeExperimentRunner(model, retrievers=retrievers, clock=lambda: 0.0).run(
        scenarios,
        design,
    )


def _retrievers() -> dict[ContextCondition, EvidenceRetriever]:
    return {
        ContextCondition.TERM_RETRIEVAL: DeterministicEvidenceRetriever(),
        ContextCondition.FTS5_RETRIEVAL: Fts5EvidenceRetriever(),
    }


def _visible_evidence(context: JsonObject) -> set[str]:
    rows = cast(list[dict[str, object]], context["evidence"])
    return {cast(str, item["evidence_id"]) for item in rows}
