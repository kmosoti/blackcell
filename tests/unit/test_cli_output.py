import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from blackcell.cli.app import app
from blackcell.config import BlackcellConfig, ProjectRef, RepositoryRef, write_config

runner = CliRunner()


def test_config_show_defaults_to_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_test_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["config", "show"], catch_exceptions=False)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["repository"]["owner"] == "kmosoti"
    assert payload["project"]["id"] == "PVT_123"


def test_config_show_renders_rich_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_test_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["--rich", "config", "show"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "BlackCell Config" in result.stdout
    assert "PVT_123" in result.stdout


def test_provider_list_jsonl_outputs_one_record_per_line() -> None:
    result = runner.invoke(app, ["--jsonl", "providers", "list"], catch_exceptions=False)

    assert result.exit_code == 0
    assert [json.loads(line) for line in result.stdout.splitlines()] == [{"name": "github"}]


def test_project_items_missing_token_reports_json_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_test_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    result = runner.invoke(app, ["project", "items"], catch_exceptions=False)

    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload == {
        "error": {"message": "GITHUB_TOKEN or GH_TOKEN is required for GitHub API calls"}
    }


def _write_test_config(path: Path) -> None:
    (path / ".git").mkdir()
    config = BlackcellConfig(
        repository=RepositoryRef(owner="kmosoti", name="blackcell", node_id="R_123"),
        project=ProjectRef(
            id="PVT_123",
            number=7,
            title="BlackCell",
            url="https://github.com/users/kmosoti/projects/7",
        ),
    )
    write_config(config, start=path)
