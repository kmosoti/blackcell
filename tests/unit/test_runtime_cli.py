import json
from pathlib import Path

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
