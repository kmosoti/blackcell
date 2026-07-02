import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from blackcell.auth import AuthSession, DeviceCode, DeviceLoginResult
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
    monkeypatch.setenv("BLACKCELL_AUTH_FILE", str(tmp_path / "missing-auth.json"))

    result = runner.invoke(app, ["project", "items"], catch_exceptions=False)

    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload == {
        "error": {
            "message": (
                "GITHUB_TOKEN, GH_TOKEN, or `blackcell auth login` is required "
                "for GitHub API calls"
            )
        }
    }


def test_auth_status_missing_session_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLACKCELL_AUTH_FILE", str(tmp_path / "auth.json"))

    result = runner.invoke(app, ["auth", "status"], catch_exceptions=False)

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["authenticated"] is False


def test_auth_logout_reports_deleted_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("BLACKCELL_AUTH_FILE", str(auth_file))

    result = runner.invoke(app, ["auth", "logout"], catch_exceptions=False)

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"deleted": True}
    assert not auth_file.exists()


def test_auth_login_uses_device_flow_without_printing_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AuthSession(
        provider="github",
        host="github.com",
        access_token="secret-token",
        scopes=("repo",),
        created_at="2026-07-02T00:00:00Z",
    )

    def fake_login(**kwargs: object) -> DeviceLoginResult:
        assert kwargs["client_id"] == "client"
        assert kwargs["scopes"] == ("repo",)
        assert kwargs["min_ttl_seconds"] == 5 * 60 * 60
        return DeviceLoginResult(session=session, path=tmp_path / "auth.json")

    monkeypatch.setattr("blackcell.cli.app.login_with_device_flow", fake_login)

    result = runner.invoke(
        app,
        ["auth", "login", "--client-id", "client", "--scope", "repo"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "secret-token" not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["authenticated"] is True
    assert payload["path"] == str(tmp_path / "auth.json")


def test_auth_login_supports_browser_and_qr_prompt_preferences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_urls: list[str] = []
    session = AuthSession(
        provider="github",
        host="github.com",
        access_token="secret-token",
        scopes=("repo",),
        created_at="2026-07-02T00:00:00Z",
    )

    def fake_open(url: str) -> bool:
        opened_urls.append(url)
        return True

    def fake_qr(value: str) -> str:
        return f"QR:{value}"

    def fake_login(**kwargs: object) -> DeviceLoginResult:
        prompt = cast("Callable[[DeviceCode], None]", kwargs["prompt"])
        prompt(
            DeviceCode(
                device_code="device",
                user_code="ABCD-EFGH",
                verification_uri="https://github.com/login/device",
                expires_in=900,
                interval=5,
                verification_uri_complete="https://github.com/login/device?user_code=ABCD-EFGH",
            )
        )
        return DeviceLoginResult(session=session, path=tmp_path / "auth.json")

    monkeypatch.setattr("blackcell.cli.app.webbrowser.open", fake_open)
    monkeypatch.setattr("blackcell.cli.app.render_terminal_qr", fake_qr)
    monkeypatch.setattr("blackcell.cli.app.login_with_device_flow", fake_login)

    result = runner.invoke(
        app,
        ["auth", "login", "--client-id", "client", "--scope", "repo", "--browser", "--qr"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert opened_urls == ["https://github.com/login/device?user_code=ABCD-EFGH"]
    assert "QR:https://github.com/login/device?user_code=ABCD-EFGH" in result.stderr
    assert "https://github.com/login/device" in result.stderr
    assert "ABCD-EFGH" in result.stderr
    assert "secret-token" not in result.stdout
    assert json.loads(result.stdout)["path"] == str(tmp_path / "auth.json")


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
