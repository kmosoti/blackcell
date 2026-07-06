import json
from pathlib import Path

import pytest

from blackcell.cli.app import app
from tests.cli_runner import CycloptsCliRunner

runner = CycloptsCliRunner()


def test_agents_list_jsonl_outputs_one_record_per_line() -> None:
    result = runner.invoke(app, ["--jsonl", "agents", "list"], catch_exceptions=False)

    assert result.exit_code == 0
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert records
    assert records[0]["key"].startswith("blackcell-")


def test_world_facts_renders_rich_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["--rich", "world", "facts"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "Facts" in result.stdout


def _write_repo(path: Path) -> None:
    (path / ".git").mkdir()
    (path / "README.md").write_text("# Test\n", encoding="utf-8")
