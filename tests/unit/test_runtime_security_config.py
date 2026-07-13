from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from blackcell.config import (
    API_TOKEN_ENV,
    API_TOKEN_FILE_ENV,
    BIND_HOST_ENV,
    BIND_PORT_ENV,
    DATA_DIR_ENV,
    TRUSTED_PROXY_HOPS_ENV,
    RuntimePaths,
    RuntimeSecurityConfig,
    SecretValue,
    SecurityConfigError,
    SecurityConfigFailureCode,
    load_service_token,
)

TOKEN = "Runtime-v1_opaque-token.0123456789-ABCDEFG"


def test_runtime_config_creates_owner_only_paths_and_redaction_policy(tmp_path: Path) -> None:
    data_root = tmp_path / "runtime-data"
    config = RuntimeSecurityConfig.from_environment(
        {DATA_DIR_ENV: str(data_root), API_TOKEN_ENV: TOKEN}
    )

    assert config.paths == RuntimePaths(
        data_root,
        data_root / "kernel.sqlite3",
        data_root / "artifacts",
        data_root / "backups",
    )
    assert config.bind_host == "127.0.0.1"
    assert config.bind_port == 8080
    assert config.trusted_proxy_hops == 0
    assert stat.S_IMODE(data_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.paths.artifact_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.paths.backup_root.stat().st_mode) == 0o700
    assert TOKEN not in repr(config)
    assert TOKEN not in repr(config.telemetry_policy())
    assert config.telemetry_policy().sanitize({"message": f"failed with {TOKEN}"}) == {
        "message": "[REDACTED]"
    }
    assert config.authenticator().authenticate((f"Bearer {TOKEN}",)) == config.principal


def test_runtime_config_accepts_explicit_non_loopback_bind_but_never_skips_auth(
    tmp_path: Path,
) -> None:
    config = RuntimeSecurityConfig.from_environment(
        {
            DATA_DIR_ENV: str(tmp_path / "data"),
            API_TOKEN_ENV: TOKEN,
            BIND_HOST_ENV: "0.0.0.0",
            BIND_PORT_ENV: "9000",
        }
    )

    assert config.bind_host == "0.0.0.0"
    assert config.bind_port == 9000
    with pytest.raises(PermissionError, match="missing-credential"):
        config.authenticator().authenticate(())


@pytest.mark.parametrize(
    ("updates", "code"),
    (
        ({}, SecurityConfigFailureCode.INVALID_DATA_DIRECTORY),
        ({DATA_DIR_ENV: "relative"}, SecurityConfigFailureCode.INVALID_DATA_DIRECTORY),
        ({BIND_HOST_ENV: "localhost"}, SecurityConfigFailureCode.INVALID_BIND),
        ({BIND_PORT_ENV: "0"}, SecurityConfigFailureCode.INVALID_BIND),
        ({BIND_PORT_ENV: "65536"}, SecurityConfigFailureCode.INVALID_BIND),
        ({BIND_PORT_ENV: "not-a-port"}, SecurityConfigFailureCode.INVALID_BIND),
        ({TRUSTED_PROXY_HOPS_ENV: "1"}, SecurityConfigFailureCode.INVALID_PROXY_TRUST),
    ),
)
def test_runtime_config_rejects_implicit_paths_and_unsafe_network_values(
    tmp_path: Path,
    updates: dict[str, str],
    code: SecurityConfigFailureCode,
) -> None:
    environment = {DATA_DIR_ENV: str(tmp_path / "data"), API_TOKEN_ENV: TOKEN}
    environment.update(updates)
    if not updates:
        environment.pop(DATA_DIR_ENV)

    with pytest.raises(SecurityConfigError) as caught:
        RuntimeSecurityConfig.from_environment(environment)

    assert caught.value.code is code
    assert str(caught.value) == code.value


def test_data_root_rejects_symlinks_loose_permissions_and_unsafe_database(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    symlink = tmp_path / "linked"
    symlink.symlink_to(target, target_is_directory=True)
    with pytest.raises(SecurityConfigError, match="unsafe-data-directory"):
        RuntimePaths.prepare(str(symlink))

    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    os.chmod(loose, 0o755)
    with pytest.raises(SecurityConfigError, match="unsafe-data-directory"):
        RuntimePaths.prepare(str(loose))

    root = tmp_path / "database-root"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    database = root / "kernel.sqlite3"
    database.write_bytes(b"")
    os.chmod(database, 0o640)
    with pytest.raises(SecurityConfigError, match="unsafe-data-directory"):
        RuntimePaths.prepare(str(root))


def test_service_token_file_requires_absolute_owner_only_regular_file(tmp_path: Path) -> None:
    credential = tmp_path / "api-token"
    credential.write_text(f"{TOKEN}\n", encoding="utf-8")
    os.chmod(credential, 0o600)

    token = load_service_token({API_TOKEN_FILE_ENV: str(credential)})

    assert token.verify(TOKEN)
    assert not token.verify(f"{TOKEN}x")
    assert str(token) == "[REDACTED]"
    assert repr(token) == "SecretValue([REDACTED])"
    assert TOKEN not in repr(token)


def test_service_token_sources_and_file_contract_fail_closed(tmp_path: Path) -> None:
    credential = tmp_path / "api-token"
    credential.write_text(TOKEN, encoding="utf-8")
    os.chmod(credential, 0o600)

    with pytest.raises(SecurityConfigError) as missing:
        load_service_token({})
    assert missing.value.code is SecurityConfigFailureCode.MISSING_SECRET
    with pytest.raises(SecurityConfigError) as ambiguous:
        load_service_token({API_TOKEN_ENV: TOKEN, API_TOKEN_FILE_ENV: str(credential)})
    assert ambiguous.value.code is SecurityConfigFailureCode.AMBIGUOUS_SECRET_SOURCE

    os.chmod(credential, 0o644)
    with pytest.raises(SecurityConfigError, match="unsafe-secret-file"):
        load_service_token({API_TOKEN_FILE_ENV: str(credential)})
    os.chmod(credential, 0o600)

    linked = tmp_path / "linked-token"
    linked.symlink_to(credential)
    with pytest.raises(SecurityConfigError, match="unsafe-secret-file"):
        load_service_token({API_TOKEN_FILE_ENV: str(linked)})
    with pytest.raises(SecurityConfigError, match="unsafe-secret-file"):
        load_service_token({API_TOKEN_FILE_ENV: credential.name})
    with pytest.raises(SecurityConfigError, match="unsafe-secret-file"):
        load_service_token(
            {API_TOKEN_FILE_ENV: str(credential)},
            expected_uid=credential.stat().st_uid + 1,
        )


@pytest.mark.parametrize(
    "value",
    (
        "short",
        "x" * 40,
        "contains whitespace but is definitely long enough",
        "comma,separated-token-that-is-definitely-long-enough",
        "line-one-is-long-enough-0123456789\nline-two",
    ),
)
def test_weak_or_header_ambiguous_service_tokens_are_rejected(value: str) -> None:
    with pytest.raises(SecurityConfigError) as caught:
        load_service_token({API_TOKEN_ENV: value})

    assert caught.value.code is SecurityConfigFailureCode.INVALID_SECRET
    assert value not in str(caught.value)


def test_secret_value_does_not_expose_raw_equality_or_display() -> None:
    first = SecretValue(TOKEN)
    second = SecretValue(TOKEN)

    assert first is not second
    assert first != second
    assert first.verify(TOKEN) and second.verify(TOKEN)
