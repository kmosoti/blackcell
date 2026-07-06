import json
from pathlib import Path

import pytest

from blackcell.cli.app import app
from blackcell.ledger import list_events, list_runs
from tests.cli_runner import CycloptsCliRunner

runner = CycloptsCliRunner()


def test_world_observe_outputs_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["world", "observe"], catch_exceptions=False)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["repo_root"] == str(tmp_path)
    assert payload["beliefs"][0]["key"].startswith("belief:")


def test_harness_plan_and_run_are_json_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    plan_result = runner.invoke(app, ["harness", "plan"], catch_exceptions=False)
    run_result = runner.invoke(
        app,
        ["harness", "run", "--runtime", "dry-run"],
        catch_exceptions=False,
    )

    assert plan_result.exit_code == 0
    assert json.loads(plan_result.stdout)["goal"].startswith("Build and iterate")
    assert run_result.exit_code == 0
    run_payload = json.loads(run_result.stdout)
    assert run_payload["status"] == "simulated"
    assert run_payload["events"][-1]["kind"] == "latent-prediction"
    assert run_payload["latent"]["confidence_label"] == "cold"
    assert run_payload["latent"]["recorded_path"] is None


def test_harness_run_can_record_latent_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "latent.sqlite3"

    first = runner.invoke(
        app,
        ["harness", "run", "--runtime", "dry-run", "--latent-db", str(db)],
        catch_exceptions=False,
    )
    second = runner.invoke(
        app,
        ["harness", "run", "--runtime", "dry-run", "--latent-db", str(db)],
        catch_exceptions=False,
    )

    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first.exit_code == 0
    assert first_payload["latent"]["recorded_path"] == str(db)
    assert first_payload["latent"]["sample_count"] == 0
    assert second.exit_code == 0
    assert second_payload["latent"]["recorded_path"] == str(db)
    assert second_payload["latent"]["sample_count"] == 1
    assert second_payload["latent"]["confidence_label"] == "warming"


def test_harness_run_latent_modes_control_output_and_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "latent.sqlite3"

    off = runner.invoke(
        app,
        ["harness", "run", "--runtime", "dry-run", "--latent", "off"],
        catch_exceptions=False,
    )
    summary = runner.invoke(
        app,
        ["harness", "run", "--runtime", "dry-run", "--latent", "summary"],
        catch_exceptions=False,
    )
    stats = runner.invoke(
        app,
        [
            "harness",
            "run",
            "--runtime",
            "dry-run",
            "--latent",
            "stats",
            "--latent-db",
            str(db),
        ],
        catch_exceptions=False,
    )

    off_payload = json.loads(off.stdout)
    summary_payload = json.loads(summary.stdout)
    stats_payload = json.loads(stats.stdout)
    assert off_payload["latent"] is None
    assert off_payload["latent_stats"] == []
    assert summary_payload["latent"]["recorded_path"] is None
    assert stats_payload["latent"]["recorded_path"] == str(db)
    assert stats_payload["latent_stats"][0]["sample_count"] == 1
    assert stats_payload["latent_stats"][0]["confidence_label"] == "warming"
    assert db.exists()


def test_harness_run_can_fold_latent_stats_into_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "latent.sqlite3"
    runner.invoke(
        app,
        ["harness", "run", "--runtime", "dry-run", "--latent-db", str(db)],
        catch_exceptions=False,
    )

    result = runner.invoke(
        app,
        [
            "harness",
            "run",
            "--runtime",
            "dry-run",
            "--latent-db",
            str(db),
            "--show-stats",
        ],
        catch_exceptions=False,
    )

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["latent"]["confidence_label"] == "warming"
    assert payload["latent_stats"][0]["action_id"] == "action:observe-validate"
    assert payload["latent_stats"][0]["sample_count"] == 2
    assert payload["latent_stats"][0]["confidence_label"] == "warming"


def test_harness_run_can_record_generic_run_event_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "ledger.sqlite3"

    first = runner.invoke(
        app,
        ["harness", "run", "--runtime", "dry-run", "--ledger-db", str(db)],
        catch_exceptions=False,
    )
    second = runner.invoke(
        app,
        ["harness", "run", "--runtime", "dry-run", "--ledger-db", str(db)],
        catch_exceptions=False,
    )

    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first.exit_code == 0
    assert first_payload["ledger_path"] == str(db)
    assert first_payload["ledger_run_id"] is not None
    assert second.exit_code == 0
    assert second_payload["ledger_run_id"] == first_payload["ledger_run_id"]
    assert len(list_runs(path=db)) == 1
    assert len(list_events(path=db, run_id=first_payload["ledger_run_id"])) == len(
        first_payload["events"]
    )


def test_harness_run_latent_off_can_record_generic_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "ledger.sqlite3"

    result = runner.invoke(
        app,
        [
            "harness",
            "run",
            "--runtime",
            "dry-run",
            "--latent",
            "off",
            "--ledger-db",
            str(db),
        ],
        catch_exceptions=False,
    )

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["latent"] is None
    assert payload["ledger_path"] == str(db)
    assert len(list_runs(path=db)) == 1
    assert len(list_events(path=db, run_id=payload["ledger_run_id"])) == 3


def test_adapters_and_doctor_report_available_runtimes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    adapters_result = runner.invoke(app, ["adapters", "list"], catch_exceptions=False)
    doctor_result = runner.invoke(app, ["doctor"], catch_exceptions=False)

    assert adapters_result.exit_code == 0
    assert json.loads(adapters_result.stdout)["adapters"][0]["name"] == "dry-run"
    assert doctor_result.exit_code == 0
    assert json.loads(doctor_result.stdout)["adapter_count"] >= 1


def _write_repo(path: Path) -> None:
    (path / ".git").mkdir()
    (path / "README.md").write_text("# Test\n", encoding="utf-8")
    (path / "docs").mkdir()
    (path / "src").mkdir()
    (path / "tests").mkdir()
    (path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
