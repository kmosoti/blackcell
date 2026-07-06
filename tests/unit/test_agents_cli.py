import json
from pathlib import Path

import pytest

from blackcell.cli.app import app
from tests.cli_runner import CycloptsCliRunner

runner = CycloptsCliRunner()


def test_agents_list_outputs_json() -> None:
    result = runner.invoke(app, ["agents", "list"], catch_exceptions=False)

    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["agents"][0]["key"] == "blackcell-astrophage"


def test_agents_render_outputs_opencode_artifacts() -> None:
    result = runner.invoke(
        app,
        ["agents", "render", "--target", "opencode", "--scope", "project"],
        catch_exceptions=False,
    )

    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["artifacts"][0]["path"] == ".opencode/agents/blackcell-astrophage.md"
    assert payload["artifacts"][0]["content"].startswith("---\n")


def test_agents_install_apply_and_check_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    install_result = runner.invoke(
        app,
        ["agents", "install", "--target", "opencode", "--scope", "project", "--apply"],
        catch_exceptions=False,
    )
    drift_result = runner.invoke(
        app,
        ["agents", "check-drift", "--target", "opencode", "--scope", "project"],
        catch_exceptions=False,
    )

    install_payload = json.loads(install_result.stdout)
    drift_payload = json.loads(drift_result.stdout)

    assert install_result.exit_code == 0
    assert install_payload["dry_run"] is False
    assert (tmp_path / ".opencode" / "agents" / "blackcell-spore.md").exists()
    assert drift_result.exit_code == 0
    assert drift_payload["drift"] is False


def test_agents_check_drift_exits_nonzero_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["agents", "check-drift", "--target", "opencode", "--scope", "project"],
        catch_exceptions=False,
    )

    payload = json.loads(result.stdout)

    assert result.exit_code == 1
    assert payload["drift"] is True


def test_agents_install_rejects_unknown_target() -> None:
    result = runner.invoke(
        app,
        ["agents", "install", "--target", "other"],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "agent target must be opencode" in result.stderr


def _write_repo(path: Path) -> None:
    (path / ".git").mkdir()
