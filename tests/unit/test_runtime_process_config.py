from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from blackcell.config import (
    API_BACKPRESSURE_ENV,
    API_TOKEN_ENV,
    DATA_DIR_ENV,
    GRACEFUL_TIMEOUT_SECONDS_ENV,
    REPOSITORY_ROOT_ENV,
    WORKER_ID_ENV,
    WORKER_LEASE_SECONDS_ENV,
    WORKER_POLL_MILLISECONDS_ENV,
    ProcessConfigError,
    ProcessConfigFailureCode,
    RuntimeProcessConfig,
)

TOKEN = "Runtime-v1_process-token.0123456789-ABCDEFG"


def test_process_config_uses_bounded_explicit_runtime_defaults(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")

    config = RuntimeProcessConfig.from_environment(
        {
            DATA_DIR_ENV: str(tmp_path / "data"),
            API_TOKEN_ENV: TOKEN,
            REPOSITORY_ROOT_ENV: str(repository),
        },
        hostname="runtime-host",
        process_id=42,
    )

    assert config.repository_root == repository
    assert config.graceful_timeout_seconds == 30
    assert config.api_backpressure == 64
    assert config.worker_poll_milliseconds == 250
    assert config.worker_lease_seconds == 30
    assert config.worker_id == "worker:runtime-host:42"


def test_process_config_accepts_explicit_bounded_lifecycle_values(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")

    config = RuntimeProcessConfig.from_environment(
        {
            DATA_DIR_ENV: str(tmp_path / "data"),
            API_TOKEN_ENV: TOKEN,
            REPOSITORY_ROOT_ENV: str(repository),
            GRACEFUL_TIMEOUT_SECONDS_ENV: "300",
            API_BACKPRESSURE_ENV: "1024",
            WORKER_POLL_MILLISECONDS_ENV: "60000",
            WORKER_LEASE_SECONDS_ENV: "86400",
            WORKER_ID_ENV: "worker:runtime-1",
        }
    )

    assert config.graceful_timeout_seconds == 300
    assert config.api_backpressure == 1024
    assert config.worker_poll_milliseconds == 60_000
    assert config.worker_lease_seconds == 86_400
    assert config.worker_id == "worker:runtime-1"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        (REPOSITORY_ROOT_ENV, "relative", ProcessConfigFailureCode.INVALID_REPOSITORY_ROOT),
        (
            GRACEFUL_TIMEOUT_SECONDS_ENV,
            "0",
            ProcessConfigFailureCode.INVALID_GRACEFUL_TIMEOUT,
        ),
        (API_BACKPRESSURE_ENV, "1025", ProcessConfigFailureCode.INVALID_API_BACKPRESSURE),
        (WORKER_POLL_MILLISECONDS_ENV, "9", ProcessConfigFailureCode.INVALID_WORKER_POLL),
        (WORKER_LEASE_SECONDS_ENV, "86401", ProcessConfigFailureCode.INVALID_WORKER_LEASE),
        (WORKER_ID_ENV, "worker with space", ProcessConfigFailureCode.INVALID_WORKER_ID),
    ),
)
def test_process_config_rejects_implicit_or_unbounded_values_content_free(
    tmp_path: Path,
    field: str,
    value: str,
    code: ProcessConfigFailureCode,
) -> None:
    repository = _repository(tmp_path / "repository")
    environment = {
        DATA_DIR_ENV: str(tmp_path / "data"),
        API_TOKEN_ENV: TOKEN,
        REPOSITORY_ROOT_ENV: str(repository),
        field: value,
    }

    with pytest.raises(ProcessConfigError) as caught:
        RuntimeProcessConfig.from_environment(environment)

    assert caught.value.code is code
    assert str(caught.value) == code.value
    assert value not in str(caught.value)


def test_process_config_rejects_missing_and_symlinked_repository_roots(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    linked = tmp_path / "linked-repository"
    linked.symlink_to(repository, target_is_directory=True)

    for value in (tmp_path / "missing", linked):
        with pytest.raises(ProcessConfigError) as caught:
            RuntimeProcessConfig.from_environment(
                {
                    DATA_DIR_ENV: str(tmp_path / f"data-{value.name}"),
                    API_TOKEN_ENV: TOKEN,
                    REPOSITORY_ROOT_ENV: str(value),
                }
            )
        assert caught.value.code is ProcessConfigFailureCode.INVALID_REPOSITORY_ROOT


def _repository(path: Path) -> Path:
    path.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return path
