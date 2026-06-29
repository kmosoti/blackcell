"""Configuration validation stays typed and secret-free."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from blackcell.config.loader import find_config, load_config
from blackcell.config.model import BlackcellConfig, RuntimeSecrets
from blackcell.contracts.errors import ValidationFailure
from tests.conftest import config_data


def test_config_accepts_pinned_authority() -> None:
    config = BlackcellConfig.model_validate(config_data())

    assert config.linear.team_key == "BLCELL"
    assert config.linear.project_presentation.brand == "BlackCell"
    assert config.identity.planner_user_id == "1ed22c47-390f-41e6-b63d-497f58cccb3b"
    assert config.ledger.append_only is True


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), "0.2"),
        (("linear", "issue_sync_mode"), "one_way"),
        (("ledger", "append_only"), False),
        (("materialization", "projection_timeout_seconds"), 0),
    ],
)
def test_config_rejects_contract_drift(path: tuple[str, ...], value: object) -> None:
    data = config_data()
    target = data
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        BlackcellConfig.model_validate(data)


def test_config_rejects_embedded_secret() -> None:
    data = config_data()
    data["linear"]["api_key"] = "must-not-be-configured"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BlackcellConfig.model_validate(data)


def test_runtime_secrets_do_not_require_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    secrets = RuntimeSecrets()

    assert secrets.linear_api_key is None
    assert secrets.github_token is None


def test_find_config_prefers_explicit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "explicit.toml"
    explicit.write_text("schema_version = '0.1'\n", encoding="utf-8")
    monkeypatch.setenv("BLACKCELL_CONFIG", str(tmp_path / "missing.toml"))

    assert find_config(explicit) == explicit.resolve()


def test_load_config_wraps_invalid_toml(tmp_path: Path) -> None:
    path = tmp_path / "blackcell.toml"
    path.write_text("not = [valid", encoding="utf-8")

    with pytest.raises(ValidationFailure, match="Invalid Blackcell configuration"):
        load_config(path)
