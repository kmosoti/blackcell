import json
import subprocess
from pathlib import Path

from blackcell.cli.app import app
from tests.cli_runner import CycloptsCliRunner

runner = CycloptsCliRunner()


def test_operator_cli_runs_inspects_and_replays_the_vertical_slice(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    database = repo / ".blackcell" / "kernel.sqlite3"

    run_result = runner.invoke(
        app,
        [
            "operator",
            "run",
            "--model",
            "recorded",
            "--repo",
            str(repo),
            "--db",
            str(database),
        ],
        catch_exceptions=False,
    )

    assert run_result.exit_code == 0
    run = json.loads(run_result.stdout)
    assert run["status"] == "completed"
    assert run["outcome"] == "executed"
    assert run["workflow_version"] == "daily-operator/v2"
    assert run["authorization_outcome"] == "allow"
    assert run["execution_status"] == "succeeded"
    assert run["evaluation_verdict"] == "pass"

    state_result = runner.invoke(
        app,
        ["operator", "state", "--repo", str(repo), "--db", str(database)],
        catch_exceptions=False,
    )
    context_result = runner.invoke(
        app,
        ["operator", "context", "--repo", str(repo), "--db", str(database)],
        catch_exceptions=False,
    )
    replay_result = runner.invoke(
        app,
        ["operator", "replay", "--repo", str(repo), "--db", str(database)],
        catch_exceptions=False,
    )

    state = json.loads(state_result.stdout)
    context = json.loads(context_result.stdout)
    replay = json.loads(replay_result.stdout)
    assert state_result.exit_code == context_result.exit_code == replay_result.exit_code == 0
    assert state["claims"]
    assert context["run_id"] == run["run_id"]
    assert context["frame_id"] == run["context_frame_id"]
    assert context["payload"]["objective"].startswith("Inspect current repository")
    assert replay["run_id"] == run["run_id"]
    assert replay["classification"] == "completed"
    assert replay["protocol_version"] == "daily-operator/v2"
    assert len(replay["events"]) == run["run_event_count"]
    assert all(artifact["verified"] for artifact in replay["artifacts"])


def test_operator_cli_requires_codex_model_before_creating_storage(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    database = repo / ".blackcell" / "kernel.sqlite3"

    result = runner.invoke(
        app,
        [
            "operator",
            "run",
            "--model",
            "codex",
            "--repo",
            str(repo),
            "--db",
            str(database),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "--codex-model is required" in json.loads(result.stderr)["error"]["message"]
    assert not database.exists()


def test_operator_cli_accepts_explicit_model_and_context_budgets(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    database = repo / ".blackcell" / "kernel.sqlite3"

    result = runner.invoke(
        app,
        [
            "operator",
            "run",
            "--repo",
            str(repo),
            "--db",
            str(database),
            "--token-budget",
            "1000",
            "--character-budget",
            "9000",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "completed"


def test_operator_cli_rejects_invalid_budget_before_creating_storage(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    database = repo / ".blackcell" / "kernel.sqlite3"

    result = runner.invoke(
        app,
        [
            "operator",
            "run",
            "--repo",
            str(repo),
            "--db",
            str(database),
            "--token-budget",
            "0",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "token budget must be between" in json.loads(result.stderr)["error"]["message"]
    assert not database.exists()


def test_operator_context_reports_missing_run_as_json_error(tmp_path: Path) -> None:
    repo = _repository(tmp_path)

    result = runner.invoke(
        app,
        ["operator", "context", "--repo", str(repo)],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert "kernel database does not exist" in payload["error"]["message"]


def test_operator_cli_rejects_a_missing_repository_without_creating_it(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    result = runner.invoke(
        app,
        ["operator", "run", "--repo", str(missing)],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "does not exist" in json.loads(result.stderr)["error"]["message"]
    assert not missing.exists()


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return repo
