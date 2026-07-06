import json
from pathlib import Path

import pytest

from blackcell.cli.app import app
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
    assert json.loads(run_result.stdout)["status"] == "simulated"


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
