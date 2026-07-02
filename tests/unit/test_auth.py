import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from blackcell.auth import (
    AuthError,
    AuthSession,
    DeviceCode,
    auth_cache_path,
    delete_auth_session,
    load_valid_access_token,
    poll_device_authorization,
    render_terminal_qr,
    request_device_code,
    save_auth_session,
)
from blackcell.config import BlackcellConfig, ProjectRef, RepositoryRef
from blackcell.providers.github import GitHubProjectsProvider


def test_auth_session_round_trips_with_restrictive_permissions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BLACKCELL_AUTH_FILE", str(tmp_path / "auth.json"))
    session = AuthSession(
        provider="github",
        host="github.com",
        access_token="token",
        scopes=("repo", "project"),
        created_at="2026-07-02T00:00:00Z",
    )

    path = save_auth_session(session)

    assert path == tmp_path / "auth.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_valid_access_token() == "token"
    assert delete_auth_session() is True
    assert delete_auth_session() is False


def test_expired_auth_session_is_not_used(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLACKCELL_AUTH_FILE", str(tmp_path / "auth.json"))
    save_auth_session(
        AuthSession(
            provider="github",
            host="github.com",
            access_token="expired",
            created_at="2026-07-02T00:00:00Z",
            expires_at="2000-01-01T00:00:00Z",
        )
    )

    assert load_valid_access_token() is None


def test_auth_cache_path_uses_xdg_config_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BLACKCELL_AUTH_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert auth_cache_path() == tmp_path / "blackcell" / "auth.json"


def test_request_device_code_preserves_complete_login_uri() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "device_code": "device",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
                "verification_uri_complete": (
                    "https://github.com/login/device?user_code=ABCD-EFGH"
                ),
                "expires_in": 900,
                "interval": 5,
            },
        )

    device_code = request_device_code(
        client_id="client",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert device_code.login_uri == "https://github.com/login/device?user_code=ABCD-EFGH"


def test_device_authorization_polls_until_token() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if len(requests) == 1:
            return httpx.Response(200, json={"error": "authorization_pending"})
        return httpx.Response(
            200,
            json={
                "access_token": "token",
                "token_type": "bearer",
                "scope": "repo,project",
                "expires_in": 8 * 60 * 60,
                "refresh_token": "refresh",
                "refresh_token_expires_in": 180 * 24 * 60 * 60,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    now = datetime(2026, 7, 2, tzinfo=UTC)

    session = poll_device_authorization(
        client_id="client",
        device_code=DeviceCode(
            device_code="device",
            user_code="ABCD-EFGH",
            verification_uri="https://github.com/login/device",
            expires_in=900,
            interval=5,
        ),
        client=client,
        sleep=lambda _: None,
        now=lambda: now,
    )

    assert session.access_token == "token"
    assert session.scopes == ("repo", "project")
    assert session.expires_at == (now + timedelta(hours=8)).isoformat().replace("+00:00", "Z")


def test_github_provider_uses_blackcell_auth_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BLACKCELL_AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    save_auth_session(
        AuthSession(
            provider="github",
            host="github.com",
            access_token="cached-token",
            created_at="2026-07-02T00:00:00Z",
        )
    )

    provider = GitHubProjectsProvider(
        BlackcellConfig(
            repository=RepositoryRef(owner="kmosoti", name="blackcell"),
            project=ProjectRef(id="PVT_123", title="BlackCell"),
        )
    )

    assert provider._token == "cached-token"


def test_saved_auth_payload_does_not_require_expiry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLACKCELL_AUTH_FILE", str(tmp_path / "auth.json"))
    save_auth_session(
        AuthSession(
            provider="github",
            host="github.com",
            access_token="token",
            created_at="2026-07-02T00:00:00Z",
        )
    )

    payload = json.loads((tmp_path / "auth.json").read_text(encoding="utf-8"))

    assert "expires_at" not in payload


def test_terminal_qr_renders_device_url() -> None:
    qr = render_terminal_qr("https://github.com/login/device")

    assert len(qr.splitlines()) >= 10
    assert any(character in qr for character in ("█", "▀", "▄"))


def test_terminal_qr_rejects_payloads_that_do_not_fit() -> None:
    with pytest.raises(AuthError, match="QR payload is too long"):
        render_terminal_qr("x" * 54)
