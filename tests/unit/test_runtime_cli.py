import json
from pathlib import Path

import pytest

from blackcell.cli.app import app
from blackcell.kernel import EventEnvelope, EventStore
from tests.cli_runner import CycloptsCliRunner

runner = CycloptsCliRunner()


def test_events_list_reads_kernel_ledger_in_global_order(tmp_path: Path) -> None:
    database = tmp_path / "kernel.sqlite3"
    store = EventStore(database)
    stored = store.append(
        EventEnvelope.create(
            stream_id="repository:test",
            stream_sequence=1,
            event_type="repository.observed",
            actor="test",
            source="fixture",
            payload={"present": True},
        ),
        expected_sequence=0,
    )

    result = runner.invoke(
        app,
        ["events", "list", "--db", str(database)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["events"][0]["event_id"] == stored.event_id
    assert payload["events"][0]["global_position"] == 1


def test_cli_version_uses_the_package_version() -> None:
    result = runner.invoke(app, ["--version"], catch_exceptions=False)

    assert result.exit_code == 0
    assert result.stdout.strip() == "0.2.0"


def test_bench_list_exposes_versioned_synthetic_scenarios() -> None:
    result = runner.invoke(app, ["bench", "list"], catch_exceptions=False)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload["scenario_digest"]) == 64
    assert len(payload["scenarios"]) == 6
    assert payload["scenarios"][0]["scenario_id"] == "dependencies-before-change"


def test_bench_run_validates_each_fixture_once() -> None:
    result = runner.invoke(
        app,
        ["bench", "run", "--condition", "structured"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["mode"] == "fixture-contract-pilot"
    assert payload["inferential"] is False
    assert payload["condition"] == "structured"
    assert payload["trial_count"] == 6
    assert len(payload["scores"]) == 6
    assert payload["aggregates"][0]["condition"] == "structured"


def test_bench_run_rejects_pseudoreplicated_trial_count() -> None:
    result = runner.invoke(app, ["bench", "run", "--trials", "3"])

    assert result.exit_code == 2
    assert "--trials must be 1" in result.stderr


def test_bench_compare_runs_the_credential_free_recorded_contract() -> None:
    result = runner.invoke(
        app,
        ["bench", "compare", "--model", "recorded", "--bootstrap-samples", "100"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "operator-bench-comparison/v1"
    assert payload["provider"] == "recorded"
    assert payload["replayed"] is True
    assert payload["inferential"] is False
    assert payload["scenario_count"] == 6
    assert len(payload["trials"]) == 30
    assert len(payload["ablations"]) == 15


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (
            ["--model", "codex", "--replicates", "3", "--artifact", "report.json"],
            "--codex-model is required",
        ),
        (
            ["--model", "codex", "--codex-model", "gpt-test", "--artifact", "report.json"],
            "--replicates must be at least 3",
        ),
        (
            ["--model", "codex", "--codex-model", "gpt-test", "--replicates", "3"],
            "--artifact is required",
        ),
        (["--model", "recorded", "--codex-model", "gpt-test"], "only valid"),
    ),
)
def test_bench_compare_rejects_unrecorded_or_unmatched_live_designs(
    arguments: list[str],
    message: str,
) -> None:
    result = runner.invoke(app, ["bench", "compare", *arguments])

    assert result.exit_code == 2
    assert message in result.stderr


def test_bench_compare_reserves_live_artifact_before_provider_use(tmp_path: Path) -> None:
    artifact = tmp_path / "existing.json"
    artifact.write_text("preserve", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "bench",
            "compare",
            "--model",
            "codex",
            "--codex-model",
            "gpt-test",
            "--replicates",
            "3",
            "--artifact",
            str(artifact),
        ],
    )

    assert result.exit_code == 1
    assert "already exists" in result.stderr
    assert artifact.read_text() == "preserve"


def test_bench_predict_runs_and_retains_the_credential_free_matched_report(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "wp24.json"
    result = runner.invoke(
        app,
        [
            "bench",
            "predict",
            "--repetitions",
            "2",
            "--artifact",
            str(artifact),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    retained = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload == retained
    assert payload["schema_version"] == "prediction-bench-report/v1"
    assert payload["scenario_count"] == 8
    assert len(payload["trials"]) == 16
    assert payload["inferential"] is False
    assert {item["condition"] for item in payload["aggregates"]} == {
        "state-persistence",
        "declared-effects",
    }


def test_bench_predict_rejects_invalid_or_overwritten_measurement_designs(
    tmp_path: Path,
) -> None:
    invalid = runner.invoke(app, ["bench", "predict", "--repetitions", "0"])
    assert invalid.exit_code == 1
    assert "repetitions must be positive" in invalid.stderr

    artifact = tmp_path / "existing.json"
    artifact.write_text("preserve", encoding="utf-8")
    existing = runner.invoke(
        app,
        ["bench", "predict", "--repetitions", "1", "--artifact", str(artifact)],
    )
    assert existing.exit_code == 1
    assert "already exists" in existing.stderr
    assert artifact.read_text(encoding="utf-8") == "preserve"


def test_bench_runtime_requires_and_exclusively_reserves_live_probe_artifact(
    tmp_path: Path,
) -> None:
    missing = runner.invoke(app, ["bench", "runtime", "--include-podman"])
    assert missing.exit_code == 2
    assert "--artifact is required" in missing.stderr

    artifact = tmp_path / "existing.json"
    artifact.write_text("preserve", encoding="utf-8")
    existing = runner.invoke(
        app,
        [
            "bench",
            "runtime",
            "--include-podman",
            "--artifact",
            str(artifact),
        ],
    )
    assert existing.exit_code == 1
    assert "already exists" in existing.stderr
    assert artifact.read_text(encoding="utf-8") == "preserve"
